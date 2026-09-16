"""주간 수집 파이프라인: 탐색 → 수집 → 추출 → 스냅샷 저장 → 비교 → 이직 연결 → 관심명단 대조."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from typing import Any

from . import db as D
from .config import settings
from .diff import diff_rosters, link_moves, mark_watchlist_hits, record_changes
from .discover import candidate_links, department_links, select_option_links
from .extract import extract
from .fetch import Fetcher, content_hash, visible_text

log = logging.getLogger(__name__)

MIN_STAFF_TEXT = 30  # 이보다 짧은 페이지는 의료진 페이지로 보지 않음


async def discover_staff_urls(fetcher: Fetcher, hospital: sqlite3.Row) -> tuple[list[str], str | None]:
    """홈페이지에서 의료진 페이지 후보 URL 을 찾는다. (urls, error)"""
    home = await fetcher.get(hospital["url"])
    if not home.ok:
        return [], home.error or f"HTTP {home.status}"
    cands = candidate_links(home.final_url, home.html)
    urls = [u for _, u, _ in cands[:6]]
    if not urls:
        # 흔한 경로를 직접 시도
        base = home.final_url.rstrip("/")
        for path in ("/doctor", "/doctors", "/medical/doctor", "/kr/doctor", "/dr", "/staff", "/medical-staff"):
            pg = await fetcher.get(base + path)
            if pg.ok and len(visible_text(pg.html)) > MIN_STAFF_TEXT:
                urls.append(pg.final_url)
                break
    return urls, None


async def collect_hospital(fetcher: Fetcher, conn_factory, hospital: sqlite3.Row, run_id: int, prefer_llm: bool = True) -> dict[str, Any]:
    """병원 한 곳 수집. 결과 dict 를 반환하고 DB 기록은 호출 측(단일 스레드)에서 수행한다."""
    hid = hospital["id"]
    result: dict[str, Any] = {"hospital_id": hid, "ok": False, "doctors": [], "pages": [], "llm_calls": 0, "error": None, "staff_urls": None}
    staff_urls = D.hospital_staff_urls(hospital)
    manual = bool(hospital["staff_urls_manual"])
    if not staff_urls:
        staff_urls, err = await discover_staff_urls(fetcher, hospital)
        if err:
            result["error"] = f"홈페이지 접속 실패: {err}"
            return result
        if not staff_urls:
            result["error"] = "의료진 페이지를 찾지 못함 (staff_urls 수동 지정 필요)"
            return result
        result["staff_urls"] = staff_urls

    queue: list[str] = list(staff_urls)
    visited: set[str] = set()
    doctors: dict[tuple[str, str], dict[str, Any]] = {}
    pages_ok = pages_failed = 0
    max_pages = settings.max_pages_per_hospital

    while queue and len(visited) < max_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        page = await fetcher.get(url)
        rec: dict[str, Any] = {"url": url, "status": page.status, "hash": None, "text_len": 0, "extracted_by": "none", "error": page.error}
        if not page.ok:
            pages_failed += 1
            result["pages"].append(rec)
            continue
        text = visible_text(page.html)
        rec["text_len"] = len(text)
        if len(text) < MIN_STAFF_TEXT:
            result["pages"].append(rec)
            continue
        h = content_hash(text)
        rec["hash"] = h
        # 캐시: 동일 내용이면 재추출 생략
        with conn_factory() as c:
            cached = D.cache_get(c, h)
        if cached:
            found, method = cached
            rec["extracted_by"] = "cache"
        else:
            found, method = await asyncio.to_thread(extract, text, hospital["name"], url, prefer_llm)
            rec["extracted_by"] = method
            if method == "llm":
                result["llm_calls"] += 1
            with conn_factory() as c:
                D.cache_put(c, h, found, method, settings.model if method == "llm" else None)
        pages_ok += 1
        for d in found:
            key = (d["name_key"], d.get("department") or "")
            if key not in doctors:
                doctors[key] = d
        result["pages"].append(rec)
        # 하위 페이지(진료과별/페이지네이션) 추가 탐색은 최초 지정 URL 에서만
        if url in staff_urls and not manual or (manual and url in staff_urls and len(staff_urls) <= 3):
            for sub in department_links(page.final_url, page.html) + select_option_links(page.final_url, page.html):
                if sub not in visited and sub not in queue and len(visited) + len(queue) < max_pages:
                    queue.append(sub)

    result["pages_ok"], result["pages_failed"] = pages_ok, pages_failed
    result["doctors"] = list(doctors.values())
    if pages_ok == 0:
        result["error"] = "의료진 페이지 수집 실패 (모든 페이지 오류)"
    elif not doctors:
        result["error"] = "의료진 페이지에서 의사를 찾지 못함"
    else:
        result["ok"] = True
    return result


def persist_result(conn: sqlite3.Connection, run_id: int, res: dict[str, Any]) -> None:
    hid = res["hospital_id"]
    ts = D.now_iso()
    for p in res["pages"]:
        conn.execute(
            "INSERT INTO pages (run_id, hospital_id, url, http_status, content_hash, text_len, extracted_by, error, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, hid, p["url"], p["status"], p["hash"], p["text_len"], p["extracted_by"], p["error"], ts),
        )
    if res.get("staff_urls"):
        D.set_staff_urls(conn, hid, res["staff_urls"], manual=False)
    conn.execute(
        "INSERT OR REPLACE INTO hospital_runs (run_id, hospital_id, ok, doctor_count, pages_ok, pages_failed, llm_calls, error) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, hid, 1 if res["ok"] else 0, len(res["doctors"]), res.get("pages_ok", 0), res.get("pages_failed", 0), res["llm_calls"], res["error"]),
    )
    if res["ok"]:
        D.save_roster(conn, run_id, hid, res["doctors"])
        conn.execute("UPDATE hospitals SET last_status='ok', last_ok_at=?, updated_at=? WHERE id=?", (ts, ts, hid))
    else:
        conn.execute("UPDATE hospitals SET last_status=?, updated_at=? WHERE id=?", (res["error"], ts, hid))
    conn.commit()


async def run_collection(
    hospital_ids: list[int] | None = None,
    limit: int | None = None,
    prefer_llm: bool = True,
    concurrency: int | None = None,
    progress=None,
) -> int:
    """전체 파이프라인 실행. run_id 반환."""
    conn = D.connect()
    run_id = D.start_run(conn)
    hospitals = D.list_hospitals(conn, active_only=True, with_url_only=True)
    if hospital_ids:
        hospitals = [h for h in hospitals if h["id"] in set(hospital_ids)]
    if limit:
        hospitals = hospitals[:limit]
    log.info("run %s: %d hospitals", run_id, len(hospitals))

    fetcher = Fetcher(concurrency=concurrency)
    stats = {"hospitals": len(hospitals), "ok": 0, "failed": 0, "doctors": 0, "llm_calls": 0, "changes": 0, "moves": 0, "watchlist_hits": 0}
    conn_factory = lambda: D.session()  # noqa: E731
    try:
        sem = asyncio.Semaphore(concurrency or settings.concurrency)

        async def one(h):
            async with sem:
                try:
                    return await collect_hospital(fetcher, conn_factory, h, run_id, prefer_llm)
                except Exception as e:  # noqa: BLE001
                    log.exception("hospital %s failed", h["name"])
                    return {"hospital_id": h["id"], "ok": False, "doctors": [], "pages": [], "llm_calls": 0, "error": f"{type(e).__name__}: {e}"}

        tasks = [asyncio.create_task(one(h)) for h in hospitals]
        done = 0
        for fut in asyncio.as_completed(tasks):
            res = await fut
            persist_result(conn, run_id, res)
            stats["ok" if res["ok"] else "failed"] += 1
            stats["doctors"] += len(res["doctors"])
            stats["llm_calls"] += res["llm_calls"]
            done += 1
            if progress:
                progress(done, len(hospitals), res)
    finally:
        await fetcher.close()

    # ── 비교 ──
    for h in hospitals:
        hr = conn.execute("SELECT ok FROM hospital_runs WHERE run_id=? AND hospital_id=?", (run_id, h["id"])).fetchone()
        if not hr or not hr["ok"]:
            continue
        prev_run = D.previous_ok_run_for_hospital(conn, h["id"], run_id)
        if prev_run is None:
            continue  # 최초 스냅샷은 기준선으로만 사용
        prev = D.load_roster(conn, prev_run, h["id"])
        cur = D.load_roster(conn, run_id, h["id"])
        # 명단이 통째로 사라진 경우(사이트 개편 등)는 오탐 가능성이 높아 '검토 필요' 로만 기록
        if prev and cur and len(cur) < len(prev) * 0.5 and len(prev) >= 6:
            conn.execute(
                "UPDATE hospital_runs SET error=? WHERE run_id=? AND hospital_id=?",
                (f"의료진 수 급감({len(prev)}→{len(cur)}): 페이지 구조 변경 여부 확인 필요", run_id, h["id"]),
            )
            continue
        changes = diff_rosters(prev, cur)
        stats["changes"] += record_changes(conn, run_id, h["id"], changes)
    conn.commit()
    stats["moves"] = link_moves(conn, run_id)
    stats["watchlist_hits"] = mark_watchlist_hits(conn, run_id)
    D.finish_run(conn, run_id, "done", stats)
    conn.close()
    log.info("run %s done: %s", run_id, json.dumps(stats, ensure_ascii=False))
    return run_id


def refresh_hira_counts(run_id: int | None = None, sido: str | None = None, cl: list[str] | None = None) -> int:
    """심평원 신고 의사 수를 갱신하고 변동을 기록한다 (홈페이지가 없는 병원용 보조 신호)."""
    from .hira import iter_hospitals

    conn = D.connect()
    if run_id is None:
        r = D.latest_run(conn)
        run_id = r["id"] if r else D.start_run(conn)
    n = 0
    for h in iter_hospitals(sido=sido, cl_codes=cl):
        if not h.get("ykiho"):
            continue
        row = conn.execute("SELECT id, dr_tot_cnt FROM hospitals WHERE ykiho=?", (h["ykiho"],)).fetchone()
        if not row:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO hira_counts (run_id, hospital_id, dr_tot_cnt, specialists) VALUES (?,?,?,?)",
            (run_id, row["id"], h.get("dr_tot_cnt"), None),
        )
        if h.get("dr_tot_cnt") is not None and row["dr_tot_cnt"] is not None and h["dr_tot_cnt"] != row["dr_tot_cnt"]:
            conn.execute(
                "INSERT INTO changes (run_id, hospital_id, kind, name, name_key, department, position, prev_position, detected_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, row["id"], "hira_count_changed", f"의사 수 {row['dr_tot_cnt']}→{h['dr_tot_cnt']}", "", None, str(h["dr_tot_cnt"]), str(row["dr_tot_cnt"]), D.now_iso()),
            )
            n += 1
        conn.execute("UPDATE hospitals SET dr_tot_cnt=?, updated_at=? WHERE id=?", (h.get("dr_tot_cnt"), D.now_iso(), row["id"]))
    conn.commit()
    conn.close()
    return n
