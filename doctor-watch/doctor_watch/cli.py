"""명령행 인터페이스.  python -m doctor_watch --help"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from pathlib import Path

from . import db as D
from .config import settings
from .names import name_key


def cmd_init_hospitals(a: argparse.Namespace) -> None:
    """심평원 API 로 병원 목록(홈페이지 포함)을 가져와 저장."""
    from .hira import iter_hospitals

    cl = [c.strip() for c in a.cl.split(",")] if a.cl else ["상급종합", "종합병원", "병원"]
    sidos = [s.strip() for s in a.sido.split(",")] if a.sido else [None]
    n = n_url = 0
    with D.session() as conn:
        for sido in sidos:
            for h in iter_hospitals(sido=sido, cl_codes=cl):
                if a.with_url_only and not h.get("url"):
                    continue
                D.upsert_hospital(conn, h)
                n += 1
                n_url += 1 if h.get("url") else 0
    print(f"저장 {n}곳 (홈페이지 보유 {n_url}곳)")


def cmd_hira_test(a: argparse.Namespace) -> None:
    """서비스키가 동작하는지 1페이지만 호출해 확인."""
    from .hira import BASIS_URL, CL_CODES, SIDO_CODES, HiraError, _items, _service_key

    import httpx

    key = _service_key(settings.hira_service_key)
    if not key:
        print("HIRA_SERVICE_KEY 가 비어 있습니다. .env 를 확인하세요.")
        sys.exit(1)
    params = {"serviceKey": key, "pageNo": 1, "numOfRows": 3, "_type": "json", "sidoCd": SIDO_CODES.get(a.sido, a.sido), "clCd": CL_CODES["종합병원"]}
    r = httpx.get(BASIS_URL, params=params, timeout=60)
    print("HTTP", r.status_code)
    try:
        rows, total = _items(r.json())
    except (ValueError, HiraError) as e:
        print("응답 해석 실패:", e)
        print(r.text[:500])
        sys.exit(1)
    print(f"정상: {a.sido} 종합병원 {total}곳")
    for row in rows:
        print(" -", row.get("yadmNm"), "|", row.get("hospUrl") or "(홈페이지 없음)", "| 의사", row.get("drTotCnt"))


def cmd_import_hospitals(a: argparse.Namespace) -> None:
    """CSV(name,url[,ykiho,sido,cl_name,addr,staff_urls]) 로 병원 추가. staff_urls 는 '|' 구분."""
    n = 0
    with D.session() as conn, open(a.csv, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if not row.get("name"):
                continue
            h = {k: (v.strip() or None) for k, v in row.items() if k}
            if h.get("staff_urls"):
                h["staff_urls"] = [u.strip() for u in h["staff_urls"].split("|") if u.strip()]
            D.upsert_hospital(conn, h)
            n += 1
    print(f"가져오기 완료: {n}곳")


def cmd_hospitals(a: argparse.Namespace) -> None:
    with D.session() as conn:
        rows = D.list_hospitals(conn, active_only=not a.all, with_url_only=False)
        for r in rows:
            urls = D.hospital_staff_urls(r)
            print(f"{r['id']:>5}  {r['name']:<24} {r['sido'] or '':<4} {r['cl_name'] or '':<6} {r['url'] or '-':<40} staff={len(urls)} {('[수동]' if r['staff_urls_manual'] else '')} {r['last_status'] or ''}")
        print(f"총 {len(rows)}곳")


def cmd_set_staff_urls(a: argparse.Namespace) -> None:
    with D.session() as conn:
        D.set_staff_urls(conn, a.hospital_id, a.urls, manual=True)
    print("저장됨")


def cmd_run(a: argparse.Namespace) -> None:
    from .pipeline import run_collection

    def progress(done: int, total: int, res: dict) -> None:
        with D.session() as conn:
            h = D.get_hospital(conn, res["hospital_id"])
        status = f"의사 {len(res['doctors'])}명" if res["ok"] else f"실패: {res['error']}"
        print(f"[{done}/{total}] {h['name'] if h else res['hospital_id']}: {status}", flush=True)

    run_id = asyncio.run(
        run_collection(
            hospital_ids=a.hospital_id or None,
            limit=a.limit,
            prefer_llm=not a.no_llm,
            concurrency=a.concurrency,
            progress=progress,
        )
    )
    with D.session() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        print(json.dumps(D.stats_of(run), ensure_ascii=False))
        if a.report:
            from .report import write_reports

            files = write_reports(conn, run_id)
            print("보고서:", files["html"])
    if a.notify:
        cmd_notify(argparse.Namespace(run_id=run_id, dry_run=False))


def cmd_report(a: argparse.Namespace) -> None:
    from .report import load_briefing, markdown_report, write_reports

    with D.session() as conn:
        if a.stdout:
            print(markdown_report(load_briefing(conn, a.run_id)))
        else:
            files = write_reports(conn, a.run_id)
            for k, p in files.items():
                print(f"{k}: {p}")


def cmd_notify(a: argparse.Namespace) -> None:
    from .notify import broadcast
    from .report import html_report, load_briefing, summary_text

    with D.session() as conn:
        b = load_briefing(conn, getattr(a, "run_id", None))
    text = summary_text(b)
    html = html_report(b)
    date = ((b["run"] or {}).get("finished_at") or "")[:10]
    if getattr(a, "dry_run", False):
        print(text)
        return
    res = broadcast(f"[doctor-watch] 의료진 변동 브리핑 {date}", text, html)
    print("발송 결과:", res if any(res.values()) else "설정된 채널 없음 (.env 의 SLACK_WEBHOOK_URL / TELEGRAM_* / SMTP_* 확인)")


def cmd_watchlist(a: argparse.Namespace) -> None:
    with D.session() as conn:
        if a.action == "add":
            wid = D.add_watch(conn, a.name, a.hospital, a.department, a.memo)
            print(f"추가됨 #{wid}")
        elif a.action == "import":
            n = 0
            with open(a.csv, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    if row.get("name"):
                        D.add_watch(conn, row["name"], row.get("hospital") or row.get("hospital_name"), row.get("department"), row.get("memo"))
                        n += 1
            print(f"{n}명 가져옴")
        elif a.action == "remove":
            conn.execute("DELETE FROM watchlist WHERE id=?", (a.id,))
            print("삭제됨")
        else:
            for w in D.list_watchlist(conn):
                print(f"{w['id']:>4}  {w['name']:<8} {w['hospital_name'] or '':<20} {w['department'] or '':<12} {w['memo'] or ''}")


def cmd_search(a: argparse.Namespace) -> None:
    """의사 이름으로 현재 소속(최신 스냅샷)과 변동 이력 검색."""
    key = name_key(a.name)
    with D.session() as conn:
        rows = conn.execute(
            """
            SELECT r.*, h.name AS hospital_name, ru.week_key FROM roster r
            JOIN hospitals h ON h.id=r.hospital_id JOIN runs ru ON ru.id=r.run_id
            WHERE r.name_key=? AND r.run_id = (SELECT MAX(run_id) FROM hospital_runs WHERE ok=1 AND hospital_id=r.hospital_id)
            ORDER BY h.name
            """,
            (key,),
        ).fetchall()
        print(f"현재 소속 ({len(rows)}건):")
        for r in rows:
            print(f"  {r['hospital_name']} · {r['department'] or '-'} · {r['position'] or '-'} ({r['week_key']})")
        chs = conn.execute(
            "SELECT c.*, h.name AS hospital_name, rh.name AS rel FROM changes c JOIN hospitals h ON h.id=c.hospital_id LEFT JOIN hospitals rh ON rh.id=c.related_hospital_id WHERE c.name_key=? ORDER BY c.id DESC",
            (key,),
        ).fetchall()
        print(f"변동 이력 ({len(chs)}건):")
        for c in chs:
            arrow = ("→ " if c["kind"] == "left" else "← ") + c["rel"] if c["rel"] else ""
            print(f"  {c['detected_at'][:10]} {c['kind']:<16} {c['hospital_name']} {c['department'] or ''} {arrow}")


def cmd_serve(a: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("doctor_watch.web:app", host=a.host, port=a.port, reload=a.reload)


def cmd_hira_counts(a: argparse.Namespace) -> None:
    from .pipeline import refresh_hira_counts

    cl = [c.strip() for c in a.cl.split(",")] if a.cl else ["상급종합", "종합병원", "병원"]
    n = refresh_hira_counts(sido=a.sido, cl=cl)
    print(f"심평원 의사 수 변동 {n}건 기록")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="doctor-watch", description="병원 홈페이지 의료진 변동(이직) 주간 추적기")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init-hospitals", help="심평원 API 로 병원 목록 가져오기 (HIRA_SERVICE_KEY 필요)")
    s.add_argument("--sido", help="시도명 콤마 구분 (예: 부산,울산,경남). 생략 시 전국")
    s.add_argument("--cl", help="종별 콤마 구분 (기본: 상급종합,종합병원,병원)")
    s.add_argument("--with-url-only", action="store_true", help="홈페이지가 있는 병원만 저장")
    s.set_defaults(fn=cmd_init_hospitals)

    s = sub.add_parser("hira-test", help="심평원 서비스키 동작 확인 (1페이지 호출)")
    s.add_argument("--sido", default="부산")
    s.set_defaults(fn=cmd_hira_test)

    s = sub.add_parser("import-hospitals", help="CSV 로 병원 추가 (name,url,staff_urls ...)")
    s.add_argument("csv")
    s.set_defaults(fn=cmd_import_hospitals)

    s = sub.add_parser("hospitals", help="병원 목록 보기")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_hospitals)

    s = sub.add_parser("set-staff-urls", help="병원의 의료진 페이지 URL 수동 지정")
    s.add_argument("hospital_id", type=int)
    s.add_argument("urls", nargs="+")
    s.set_defaults(fn=cmd_set_staff_urls)

    s = sub.add_parser("run", help="수집 실행 (탐색→수집→추출→비교)")
    s.add_argument("--limit", type=int)
    s.add_argument("--hospital-id", type=int, action="append")
    s.add_argument("--concurrency", type=int)
    s.add_argument("--no-llm", action="store_true", help="Claude 를 쓰지 않고 규칙 기반 추출만 사용")
    s.add_argument("--report", action="store_true", help="실행 후 보고서 파일 생성")
    s.add_argument("--notify", action="store_true", help="실행 후 브리핑 발송")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("report", help="보고서 생성")
    s.add_argument("--run-id", type=int)
    s.add_argument("--stdout", action="store_true")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("notify", help="브리핑 발송")
    s.add_argument("--run-id", type=int)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_notify)

    s = sub.add_parser("watchlist", help="고객(관심) 의사 명단 관리")
    s.add_argument("action", choices=["list", "add", "import", "remove"])
    s.add_argument("--name")
    s.add_argument("--hospital")
    s.add_argument("--department")
    s.add_argument("--memo")
    s.add_argument("--csv")
    s.add_argument("--id", type=int)
    s.set_defaults(fn=cmd_watchlist)

    s = sub.add_parser("search", help="의사 이름 검색")
    s.add_argument("name")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("hira-counts", help="심평원 신고 의사 수 갱신/변동 기록")
    s.add_argument("--sido")
    s.add_argument("--cl")
    s.set_defaults(fn=cmd_hira_counts)

    s = sub.add_parser("serve", help="웹 대시보드 실행")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> None:
    a = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    a.fn(a)


if __name__ == "__main__":
    main()
