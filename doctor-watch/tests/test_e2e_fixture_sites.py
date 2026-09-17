"""로컬 모의 병원 사이트 2주치를 돌려 탐색→수집→추출→비교→이직연결→보고서 전 과정을 검증한다."""
import asyncio
import http.server
import os
import socketserver
import threading
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


class _Switchable(http.server.SimpleHTTPRequestHandler):
    """주차 전환이 가능한 정적 서버 (root 를 바꾸면 같은 URL 이 다른 주차 내용을 돌려준다)."""

    root = FIX / "week1"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(self.root), **kw)

    def log_message(self, *a):  # noqa: D102
        pass


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    db = tmp_path_factory.mktemp("db") / "t.db"
    os.environ["DOCTOR_WATCH_DB"] = str(db)
    os.environ["DOCTOR_WATCH_REPORTS"] = str(tmp_path_factory.mktemp("reports"))
    os.environ["ANTHROPIC_API_KEY"] = ""
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    from doctor_watch import config

    config.settings = config.Settings()
    config.settings.per_host_delay = 0.0
    import doctor_watch.db as D
    from doctor_watch import diff, extract, fetch, pipeline, report

    for m in (fetch, pipeline, extract, report, D, diff):
        m.settings = config.settings
    return config.settings


@pytest.fixture(scope="module")
def server():
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("127.0.0.1", 0), _Switchable)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_two_week_cycle(env, server):
    from doctor_watch import db as D
    from doctor_watch.pipeline import run_collection
    from doctor_watch.report import load_briefing, markdown_report, summary_text, write_reports

    srv, port = server
    _Switchable.root = FIX / "week1"
    base = f"http://127.0.0.1:{port}"
    with D.session() as conn:
        for name, path in [("하나종합병원", "/hana/index.html"), ("부산중앙병원", "/busan-central/index.html"), ("해운병원", "/haeun/index.html")]:
            D.upsert_hospital(conn, {"name": name, "url": base + path, "sido": "부산"})
        D.add_watch(conn, "이영희", "하나종합병원", "소화기내과", "KOL")

    run1 = asyncio.run(run_collection(prefer_llm=False, concurrency=4))
    with D.session() as conn:
        st = D.stats_of(conn.execute("SELECT * FROM runs WHERE id=?", (run1,)).fetchone())
        assert st["ok"] == 3, st
        assert st["doctors"] == 11, st
        assert st["changes"] == 0  # 최초 실행은 기준선
        hana = conn.execute("SELECT * FROM hospitals WHERE name='하나종합병원'").fetchone()
        assert D.hospital_staff_urls(hana), "의료진 페이지 자동 탐색 실패"
        names = {r["name"] for r in D.load_roster(conn, run1, hana["id"])}
        assert names == {"김민수", "이영희", "박준호", "최수진", "정우성"}

    # 2주차: 같은 URL 에서 내용만 바뀐다
    _Switchable.root = FIX / "week2"
    run2 = asyncio.run(run_collection(prefer_llm=False, concurrency=4))

    with D.session() as conn:
        st = D.stats_of(conn.execute("SELECT * FROM runs WHERE id=?", (run2,)).fetchone())
        assert st["ok"] == 2 and st["failed"] == 1, st  # 해운병원은 사이트 다운
        b = load_briefing(conn, run2)
        kinds = {(c["hospital_name"], c["kind"], c["name"]) for c in b["changes"]}
        # 합류/제외/직위 변경 모두 2회 연속 확인 후 확정 → 2주차에는 아직 잡히지 않는다
        assert kinds == set(), kinds
        assert not any(h == "해운병원" for h, _, _ in kinds)
        pend = conn.execute("SELECT name, status, miss_count FROM doctor_state WHERE name IN ('이영희','강다혜') ORDER BY name, hospital_id").fetchall()
        assert {(r["name"], r["status"]) for r in pend} == {("강다혜", "pending"), ("이영희", "present"), ("이영희", "pending")}
        # 변경 없는 페이지는 캐시로 재추출 생략
        cached = conn.execute("SELECT COUNT(*) FROM pages WHERE run_id=? AND extracted_by='cache'", (run2,)).fetchone()[0]
        assert cached >= 1


def test_third_week_confirms_changes_and_links_move(env, server):
    """3주차(내용 동일): 하나종합병원 이영희 제외·강다혜 합류 확정, 부산중앙병원 이영희 합류 확정 → 이직 연결·고객명단 표시."""
    from doctor_watch import db as D
    from doctor_watch.pipeline import run_collection
    from doctor_watch.report import load_briefing, markdown_report, summary_text, write_reports

    _Switchable.root = FIX / "week3"
    run3 = asyncio.run(run_collection(prefer_llm=False, concurrency=4))
    with D.session() as conn:
        b = load_briefing(conn, run3)
        kinds = {(c["hospital_name"], c["kind"], c["name"]) for c in b["changes"]}
        assert kinds == {("하나종합병원", "left", "이영희"), ("하나종합병원", "joined", "강다혜"), ("하나종합병원", "position_changed", "박준호"), ("부산중앙병원", "joined", "이영희")}, kinds
        assert {f["hospital_name"] for f in b["failed"]} == {"해운병원"}
        # 이직 연결
        assert len(b["moves"]) == 1 and b["moves"][0]["related_hospital_name"] == "부산중앙병원"
        # 관심 명단 대조 (하나종합병원 제외 + 부산중앙병원 합류 모두 표시)
        assert {c["hospital_name"] for c in b["watch_hits"]} == {"하나종합병원", "부산중앙병원"}
        md = markdown_report(b)
        assert "이영희" in md and "이직 추정" in md
        assert "고객 명단 변동" in summary_text(b)
        files = write_reports(conn, run3)
        assert files["latest_html"].exists() and files["md"].exists()
        site = files["latest_html"].parent
        assert (site / "hospitals.html").exists() and (site / "search.html").exists() and (site / "runs.html").exists()
        hid = conn.execute("SELECT id FROM hospitals WHERE name='부산중앙병원'").fetchone()[0]
        page = (site / f"hospital-{hid}.html").read_text(encoding="utf-8")
        assert "이영희" in page and "← 하나종합병원" in page
        assert "이영희" in (site / "search.html").read_text(encoding="utf-8")
        failed_csv = (site / "failed-hospitals.csv").read_text(encoding="utf-8-sig")
        assert "해운병원" in failed_csv and "staff_urls" in failed_csv


def test_web_app_renders(env):
    from fastapi.testclient import TestClient
    from doctor_watch.web import app

    c = TestClient(app)
    assert c.get("/").status_code == 200
    assert "이직 추정" in c.get("/").text
    assert c.get("/hospitals").status_code == 200
    r = c.get("/doctors?q=이영희")
    assert r.status_code == 200 and "부산중앙병원" in r.text
    assert c.get("/watchlist").status_code == 200
    assert c.get("/runs").status_code == 200
    j = c.get("/api/briefing").json()
    assert j["stats"]["moves"] == 1
    hid = c.get("/api/briefing").json()["moves"][0]["hospital_id"]
    assert c.get(f"/hospitals/{hid}").status_code == 200
    assert len(c.get(f"/api/hospitals/{hid}/roster").json()) == 5
