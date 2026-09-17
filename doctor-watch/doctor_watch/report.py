"""주간 브리핑 생성 (Markdown / HTML / 텍스트 요약)."""
from __future__ import annotations

import html
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from . import db as D
from .config import settings

KIND_LABEL = {
    "joined": "신규 합류",
    "left": "명단 제외",
    "dept_changed": "진료과 변경",
    "position_changed": "직위 변경",
    "hira_count_changed": "심평원 의사 수 변동",
}
KIND_ORDER = ["left", "joined", "dept_changed", "position_changed", "hira_count_changed"]


def load_briefing(conn: sqlite3.Connection, run_id: int | None = None) -> dict[str, Any]:
    run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone() if run_id else D.latest_run(conn)
    if not run:
        return {"run": None, "changes": [], "moves": [], "watch_hits": [], "failed": [], "stats": {}, "by_hospital": {}}
    rows = conn.execute(
        """
        SELECT c.*, h.name AS hospital_name, h.sido, h.cl_name, h.url AS hospital_url,
               rh.name AS related_hospital_name, w.memo AS watch_memo, w.hospital_name AS watch_hospital
        FROM changes c
        JOIN hospitals h ON h.id=c.hospital_id
        LEFT JOIN hospitals rh ON rh.id=c.related_hospital_id
        LEFT JOIN watchlist w ON w.id=c.watchlist_id
        WHERE c.run_id=?
        ORDER BY (c.watchlist_id IS NULL), c.kind, h.sido, h.name, c.department, c.name
        """,
        (run["id"],),
    ).fetchall()
    changes = [dict(r) for r in rows]
    moves = [c for c in changes if c["kind"] == "left" and c["related_hospital_id"]]
    watch_hits = [c for c in changes if c["watchlist_id"]]
    failed = [
        dict(r)
        for r in conn.execute(
            "SELECT hr.*, h.name AS hospital_name, h.url FROM hospital_runs hr JOIN hospitals h ON h.id=hr.hospital_id WHERE hr.run_id=? AND (hr.ok=0 OR hr.error IS NOT NULL) ORDER BY h.name",
            (run["id"],),
        ).fetchall()
    ]
    by_hospital: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in changes:
        by_hospital[c["hospital_name"]].append(c)
    return {
        "run": dict(run),
        "stats": D.stats_of(run),
        "changes": changes,
        "moves": moves,
        "watch_hits": watch_hits,
        "failed": failed,
        "by_hospital": dict(by_hospital),
    }


def _who(c: dict[str, Any]) -> str:
    parts = [c["name"]]
    if c.get("department"):
        parts.append(c["department"])
    if c.get("position"):
        parts.append(c["position"])
    return " · ".join(parts)


def summary_text(b: dict[str, Any]) -> str:
    """슬랙/텔레그램용 짧은 텍스트."""
    run = b["run"]
    if not run:
        return "doctor-watch: 아직 완료된 실행이 없습니다."
    s = b["stats"]
    date = run["finished_at"][:10] if run["finished_at"] else run["started_at"][:10]
    lines = [f"🩺 의료진 변동 주간 브리핑 ({date}, {run['week_key']})"]
    lines.append(f"병원 {s.get('hospitals', 0)}곳 중 {s.get('ok', 0)}곳 수집 성공 · 의사 {s.get('doctors', 0)}명 · 변동 {s.get('changes', 0)}건 · 이직 추정 {s.get('moves', 0)}건")
    if s.get("llm_disabled"):
        lines.append(f"🚨 Claude 추출 중단: {s['llm_disabled']}")
    if b["watch_hits"]:
        lines.append("")
        lines.append(f"⭐ 고객 명단 변동 {len(b['watch_hits'])}건")
        for c in b["watch_hits"][:20]:
            extra = ""
            if c.get("related_hospital_name"):
                extra = f" → {c['related_hospital_name']}" if c["kind"] == "left" else f" ← {c['related_hospital_name']}"
            lines.append(f"  - [{KIND_LABEL.get(c['kind'], c['kind'])}] {c['hospital_name']} {_who(c)}{extra}")
    if b["moves"]:
        lines.append("")
        lines.append(f"🔁 이직 추정 {len(b['moves'])}건")
        for c in b["moves"][:20]:
            lines.append(f"  - {c['name']} ({c.get('department') or '-'}): {c['hospital_name']} → {c['related_hospital_name']}")
    counts = defaultdict(int)
    for c in b["changes"]:
        counts[c["kind"]] += 1
    if counts:
        lines.append("")
        lines.append("📋 유형별: " + ", ".join(f"{KIND_LABEL.get(k, k)} {counts[k]}" for k in KIND_ORDER if counts.get(k)))
    top = sorted(b["by_hospital"].items(), key=lambda kv: -len(kv[1]))[:10]
    for hname, chs in top:
        lines.append(f"• {hname} ({len(chs)}건)")
        for c in chs[:6]:
            lines.append(f"    {KIND_LABEL.get(c['kind'], c['kind'])}: {_who(c)}")
        if len(chs) > 6:
            lines.append(f"    … 외 {len(chs) - 6}건")
    if b["failed"]:
        lines.append("")
        lines.append(f"⚠️ 수집 실패/검토 필요 {len(b['failed'])}곳")
    return "\n".join(lines)


