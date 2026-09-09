"""SQLite 저장소. 스키마는 최초 연결 시 자동 생성된다."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS hospitals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ykiho        TEXT UNIQUE,              -- 심평원 암호화 요양기호
    name         TEXT NOT NULL,
    cl_cd        TEXT,                     -- 종별코드 (01 상급종합, 05 종합병원, 11 병원 ...)
    cl_name      TEXT,
    sido         TEXT,
    sggu         TEXT,
    addr         TEXT,
    tel          TEXT,
    url          TEXT,                     -- 홈페이지
    dr_tot_cnt   INTEGER,                  -- 심평원 신고 의사 총수
    active       INTEGER NOT NULL DEFAULT 1,
    staff_urls   TEXT,                     -- JSON 배열: 의료진 페이지 URL (자동 탐색 또는 수동 지정)
    staff_urls_manual INTEGER NOT NULL DEFAULT 0,
    last_status  TEXT,
    last_ok_at   TEXT,
    notes        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hospitals_name ON hospitals(name);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',   -- running | done | failed
    week_key     TEXT,                              -- ISO 주차 (2026-W37)
    stats        TEXT                               -- JSON
);

CREATE TABLE IF NOT EXISTS pages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    hospital_id  INTEGER NOT NULL REFERENCES hospitals(id),
    url          TEXT NOT NULL,
    http_status  INTEGER,
    content_hash TEXT,
    text_len     INTEGER,
    extracted_by TEXT,                              -- llm | heuristic | cache | none
    error        TEXT,
    fetched_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pages_run ON pages(run_id, hospital_id);
CREATE INDEX IF NOT EXISTS idx_pages_hash ON pages(hospital_id, url, content_hash);

-- 페이지 해시별 추출 결과 캐시: 내용이 같으면 LLM 을 다시 호출하지 않는다.
CREATE TABLE IF NOT EXISTS extraction_cache (
    content_hash TEXT PRIMARY KEY,
    doctors      TEXT NOT NULL,        -- JSON 배열
    extracted_by TEXT NOT NULL,
    model        TEXT,
    created_at   TEXT NOT NULL
);

-- 실행(주차)별 병원 의료진 스냅샷
CREATE TABLE IF NOT EXISTS roster (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    hospital_id  INTEGER NOT NULL REFERENCES hospitals(id),
    name         TEXT NOT NULL,
    name_key     TEXT NOT NULL,        -- 정규화 이름
    department   TEXT,
    position     TEXT,
    specialty    TEXT,
    source_url   TEXT
);
CREATE INDEX IF NOT EXISTS idx_roster_run_h ON roster(run_id, hospital_id);
CREATE INDEX IF NOT EXISTS idx_roster_name ON roster(name_key);

-- 병원별 실행 결과 (성공한 스냅샷만 비교 대상)
CREATE TABLE IF NOT EXISTS hospital_runs (
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    hospital_id  INTEGER NOT NULL REFERENCES hospitals(id),
    ok           INTEGER NOT NULL,
    doctor_count INTEGER NOT NULL DEFAULT 0,
    pages_ok     INTEGER NOT NULL DEFAULT 0,
    pages_failed INTEGER NOT NULL DEFAULT 0,
    llm_calls    INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    PRIMARY KEY (run_id, hospital_id)
);

CREATE TABLE IF NOT EXISTS changes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER NOT NULL REFERENCES runs(id),
    hospital_id    INTEGER NOT NULL REFERENCES hospitals(id),
    kind           TEXT NOT NULL,       -- joined | left | dept_changed
    name           TEXT NOT NULL,
    name_key       TEXT NOT NULL,
    department     TEXT,
    position       TEXT,
    prev_department TEXT,
    prev_position  TEXT,
    related_hospital_id INTEGER REFERENCES hospitals(id),   -- 이직 추정 상대 병원
    related_change_id   INTEGER,
    watchlist_id   INTEGER,
    detected_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_run ON changes(run_id);
CREATE INDEX IF NOT EXISTS idx_changes_name ON changes(name_key);

-- 고객(관심) 의사 명단
CREATE TABLE IF NOT EXISTS watchlist (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    name_key     TEXT NOT NULL,
    hospital_name TEXT,
    department   TEXT,
    memo         TEXT,
    created_at   TEXT NOT NULL
);

-- 심평원 신고 인력 수 (홈페이지가 없는 병원용 보조 신호)
CREATE TABLE IF NOT EXISTS hira_counts (
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    hospital_id  INTEGER NOT NULL REFERENCES hospitals(id),
    dr_tot_cnt   INTEGER,
    specialists  TEXT,                  -- JSON {전문과목: 전문의수}
    PRIMARY KEY (run_id, hospital_id)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── 병원 ────────────────────────────────────────────────────────────────
def upsert_hospital(conn: sqlite3.Connection, h: dict[str, Any]) -> int:
    """ykiho 가 있으면 ykiho 기준, 없으면 (name, url) 기준으로 upsert."""
    ts = now_iso()
    row = None
    if h.get("ykiho"):
        row = conn.execute("SELECT id FROM hospitals WHERE ykiho=?", (h["ykiho"],)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id FROM hospitals WHERE name=? AND IFNULL(url,'')=IFNULL(?, '')",
            (h["name"], h.get("url")),
        ).fetchone()
    fields = ["ykiho", "name", "cl_cd", "cl_name", "sido", "sggu", "addr", "tel", "url", "dr_tot_cnt", "notes"]
    if row:
        sets = ", ".join(f"{f}=COALESCE(?, {f})" for f in fields)
        conn.execute(
            f"UPDATE hospitals SET {sets}, updated_at=? WHERE id=?",
            [h.get(f) for f in fields] + [ts, row["id"]],
        )
        if h.get("staff_urls"):
            conn.execute(
                "UPDATE hospitals SET staff_urls=?, staff_urls_manual=1 WHERE id=?",
                (json.dumps(h["staff_urls"], ensure_ascii=False), row["id"]),
            )
        return int(row["id"])
    cur = conn.execute(
        f"INSERT INTO hospitals ({', '.join(fields)}, staff_urls, staff_urls_manual, created_at, updated_at) "
        f"VALUES ({', '.join('?' for _ in fields)}, ?, ?, ?, ?)",
        [h.get(f) for f in fields]
        + [
            json.dumps(h["staff_urls"], ensure_ascii=False) if h.get("staff_urls") else None,
            1 if h.get("staff_urls") else 0,
            ts,
            ts,
        ],
    )
    return int(cur.lastrowid)


def list_hospitals(conn: sqlite3.Connection, active_only: bool = True, with_url_only: bool = False) -> list[sqlite3.Row]:
    q = "SELECT * FROM hospitals"
    conds = []
    if active_only:
        conds.append("active=1")
    if with_url_only:
        conds.append("url IS NOT NULL AND url<>''")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY sido, name"
    return conn.execute(q).fetchall()


def get_hospital(conn: sqlite3.Connection, hospital_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM hospitals WHERE id=?", (hospital_id,)).fetchone()


def set_staff_urls(conn: sqlite3.Connection, hospital_id: int, urls: list[str], manual: bool = False) -> None:
    conn.execute(
        "UPDATE hospitals SET staff_urls=?, staff_urls_manual=?, updated_at=? WHERE id=?",
        (json.dumps(urls, ensure_ascii=False), 1 if manual else 0, now_iso(), hospital_id),
    )


def hospital_staff_urls(row: sqlite3.Row) -> list[str]:
    if row["staff_urls"]:
        try:
            return list(json.loads(row["staff_urls"]))
        except json.JSONDecodeError:
            return []
    return []


# ── 실행 ────────────────────────────────────────────────────────────────
def start_run(conn: sqlite3.Connection) -> int:
    week = datetime.now().strftime("%G-W%V")
    cur = conn.execute("INSERT INTO runs (started_at, status, week_key) VALUES (?, 'running', ?)", (now_iso(), week))
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, stats: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, status=?, stats=? WHERE id=?",
        (now_iso(), status, json.dumps(stats, ensure_ascii=False), run_id),
    )
    conn.commit()


def latest_run(conn: sqlite3.Connection, status: str | None = "done") -> sqlite3.Row | None:
    if status:
        return conn.execute("SELECT * FROM runs WHERE status=? ORDER BY id DESC LIMIT 1", (status,)).fetchone()
    return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()


def list_runs(conn: sqlite3.Connection, limit: int = 30) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def previous_ok_run_for_hospital(conn: sqlite3.Connection, hospital_id: int, before_run_id: int) -> int | None:
    row = conn.execute(
        "SELECT run_id FROM hospital_runs WHERE hospital_id=? AND ok=1 AND run_id<? ORDER BY run_id DESC LIMIT 1",
        (hospital_id, before_run_id),
    ).fetchone()
    return int(row["run_id"]) if row else None


# ── 명단 ────────────────────────────────────────────────────────────────
def save_roster(conn: sqlite3.Connection, run_id: int, hospital_id: int, doctors: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM roster WHERE run_id=? AND hospital_id=?", (run_id, hospital_id))
    conn.executemany(
        "INSERT INTO roster (run_id, hospital_id, name, name_key, department, position, specialty, source_url) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                run_id,
                hospital_id,
                d["name"],
                d["name_key"],
                d.get("department"),
                d.get("position"),
                d.get("specialty"),
                d.get("source_url"),
            )
            for d in doctors
        ],
    )


def load_roster(conn: sqlite3.Connection, run_id: int, hospital_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM roster WHERE run_id=? AND hospital_id=? ORDER BY department, name",
        (run_id, hospital_id),
    ).fetchall()


def cache_get(conn: sqlite3.Connection, content_hash: str) -> tuple[list[dict[str, Any]], str] | None:
    row = conn.execute("SELECT doctors, extracted_by FROM extraction_cache WHERE content_hash=?", (content_hash,)).fetchone()
    if not row:
        return None
    return json.loads(row["doctors"]), row["extracted_by"]


def cache_put(conn: sqlite3.Connection, content_hash: str, doctors: list[dict[str, Any]], extracted_by: str, model: str | None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO extraction_cache (content_hash, doctors, extracted_by, model, created_at) VALUES (?,?,?,?,?)",
        (content_hash, json.dumps(doctors, ensure_ascii=False), extracted_by, model, now_iso()),
    )


# ── 관심 명단 ───────────────────────────────────────────────────────────
def list_watchlist(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM watchlist ORDER BY name").fetchall()


def add_watch(conn: sqlite3.Connection, name: str, hospital_name: str | None, department: str | None, memo: str | None) -> int:
    from .names import name_key

    cur = conn.execute(
        "INSERT INTO watchlist (name, name_key, hospital_name, department, memo, created_at) VALUES (?,?,?,?,?,?)",
        (name.strip(), name_key(name), (hospital_name or "").strip() or None, (department or "").strip() or None, memo, now_iso()),
    )
    return int(cur.lastrowid)


def stats_of(run: sqlite3.Row | None) -> dict[str, Any]:
    if not run or not run["stats"]:
        return {}
    try:
        return json.loads(run["stats"])
    except json.JSONDecodeError:
        return {}
