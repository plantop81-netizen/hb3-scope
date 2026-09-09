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
    """
    parts = [f"<meta charset='utf-8'><title>의료진 변동 브리핑 {e(date)}</title><style>{css}</style>"]
    parts.append(f"<h1>🩺 의료진 변동 주간 브리핑 <span class='muted'>{e(date)} · {e(run['week_key'] if run else '')}</span></h1>")
    if not run:
        parts.append("<p>완료된 실행이 없습니다.</p>")
        return "\n".join(parts)
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
    files["latest_html"].write_text(ht, encoding="utf-8")
    files["json"].write_text(json.dumps({k: v for k, v in b.items() if k != "by_hospital"}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return files