def markdown_report(b: dict[str, Any]) -> str:
    run = b["run"]
    if not run:
        return "# 의료진 변동 브리핑\n\n완료된 실행이 없습니다.\n"
    s = b["stats"]
    date = (run["finished_at"] or run["started_at"])[:10]
    out = [f"# 의료진 변동 주간 브리핑 — {date} ({run['week_key']})", ""]
    out.append(f"- 수집 병원: {s.get('hospitals', 0)}곳 (성공 {s.get('ok', 0)}, 실패 {s.get('failed', 0)})")
    out.append(f"- 확인된 의사: {s.get('doctors', 0)}명")
    out.append(f"- 변동: {s.get('changes', 0)}건 · 이직 추정 {s.get('moves', 0)}건 · 고객 명단 해당 {s.get('watchlist_hits', 0)}건")
    if s.get("llm_disabled"):
        out.append(f"- 🚨 **Claude 추출 중단: {s['llm_disabled']}** — 이번 주 명단은 규칙 기반 추출이라 정확도가 낮습니다. 해결 후 다음 실행에서 자동으로 다시 추출됩니다.")
    if s.get("llm_budget_exceeded"):
        out.append(f"- ⚠️ Claude 호출 상한({s['llm_budget_exceeded']}회) 초과: 일부 페이지는 규칙 기반으로 추출되어 정확도가 낮을 수 있음")
    if s.get("heuristic_pages"):
        out.append(f"- 규칙 기반으로 임시 추출된 페이지 {s['heuristic_pages']}개 (다음 실행에서 Claude 로 재추출)")
    out.append("")
    if b["watch_hits"]:
        out += ["## ⭐ 고객 명단 변동", "", "| 유형 | 병원 | 이름 | 진료과 | 직위 | 이동 병원 | 메모 |", "|---|---|---|---|---|---|---|"]
        for c in b["watch_hits"]:
            out.append(f"| {KIND_LABEL.get(c['kind'], c['kind'])} | {c['hospital_name']} | {c['name']} | {c.get('department') or ''} | {c.get('position') or ''} | {c.get('related_hospital_name') or ''} | {c.get('watch_memo') or ''} |")
        out.append("")
    if b["moves"]:
        out += ["## 🔁 이직 추정 (A 병원 제외 + B 병원 합류)", "", "| 이름 | 진료과 | 이전 병원 | 이동 병원 |", "|---|---|---|---|"]
        for c in b["moves"]:
            out.append(f"| {c['name']} | {c.get('department') or ''} | {c['hospital_name']} | {c['related_hospital_name']} |")
        out.append("")
    out += ["## 병원별 변동", ""]
    if not b["by_hospital"]:
        out.append("이번 주 변동 없음.")
    for hname, chs in sorted(b["by_hospital"].items(), key=lambda kv: (-len(kv[1]), kv[0])):
        out.append(f"### {hname} ({len(chs)}건)")
        out.append("")
        for c in chs:
            extra = ""
            if c["kind"] == "dept_changed":
                extra = f" (이전: {c.get('prev_department') or '-'})"
            elif c["kind"] == "position_changed":
                extra = f" (이전: {c.get('prev_position') or '-'})"
            elif c.get("related_hospital_name"):
                extra = f" → {c['related_hospital_name']}" if c["kind"] == "left" else f" ← {c['related_hospital_name']}"
            star = "⭐ " if c.get("watchlist_id") else ""
            out.append(f"- {star}**{KIND_LABEL.get(c['kind'], c['kind'])}** {_who(c)}{extra}")
        out.append("")
    if b["failed"]:
        out += ["## ⚠️ 수집 실패 / 검토 필요", ""]
        for f in b["failed"]:
            out.append(f"- {f['hospital_name']}: {f.get('error') or '알 수 없는 오류'}")
        out.append("")
    out.append(f"_생성: {datetime.now().strftime('%Y-%m-%d %H:%M')} · doctor-watch_")
    return "\n".join(out)


