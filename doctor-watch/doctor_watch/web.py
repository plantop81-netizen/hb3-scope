"""웹 대시보드 (FastAPI). `python -m doctor_watch serve` 로 실행."""
from __future__ import annotations

import asyncio
import csv
import io
import json
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import db as D
from .names import name_key
from .report import KIND_LABEL, load_briefing

app = FastAPI(title="doctor-watch")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["KIND_LABEL"] = KIND_LABEL

_run_state: dict = {"running": False, "last_error": None}


def _ctx(request: Request, **kw):
    return {"request": request, "run_state": _run_state, **kw}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, run_id: int | None = None):
    with D.session() as conn:
        b = load_briefing(conn, run_id)
        runs = D.list_runs(conn, 20)
        wl_count = conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
        h_count = conn.execute("SELECT COUNT(*) FROM hospitals WHERE active=1").fetchone()[0]
    return templates.TemplateResponse(request, "index.html", _ctx(request, b=b, runs=runs, wl_count=wl_count, h_count=h_count))


@app.get("/hospitals", response_class=HTMLResponse)
def hospitals(request: Request, q: str = ""):
    with D.session() as conn:
        rows = conn.execute(
            """
            SELECT h.*, hr.doctor_count, hr.ok AS last_ok, hr.error AS last_error
            FROM hospitals h
            LEFT JOIN hospital_runs hr ON hr.hospital_id=h.id AND hr.run_id=(SELECT MAX(run_id) FROM hospital_runs WHERE hospital_id=h.id)
            WHERE h.active=1 AND (? = '' OR h.name LIKE '%' || ? || '%' OR IFNULL(h.sido,'') LIKE '%' || ? || '%')
            ORDER BY h.sido, h.name
            """,
            (q, q, q),
        ).fetchall()
    return templates.TemplateResponse(request, "hospitals.html", _ctx(request, rows=rows, q=q, staff_urls=D.hospital_staff_urls))


@app.get("/hospitals/{hospital_id}", response_class=HTMLResponse)
def hospital_detail(request: Request, hospital_id: int):
    with D.session() as conn:
        h = D.get_hospital(conn, hospital_id)
        if not h:
            return HTMLResponse("not found", status_code=404)
        last = conn.execute("SELECT MAX(run_id) AS r FROM hospital_runs WHERE hospital_id=? AND ok=1", (hospital_id,)).fetchone()["r"]
        roster = D.load_roster(conn, last, hospital_id) if last else []
        changes = conn.execute(
            "SELECT c.*, rh.name AS related_hospital_name, ru.week_key FROM changes c LEFT JOIN hospitals rh ON rh.id=c.related_hospital_id JOIN runs ru ON ru.id=c.run_id WHERE c.hospital_id=? ORDER BY c.id DESC LIMIT 200",
            (hospital_id,),
        ).fetchall()
        history = conn.execute(
            "SELECT hr.*, ru.week_key, ru.started_at FROM hospital_runs hr JOIN runs ru ON ru.id=hr.run_id WHERE hr.hospital_id=? ORDER BY hr.run_id DESC LIMIT 30",
            (hospital_id,),
        ).fetchall()
        pages = conn.execute("SELECT * FROM pages WHERE hospital_id=? AND run_id=(SELECT MAX(run_id) FROM pages WHERE hospital_id=?) ORDER BY id", (hospital_id, hospital_id)).fetchall()
    return templates.TemplateResponse(
        request, "hospital.html", _ctx(request, h=h, roster=roster, changes=changes, history=history, pages=pages, staff_urls=D.hospital_staff_urls(h))
    )


@app.post("/hospitals/{hospital_id}/staff-urls")
def set_staff_urls(hospital_id: int, urls: str = Form("")):
    lst = [u.strip() for u in urls.replace(",", "\n").splitlines() if u.strip()]
    with D.session() as conn:
        D.set_staff_urls(conn, hospital_id, lst, manual=bool(lst))
    return RedirectResponse(f"/hospitals/{hospital_id}", status_code=303)


@app.post("/hospitals/{hospital_id}/toggle")
def toggle_hospital(hospital_id: int):
    with D.session() as conn:
        conn.execute("UPDATE hospitals SET active = 1 - active WHERE id=?", (hospital_id,))
    return RedirectResponse(f"/hospitals/{hospital_id}", status_code=303)


