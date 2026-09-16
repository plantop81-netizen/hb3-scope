"""두 스냅샷(이전 성공 실행 vs 이번 실행)을 비교하여 변동을 계산하고, 병원 간 이동(이직)을 연결한다."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any

from .db import now_iso

MOVE_LOOKBACK_RUNS = 8  # 이직 매칭 시 과거 몇 회 실행까지 거슬러 볼지


def _index(rows: list[sqlite3.Row] | list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    idx: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        d = dict(r)
        idx[d["name_key"]].append(d)
    return idx


def diff_rosters(prev: list, cur: list) -> list[dict[str, Any]]:
    """병원 하나의 이전/현재 명단 비교 → [{kind, name, ...}]."""
    p, c = _index(prev), _index(cur)
    changes: list[dict[str, Any]] = []
    for key, cur_rows in c.items():
        prev_rows = p.get(key, [])
        if not prev_rows:
            for r in cur_rows:
                changes.append({"kind": "joined", **_pick(r)})
            continue
        # 동명이인 처리: 진료과 기준으로 짝을 맞춘다
        prev_by_dept = {(r.get("department") or ""): r for r in prev_rows}
        cur_by_dept = {(r.get("department") or ""): r for r in cur_rows}
        for dept, r in cur_by_dept.items():
            if dept in prev_by_dept:
                pr = prev_by_dept[dept]
                if (pr.get("position") or "") != (r.get("position") or "") and pr.get("position") and r.get("position"):
                    changes.append({"kind": "position_changed", **_pick(r), "prev_department": pr.get("department"), "prev_position": pr.get("position")})
            elif len(prev_rows) == 1 and len(cur_rows) == 1:
                pr = prev_rows[0]
                changes.append({"kind": "dept_changed", **_pick(r), "prev_department": pr.get("department"), "prev_position": pr.get("position")})
            elif len(cur_by_dept) > len(prev_by_dept):
                changes.append({"kind": "joined", **_pick(r)})
        for dept, pr in prev_by_dept.items():
            if dept not in cur_by_dept and not (len(prev_rows) == 1 and len(cur_rows) == 1) and len(prev_by_dept) > len(cur_by_dept):
                changes.append({"kind": "left", **_pick(pr)})
    for key, prev_rows in p.items():
        if key not in c:
            for r in prev_rows:
                changes.append({"kind": "left", **_pick(r)})
    return changes


def _pick(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": r["name"],
        "name_key": r["name_key"],
        "department": r.get("department"),
        "position": r.get("position"),
    }


def record_changes(conn: sqlite3.Connection, run_id: int, hospital_id: int, changes: list[dict[str, Any]]) -> int:
    ts = now_iso()
    n = 0
    for ch in changes:
        conn.execute(
            "INSERT INTO changes (run_id, hospital_id, kind, name, name_key, department, position, prev_department, prev_position, detected_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                hospital_id,
                ch["kind"],
                ch["name"],
                ch["name_key"],
                ch.get("department"),
                ch.get("position"),
                ch.get("prev_department"),
                ch.get("prev_position"),
                ts,
            ),
        )
        n += 1
    return n


def link_moves(conn: sqlite3.Connection, run_id: int) -> int:
    """같은 이름이 A 병원에서 'left', B 병원에서 'joined' 로 잡히면 이직으로 연결한다.

    홈페이지 갱신 시점이 병원마다 다르므로 최근 N 회 실행까지 거슬러 짝을 찾는다.
    진료과가 둘 다 있는데 다르면(예: 내과 ↔ 정형외과) 동명이인으로 보고 연결하지 않는다.
    """
    min_run = max(1, run_id - MOVE_LOOKBACK_RUNS)
    rows = conn.execute(
        "SELECT * FROM changes WHERE run_id BETWEEN ? AND ? AND kind IN ('joined','left') AND related_change_id IS NULL",
        (min_run, run_id),
    ).fetchall()
    joined = defaultdict(list)
    left = defaultdict(list)
    for r in rows:
        (joined if r["kind"] == "joined" else left)[r["name_key"]].append(r)
    linked = 0
    for key, lefts in left.items():
        for l in lefts:
            for j in joined.get(key, []):
                if j["related_change_id"] is not None or j["hospital_id"] == l["hospital_id"]:
                    continue
                if l["department"] and j["department"] and l["department"] != j["department"]:
                    continue
                # 이번 실행에서 잡힌 변동이 최소 한쪽에는 있어야 한다
                if l["run_id"] != run_id and j["run_id"] != run_id:
                    continue
                conn.execute("UPDATE changes SET related_hospital_id=?, related_change_id=? WHERE id=?", (j["hospital_id"], j["id"], l["id"]))
                conn.execute("UPDATE changes SET related_hospital_id=?, related_change_id=? WHERE id=?", (l["hospital_id"], l["id"], j["id"]))
                j = dict(j)
                j["related_change_id"] = l["id"]
                linked += 1
                break
    return linked


def mark_watchlist_hits(conn: sqlite3.Connection, run_id: int) -> int:
    """변동 인물이 관심(고객) 명단에 있으면 표시. 병원명/진료과가 있으면 함께 대조한다."""
    wl = conn.execute("SELECT w.*, h.id AS hid FROM watchlist w LEFT JOIN hospitals h ON h.name LIKE '%' || w.hospital_name || '%'").fetchall()
    by_key = defaultdict(list)
    for w in wl:
        by_key[w["name_key"]].append(w)
    hits = 0
    for ch in conn.execute("SELECT * FROM changes WHERE run_id=?", (run_id,)).fetchall():
        cands = by_key.get(ch["name_key"])
        if not cands:
            continue
        best = None
        for w in cands:
            score = 0
            if w["hospital_name"]:
                if w["hid"] in (ch["hospital_id"], ch["related_hospital_id"]):
                    score += 2
                else:
                    continue
            if w["department"] and ch["department"]:
                if w["department"].replace(" ", "") in (ch["department"] or "") or (ch["department"] or "") in w["department"]:
                    score += 1
                else:
                    continue
            if best is None or score > best[0]:
                best = (score, w)
        if best:
            conn.execute("UPDATE changes SET watchlist_id=? WHERE id=?", (best[1]["id"], ch["id"]))
            hits += 1
    return hits
