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
    """병원 하나의 이전/현재 명단 비교 → [{kind, name, ...}].

    같은 병원 안에서 같은 이름은 한 사람으로 본다(대학병원은 한 교수가 진료과·센터 페이지 여러 곳에 실린다).
    - 이전에 없던 이름 → joined, 현재 없는 이름 → left
    - 둘 다 있는데 진료과 집합이 전혀 겹치지 않으면 → dept_changed
    - 직위가 (하나로 확정되고) 바뀌면 → position_changed
    """
    p, c = _index(prev), _index(cur)
    changes: list[dict[str, Any]] = []

    def depts(rows: list[dict[str, Any]]) -> list[str]:
        seen: list[str] = []
        for r in rows:
            d = r.get("department")
            if d and d not in seen:
                seen.append(d)
        return seen

    def positions(rows: list[dict[str, Any]]) -> set[str]:
        return {r["position"] for r in rows if r.get("position")}

    for key, cur_rows in c.items():
        prev_rows = p.get(key)
        rep = _pick(cur_rows[0])
        rep["department"] = " / ".join(depts(cur_rows)) or None
        if not prev_rows:
            changes.append({"kind": "joined", **rep})
            continue
        pd, cd = depts(prev_rows), depts(cur_rows)
        if pd and cd and not (set(pd) & set(cd)):
            changes.append({"kind": "dept_changed", **rep, "prev_department": " / ".join(pd), "prev_position": (prev_rows[0].get("position"))})
            continue
        pp, cp = positions(prev_rows), positions(cur_rows)
        if len(pp) == 1 and len(cp) == 1 and pp != cp:
            changes.append({"kind": "position_changed", **rep, "position": next(iter(cp)), "prev_department": " / ".join(pd) or None, "prev_position": next(iter(pp))})
    for key, prev_rows in p.items():
        if key not in c:
            rep = _pick(prev_rows[0])
            rep["department"] = " / ".join(depts(prev_rows)) or None
            changes.append({"kind": "left", **rep})
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
                if l["department"] and j["department"]:
                    ld = {x.strip() for x in l["department"].split("/")}
                    jd = {x.strip() for x in j["department"].split("/")}
                    if not (ld & jd):
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