def html_report(b: dict[str, Any]) -> str:
    """이메일/정적 페이지용 단독 HTML."""
    run = b["run"]
    e = html.escape
    date = (run["finished_at"] or run["started_at"])[:10] if run else "-"
    s = b.get("stats", {})
    css = """
    body{font-family:-apple-system,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#111;background:#fff}
    h1{font-size:22px} h2{font-size:17px;margin-top:28px;border-bottom:1px solid #ddd;padding-bottom:6px} h3{font-size:15px;margin:18px 0 6px}
    .kpi{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}.kpi div{background:#f4f6f8;border-radius:8px;padding:10px 14px;min-width:120px}
    .kpi b{display:block;font-size:20px} table{border-collapse:collapse;width:100%;font-size:14px} th,td{border-bottom:1px solid #e5e7eb;padding:6px 8px;text-align:left}
    .tag{display:inline-block;border-radius:4px;padding:1px 6px;font-size:12px;margin-right:6px;color:#fff}
    .joined{background:#1a8f5a}.left{background:#c0392b}.dept_changed{background:#b7791f}.position_changed{background:#4b5563}.hira_count_changed{background:#2563eb}
    .star{color:#d97706} .muted{color:#6b7280;font-size:12px} ul{padding-left:18px} li{margin:3px 0}
    nav{display:flex;gap:16px;align-items:center;padding:12px 0;border-bottom:1px solid #e5e7eb;margin-bottom:18px;flex-wrap:wrap}
    nav .brand{font-weight:700;font-size:16px;color:#111;text-decoration:none}nav a{color:#0d5c69;text-decoration:none}nav a.on{font-weight:700}
    """
    parts = [f"<meta charset='utf-8'><title>의료진 변동 브리핑 {e(date)}</title><style>{css}</style>"]
    parts.append(f"<h1>🩺 의료진 변동 주간 브리핑 <span class='muted'>{e(date)} · {e(run['week_key'] if run else '')}</span></h1>")
    if not run:
        parts.append("<p>완료된 실행이 없습니다.</p>")
        return "\n".join(parts)
    if s.get("llm_disabled"):
        parts.append(f"<p style='background:#fde8e8;border:1px solid #f5b5b5;padding:10px;border-radius:6px'>🚨 <b>Claude 추출 중단: {e(s['llm_disabled'])}</b> — 이번 주 명단은 규칙 기반 추출이라 정확도가 낮습니다. 해결 후 다음 실행에서 자동으로 다시 추출됩니다.</p>")
    elif s.get("heuristic_pages"):
        parts.append(f"<p class='muted'>규칙 기반으로 임시 추출된 페이지 {s['heuristic_pages']}개 (다음 실행에서 Claude 로 재추출)</p>")
    parts.append(
        "<div class='kpi'>"
        f"<div><b>{s.get('hospitals', 0)}</b>수집 병원 <span class='muted'>(성공 {s.get('ok', 0)})</span></div>"
        f"<div><b>{s.get('doctors', 0)}</b>확인된 의사</div>"
        f"<div><b>{s.get('changes', 0)}</b>변동</div>"
        f"<div><b>{s.get('moves', 0)}</b>이직 추정</div>"
        f"<div><b>{s.get('watchlist_hits', 0)}</b>고객 명단 해당</div>"
        "</div>"
    )

    def tag(kind: str) -> str:
        return f"<span class='tag {e(kind)}'>{e(KIND_LABEL.get(kind, kind))}</span>"

    if b["watch_hits"]:
        parts.append("<h2>⭐ 고객 명단 변동</h2><table><tr><th>유형</th><th>병원</th><th>이름</th><th>진료과</th><th>직위</th><th>이동 병원</th><th>메모</th></tr>")
        for c in b["watch_hits"]:
            parts.append(f"<tr><td>{tag(c['kind'])}</td><td>{e(c['hospital_name'])}</td><td><b>{e(c['name'])}</b></td><td>{e(c.get('department') or '')}</td><td>{e(c.get('position') or '')}</td><td>{e(c.get('related_hospital_name') or '')}</td><td>{e(c.get('watch_memo') or '')}</td></tr>")
        parts.append("</table>")
    if b["moves"]:
        parts.append("<h2>🔁 이직 추정</h2><table><tr><th>이름</th><th>진료과</th><th>이전 병원</th><th>이동 병원</th></tr>")
        for c in b["moves"]:
            parts.append(f"<tr><td><b>{e(c['name'])}</b></td><td>{e(c.get('department') or '')}</td><td>{e(c['hospital_name'])}</td><td>{e(c['related_hospital_name'])}</td></tr>")
        parts.append("</table>")
    parts.append("<h2>병원별 변동</h2>")
    if not b["by_hospital"]:
        parts.append("<p>이번 주 변동 없음.</p>")
    for hname, chs in sorted(b["by_hospital"].items(), key=lambda kv: (-len(kv[1]), kv[0])):
        parts.append(f"<h3>{e(hname)} <span class='muted'>{len(chs)}건</span></h3><ul>")
        for c in chs:
            extra = ""
            if c["kind"] == "dept_changed":
                extra = f" <span class='muted'>(이전: {e(c.get('prev_department') or '-')})</span>"
            elif c["kind"] == "position_changed":
                extra = f" <span class='muted'>(이전: {e(c.get('prev_position') or '-')})</span>"
            elif c.get("related_hospital_name"):
                extra = f" → {e(c['related_hospital_name'])}" if c["kind"] == "left" else f" ← {e(c['related_hospital_name'])}"
            star = "<span class='star'>⭐</span> " if c.get("watchlist_id") else ""
            parts.append(f"<li>{star}{tag(c['kind'])}{e(_who(c))}{extra}</li>")
        parts.append("</ul>")
    if b["failed"]:
        parts.append("<h2>⚠️ 수집 실패 / 검토 필요</h2><ul>")
        for f in b["failed"]:
            parts.append(f"<li>{e(f['hospital_name'])}: <span class='muted'>{e(f.get('error') or '')}</span></li>")
        parts.append("</ul>")
    parts.append(f"<p class='muted'>생성 {e(datetime.now().strftime('%Y-%m-%d %H:%M'))} · doctor-watch</p>")
    return "\n".join(parts)