@app.get("/doctors", response_class=HTMLResponse)
def doctors(request: Request, q: str = ""):
    rows, changes = [], []
    if q.strip():
        key = name_key(q)
        with D.session() as conn:
            rows = conn.execute(
                """
                SELECT r.*, h.name AS hospital_name, h.id AS hid, ru.week_key FROM roster r
                JOIN hospitals h ON h.id=r.hospital_id JOIN runs ru ON ru.id=r.run_id
                WHERE (r.name_key=? OR r.name LIKE '%' || ? || '%')
                  AND r.run_id = (SELECT MAX(run_id) FROM hospital_runs WHERE ok=1 AND hospital_id=r.hospital_id)
                ORDER BY h.name
                """,
                (key, q.strip()),
            ).fetchall()
            changes = conn.execute(
                "SELECT c.*, h.name AS hospital_name, rh.name AS related_hospital_name FROM changes c JOIN hospitals h ON h.id=c.hospital_id LEFT JOIN hospitals rh ON rh.id=c.related_hospital_id WHERE c.name_key=? OR c.name LIKE '%' || ? || '%' ORDER BY c.id DESC LIMIT 100",
                (key, q.strip()),
            ).fetchall()
    return templates.TemplateResponse(request, "doctors.html", _ctx(request, q=q, rows=rows, changes=changes))


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist(request: Request):
    with D.session() as conn:
        rows = D.list_watchlist(conn)
        hits = conn.execute(
            "SELECT c.*, h.name AS hospital_name, rh.name AS related_hospital_name, ru.week_key FROM changes c JOIN hospitals h ON h.id=c.hospital_id LEFT JOIN hospitals rh ON rh.id=c.related_hospital_id JOIN runs ru ON ru.id=c.run_id WHERE c.watchlist_id IS NOT NULL ORDER BY c.id DESC LIMIT 100"
        ).fetchall()
    return templates.TemplateResponse(request, "watchlist.html", _ctx(request, rows=rows, hits=hits))


@app.post("/watchlist/add")
def watchlist_add(name: str = Form(...), hospital: str = Form(""), department: str = Form(""), memo: str = Form("")):
    with D.session() as conn:
        D.add_watch(conn, name, hospital, department, memo)
    return RedirectResponse("/watchlist", status_code=303)


@app.post("/watchlist/import")
async def watchlist_import(file: UploadFile):
    data = (await file.read()).decode("utf-8-sig", errors="replace")
    with D.session() as conn:
        for row in csv.DictReader(io.StringIO(data)):
            if row.get("name"):
                D.add_watch(conn, row["name"], row.get("hospital") or row.get("hospital_name"), row.get("department"), row.get("memo"))
    return RedirectResponse("/watchlist", status_code=303)


@app.post("/watchlist/{wid}/delete")
def watchlist_delete(wid: int):
    with D.session() as conn:
        conn.execute("DELETE FROM watchlist WHERE id=?", (wid,))
    return RedirectResponse("/watchlist", status_code=303)


@app.get("/runs", response_class=HTMLResponse)
def runs(request: Request):
    with D.session() as conn:
        rows = D.list_runs(conn, 100)
    return templates.TemplateResponse(request, "runs.html", _ctx(request, rows=[dict(r, stats=D.stats_of(r)) for r in rows]))


async def _run_bg(no_llm: bool):
    from .pipeline import run_collection
    from .report import write_reports

    _run_state["running"] = True
    _run_state["last_error"] = None
    try:
        run_id = await run_collection(prefer_llm=not no_llm)
        with D.session() as conn:
            write_reports(conn, run_id)
    except Exception as e:  # noqa: BLE001
        _run_state["last_error"] = f"{type(e).__name__}: {e}"
    finally:
        _run_state["running"] = False


@app.post("/run")
async def trigger_run(background: BackgroundTasks, no_llm: str = Form("")):
    if not _run_state["running"]:
        asyncio.get_event_loop().create_task(_run_bg(bool(no_llm)))
    return RedirectResponse("/", status_code=303)


# ── JSON API ──
@app.get("/api/briefing")
def api_briefing(run_id: int | None = None):
    with D.session() as conn:
        b = load_briefing(conn, run_id)
    return JSONResponse(json.loads(json.dumps(b, ensure_ascii=False, default=str)))


@app.get("/api/hospitals/{hospital_id}/roster")
def api_roster(hospital_id: int):
    with D.session() as conn:
        last = conn.execute("SELECT MAX(run_id) AS r FROM hospital_runs WHERE hospital_id=? AND ok=1", (hospital_id,)).fetchone()["r"]
        rows = D.load_roster(conn, last, hospital_id) if last else []
    return [dict(r) for r in rows]


@app.get("/api/status")
def api_status():
    return _run_state