def write_reports(conn: sqlite3.Connection, run_id: int | None = None, out_dir: Path | None = None) -> dict[str, Path]:
    b = load_briefing(conn, run_id)
    out_dir = out_dir or settings.reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    date = ((b["run"] or {}).get("finished_at") or (b["run"] or {}).get("started_at") or datetime.now().isoformat())[:10]
    md = markdown_report(b)
    ht = html_report(b)
    files = {
        "md": out_dir / f"{date}.md",
        "html": out_dir / f"{date}.html",
        "latest_md": out_dir / "latest.md",
        "latest_html": out_dir / "index.html",
        "json": out_dir / f"{date}.json",
    }
    files["md"].write_text(md, encoding="utf-8")
    files["html"].write_text(ht, encoding="utf-8")
    files["latest_md"].write_text(md, encoding="utf-8")
    # 최신 브리핑은 정적 사이트의 첫 화면: 공통 내비게이션을 붙인다
    files["latest_html"].write_text(ht.replace("<h1>", _nav("index.html") + "<h1>", 1), encoding="utf-8")
    write_site(conn, out_dir)
    files["json"].write_text(json.dumps({k: v for k, v in b.items() if k != "by_hospital"}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return files


# ── 정적 대시보드 (GitHub Pages 등 정적 호스팅용) ─────────────────────────
SITE_CSS = """
body{font-family:-apple-system,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;max-width:1100px;margin:0 auto;padding:0 16px 40px;color:#111;background:#fff}
nav{display:flex;gap:16px;align-items:center;padding:12px 0;border-bottom:1px solid #e5e7eb;margin-bottom:18px;flex-wrap:wrap}
nav .brand{font-weight:700;font-size:16px;color:#111;text-decoration:none}nav a{color:#0d5c69;text-decoration:none}nav a.on{font-weight:700}
h1{font-size:22px} h2{font-size:17px;margin-top:28px;border-bottom:1px solid #ddd;padding-bottom:6px} h3{font-size:15px;margin:18px 0 6px}
.kpi{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}.kpi div{background:#f4f6f8;border-radius:8px;padding:10px 14px;min-width:120px}.kpi b{display:block;font-size:20px}
table{border-collapse:collapse;width:100%;font-size:14px} th,td{border-bottom:1px solid #e5e7eb;padding:6px 8px;text-align:left;vertical-align:top} th{color:#6b7280;font-size:12px}
.tag{display:inline-block;border-radius:4px;padding:1px 6px;font-size:12px;margin-right:6px;color:#fff}
.joined{background:#1a8f5a}.left{background:#c0392b}.dept_changed{background:#b7791f}.position_changed{background:#4b5563}.hira_count_changed{background:#2563eb}
.ok{color:#1a8f5a}.err{color:#c0392b}.muted{color:#6b7280;font-size:12px} ul{padding-left:18px} li{margin:3px 0} a{color:#0d5c69}
input[type=text]{width:100%;max-width:480px;padding:8px 10px;border:1px solid #d1d5db;border-radius:6px;font:inherit}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}@media(max-width:800px){.grid{grid-template-columns:1fr}}
"""


def _nav(active: str) -> str:
    items = [("index.html", "브리핑"), ("hospitals.html", "병원"), ("search.html", "의사 검색"), ("runs.html", "실행 이력")]
    links = "".join(f"<a href='{href}' class='{'on' if href == active else ''}'>{label}</a>" for href, label in items)
    return f"<nav><a class='brand' href='index.html'>🩺 doctor-watch</a>{links}</nav>"


def _page(title: str, active: str, body: str) -> str:
    return f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title><style>{SITE_CSS}</style></head><body>{_nav(active)}{body}<p class='muted'>생성 {datetime.now().strftime('%Y-%m-%d %H:%M')} · doctor-watch</p></body></html>"


def write_site(conn: sqlite3.Connection, out_dir: Path | None = None) -> list[Path]:
    """병원 목록 / 병원별 명단·이력 / 의사 검색 / 실행 이력 정적 페이지 생성."""
    e = html.escape
    out_dir = out_dir or settings.reports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    hospitals = conn.execute(
        """
        SELECT h.*, hr.ok AS last_ok, hr.doctor_count, hr.error AS last_error, hr.run_id AS last_run
        FROM hospitals h
        LEFT JOIN hospital_runs hr ON hr.hospital_id=h.id AND hr.run_id=(SELECT MAX(run_id) FROM hospital_runs WHERE hospital_id=h.id)
        WHERE h.active=1 ORDER BY h.sido, h.name
        """
    ).fetchall()

    # 병원 목록
    rows = []
    for h in hospitals:
        status = "<span class='ok'>정상</span>" if h["last_ok"] else (f"<span class='err'>{e((h['last_error'] or '')[:60])}</span>" if h["last_error"] else "<span class='muted'>미수집</span>")
        rows.append(
            f"<tr><td><a href='hospital-{h['id']}.html'>{e(h['name'])}</a></td><td>{e(h['sido'] or '')}</td><td>{e(h['cl_name'] or '')}</td>"
            f"<td>{('<a href=%s target=_blank rel=noopener>%s</a>' % (e(h['url']), e(h['url'].replace('https://', '').replace('http://', '')[:40]))) if h['url'] else ''}</td>"
            f"<td>{h['doctor_count'] if h['doctor_count'] is not None else ''}</td><td>{status}</td></tr>"
        )
    body = f"<h1>병원 <span class='muted'>{len(hospitals)}곳</span></h1><table><tr><th>병원</th><th>시도</th><th>종별</th><th>홈페이지</th><th>의사 수</th><th>최근 상태</th></tr>{''.join(rows)}</table>"
    p = out_dir / "hospitals.html"
    p.write_text(_page("병원 · doctor-watch", "hospitals.html", body), encoding="utf-8")
    written.append(p)

    # 병원별 페이지 + 검색용 데이터
    search_rows: list[dict[str, Any]] = []
    for h in hospitals:
        last = conn.execute("SELECT MAX(run_id) AS r FROM hospital_runs WHERE hospital_id=? AND ok=1", (h["id"],)).fetchone()["r"]
        roster = D.load_roster(conn, last, h["id"]) if last else []
        by_dept: dict[str, list] = defaultdict(list)
        for d in roster:
            by_dept[d["department"] or "(진료과 미표기)"].append(d)
            search_rows.append({"n": d["name"], "h": h["name"], "hid": h["id"], "d": d["department"] or "", "p": d["position"] or ""})
        changes = conn.execute(
            "SELECT c.*, rh.name AS related_hospital_name, ru.week_key FROM changes c LEFT JOIN hospitals rh ON rh.id=c.related_hospital_id JOIN runs ru ON ru.id=c.run_id WHERE c.hospital_id=? ORDER BY c.id DESC LIMIT 300",
            (h["id"],),
        ).fetchall()
        history = conn.execute(
            "SELECT hr.*, ru.week_key, ru.started_at FROM hospital_runs hr JOIN runs ru ON ru.id=hr.run_id WHERE hr.hospital_id=? ORDER BY hr.run_id DESC LIMIT 30",
            (h["id"],),
        ).fetchall()
        staff_urls = D.hospital_staff_urls(h)
        parts = [f"<h1>{e(h['name'])} <span class='muted'>{e(h['sido'] or '')} {e(h['cl_name'] or '')}</span></h1>"]
        parts.append(f"<p>{('<a href=%s target=_blank rel=noopener>%s</a>' % (e(h['url']), e(h['url']))) if h['url'] else ''} <span class='muted'>{e(h['addr'] or '')}{(' · 심평원 신고 의사 %d명' % h['dr_tot_cnt']) if h['dr_tot_cnt'] else ''}</span></p>")
        if staff_urls:
            parts.append("<p class='muted'>의료진 페이지: " + " · ".join(f"<a href='{e(u)}' target='_blank' rel='noopener'>{e(u[:70])}</a>" for u in staff_urls[:6]) + (f" 외 {len(staff_urls) - 6}" if len(staff_urls) > 6 else "") + "</p>")
        parts.append("<div class='grid'><div>")
        parts.append(f"<h2>현재 의료진 <span class='muted'>{len({d['name_key'] for d in roster})}명 · {len(by_dept)}개 진료과</span></h2>")
        for dept, ds in sorted(by_dept.items()):
            parts.append(f"<h3>{e(dept)} <span class='muted'>{len(ds)}</span></h3><ul>" + "".join(f"<li><b>{e(d['name'])}</b> {e(d['position'] or '')} <span class='muted'>{e((d['specialty'] or '')[:60])}</span></li>" for d in ds) + "</ul>")
        parts.append("</div><div>")
        parts.append(f"<h2>변동 이력 <span class='muted'>{len(changes)}건</span></h2>")
        if changes:
            parts.append("<table><tr><th>주차</th><th>유형</th><th>이름</th><th>진료과</th><th>비고</th></tr>")
            for c in changes:
                note = ""
                if c["related_hospital_name"]:
                    note = ("→ " if c["kind"] == "left" else "← ") + e(c["related_hospital_name"])
                elif c["prev_department"] and c["kind"] == "dept_changed":
                    note = "이전 " + e(c["prev_department"])
                elif c["prev_position"] and c["kind"] == "position_changed":
                    note = "이전 " + e(c["prev_position"])
                parts.append(f"<tr><td>{e(c['week_key'])}</td><td><span class='tag {e(c['kind'])}'>{e(KIND_LABEL.get(c['kind'], c['kind']))}</span></td><td>{e(c['name'])}</td><td>{e(c['department'] or '')}</td><td>{note}</td></tr>")
            parts.append("</table>")
        else:
            parts.append("<p class='muted'>기록된 변동 없음</p>")
        parts.append("<h2>수집 이력</h2><table><tr><th>주차</th><th>결과</th><th>의사</th><th>페이지</th><th>LLM</th></tr>")
        for x in history:
            res = "<span class='ok'>성공</span>" if x["ok"] else "<span class='err'>실패</span>"
            parts.append(f"<tr><td>{e(x['week_key'] or '')}</td><td>{res} <span class='muted'>{e((x['error'] or '')[:70])}</span></td><td>{x['doctor_count']}</td><td>{x['pages_ok']}/{x['pages_ok'] + x['pages_failed']}</td><td>{x['llm_calls']}</td></tr>")
        parts.append("</table></div></div>")
        p = out_dir / f"hospital-{h['id']}.html"
        p.write_text(_page(f"{h['name']} · doctor-watch", "hospitals.html", "".join(parts)), encoding="utf-8")
        written.append(p)

    # 의사 검색 (클라이언트 측)
    change_rows = [
        {"n": r["name"], "h": r["hospital_name"], "hid": r["hospital_id"], "k": KIND_LABEL.get(r["kind"], r["kind"]), "d": r["department"] or "", "w": r["week_key"], "r": r["related_hospital_name"] or ""}
        for r in conn.execute(
            "SELECT c.name, c.kind, c.department, c.hospital_id, h.name AS hospital_name, rh.name AS related_hospital_name, ru.week_key FROM changes c JOIN hospitals h ON h.id=c.hospital_id LEFT JOIN hospitals rh ON rh.id=c.related_hospital_id JOIN runs ru ON ru.id=c.run_id ORDER BY c.id DESC LIMIT 5000"
        ).fetchall()
    ]
    data_json = json.dumps({"roster": search_rows, "changes": change_rows}, ensure_ascii=False).replace("</", "<\\/")
    body = f"""
<h1>의사 검색</h1>
<p><input type='text' id='q' placeholder='이름 또는 병원·진료과 (예: 김민수, 소화기내과)' autofocus> <span class='muted' id='cnt'></span></p>
<h2>현재 소속</h2><table><tr><th>이름</th><th>병원</th><th>진료과</th><th>직위</th></tr><tbody id='r'></tbody></table>
<h2>변동 이력</h2><table><tr><th>주차</th><th>유형</th><th>이름</th><th>병원</th><th>진료과</th><th>이동</th></tr><tbody id='c'></tbody></table>
<script>
const DATA={data_json};
const esc=s=>String(s).replace(/[&<>"']/g,m=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[m]));
function run(){{
  const q=document.getElementById('q').value.trim().replace(/\\s+/g,'');
  const R=document.getElementById('r'),C=document.getElementById('c');
  if(!q){{R.innerHTML='';C.innerHTML='';document.getElementById('cnt').textContent='';return;}}
  const m=DATA.roster.filter(x=>(x.n+x.h+x.d).replace(/\\s+/g,'').includes(q)).slice(0,300);
  R.innerHTML=m.map(x=>`<tr><td><b>${{esc(x.n)}}</b></td><td><a href='hospital-${{x.hid}}.html'>${{esc(x.h)}}</a></td><td>${{esc(x.d)}}</td><td>${{esc(x.p)}}</td></tr>`).join('');
  const c=DATA.changes.filter(x=>(x.n+x.h+x.d).replace(/\\s+/g,'').includes(q)).slice(0,300);
  C.innerHTML=c.map(x=>`<tr><td>${{esc(x.w)}}</td><td>${{esc(x.k)}}</td><td>${{esc(x.n)}}</td><td><a href='hospital-${{x.hid}}.html'>${{esc(x.h)}}</a></td><td>${{esc(x.d)}}</td><td>${{esc(x.r)}}</td></tr>`).join('');
  document.getElementById('cnt').textContent=`${{m.length}}명 · 변동 ${{c.length}}건`;
}}
document.getElementById('q').addEventListener('input',run);
const u=new URLSearchParams(location.search).get('q'); if(u){{document.getElementById('q').value=u;run();}}
</script>"""
    p = out_dir / "search.html"
    p.write_text(_page("의사 검색 · doctor-watch", "search.html", body), encoding="utf-8")
    written.append(p)

    # 실행 이력
    rows = []
    for r in D.list_runs(conn, 100):
        s = D.stats_of(r)
        rows.append(f"<tr><td>{r['id']}</td><td>{e(r['week_key'] or '')}</td><td>{e(r['started_at'][:16])}</td><td>{e((r['finished_at'] or '')[:16])}</td><td>{e(r['status'])}</td><td>{s.get('hospitals', '')} ({s.get('ok', '')}/{s.get('failed', '')})</td><td>{s.get('doctors', '')}</td><td>{s.get('changes', '')}</td><td>{s.get('moves', '')}</td><td>{s.get('llm_calls', '')}</td></tr>")
    body = "<h1>실행 이력</h1><table><tr><th>#</th><th>주차</th><th>시작</th><th>종료</th><th>상태</th><th>병원(성공/실패)</th><th>의사</th><th>변동</th><th>이직</th><th>LLM</th></tr>" + "".join(rows) + "</table>"
    p = out_dir / "runs.html"
    p.write_text(_page("실행 이력 · doctor-watch", "runs.html", body), encoding="utf-8")
    written.append(p)
    return written
