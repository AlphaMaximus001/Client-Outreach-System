"""FastAPI app: dashboard + JSON API. Run with: uvicorn app.main:app"""
import logging
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from . import services
from .config import load_config
from .db import DEFAULT_SETTINGS, Database
from .messaging import Dispatcher, start_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

cfg = load_config()
db = Database(cfg.database_path)
dispatcher = Dispatcher(db, cfg)
_stop = threading.Event()
security = HTTPBasic(auto_error=False)


def auth(request: Request, creds: HTTPBasicCredentials | None = Depends(security)):
    if not cfg.dashboard_password or request.url.path == "/healthz":
        return
    ok = creds and secrets.compare_digest(creds.username, cfg.dashboard_user) \
        and secrets.compare_digest(creds.password, cfg.dashboard_password)
    if not ok:
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})


@asynccontextmanager
async def lifespan(_app):
    start_scheduler(dispatcher, _stop)
    yield
    _stop.set()


app = FastAPI(title="Client Outreach System", lifespan=lifespan, dependencies=[Depends(auth)])
STATIC = Path(__file__).parent / "static"


class ClientIn(BaseModel):
    name: str
    dob: str
    address: str = ""
    phone: str = ""
    email: str = ""


class FestivalIn(BaseModel):
    name: str
    date: str
    recurring: bool = True
    offer: str = ""
    active: bool = True


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Unauthenticated health check for the hosting platform."""
    db.one("SELECT 1 AS ok")
    return {"ok": True}


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC / "index.html")


# ----- overview -----

@app.get("/api/overview")
def overview():
    today = dispatcher.today()
    window = int(db.settings()["dashboard_window_days"])
    return {
        "today": today.isoformat(),
        "window_days": window,
        "total_clients": db.one("SELECT COUNT(*) n FROM clients")["n"],
        "birthdays": services.upcoming_birthdays(db, today, window),
        "festivals": services.upcoming_festivals(db, today, window),
        "messages_today": db.query(
            "SELECT channel, status, COUNT(*) n FROM messages WHERE date(created_at)=date('now') GROUP BY 1,2"),
        "run_status": dispatcher.status,
        "channels": {
            "dry_run": cfg.dry_run,
            "whatsapp": cfg.whatsapp_configured,
            "email": cfg.email_configured,
            "send_hour": cfg.send_hour,
            "timezone": cfg.timezone,
        },
    }


# ----- clients -----

@app.get("/api/clients")
def list_clients(q: str = ""):
    today = dispatcher.today()
    like = f"%{q.strip()}%"
    rows = db.query("SELECT * FROM clients WHERE name LIKE ? OR email LIKE ? OR phone LIKE ? ORDER BY name",
                    (like, like, like))
    return [services.with_age(r, today) for r in rows]


@app.post("/api/clients", status_code=201)
def add_client(body: ClientIn):
    try:
        cid, created = services.upsert_client(db, services.clean_client(body.model_dump(), cfg))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"id": cid, "created": created}


@app.put("/api/clients/{cid}")
def update_client(cid: int, body: ClientIn):
    try:
        c = services.clean_client(body.model_dump(), cfg)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    with db.tx() as conn:
        n = conn.execute("UPDATE clients SET name=?, dob=?, address=?, phone=?, email=? WHERE id=?",
                         (c["name"], c["dob"], c["address"], c["phone"], c["email"], cid)).rowcount
    if not n:
        raise HTTPException(404, "client not found")
    return {"id": cid}


@app.post("/api/clients/{cid}/opt-out")
def opt_out(cid: int, opted_out: bool = True):
    with db.tx() as conn:
        conn.execute("UPDATE clients SET opted_out=? WHERE id=?", (int(opted_out), cid))
    return {"id": cid, "opted_out": opted_out}


@app.delete("/api/clients/{cid}")
def delete_client(cid: int):
    with db.tx() as conn:
        conn.execute("DELETE FROM clients WHERE id=?", (cid,))
    return {"deleted": cid}


@app.post("/api/clients/upload")
async def upload_csv(file: UploadFile):
    raw = await file.read()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "CSV too large (max 10 MB)")
    try:
        return services.import_csv(db, cfg, raw)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/clients/template.csv", include_in_schema=False)
def csv_template():
    return FileResponse(STATIC / "clients_template.csv", filename="clients_template.csv")


# ----- festivals -----

@app.get("/api/festivals")
def list_festivals():
    return db.query("SELECT * FROM festivals ORDER BY substr(date, 6)")


@app.post("/api/festivals", status_code=201)
def add_festival(body: FestivalIn):
    _check_date(body.date)
    with db.tx() as conn:
        cur = conn.execute("INSERT INTO festivals(name, date, recurring, offer, active) VALUES (?,?,?,?,?)",
                           (body.name.strip(), body.date, int(body.recurring), body.offer, int(body.active)))
    return {"id": cur.lastrowid}


@app.put("/api/festivals/{fid}")
def update_festival(fid: int, body: FestivalIn):
    _check_date(body.date)
    with db.tx() as conn:
        conn.execute("UPDATE festivals SET name=?, date=?, recurring=?, offer=?, active=? WHERE id=?",
                     (body.name.strip(), body.date, int(body.recurring), body.offer, int(body.active), fid))
    return {"id": fid}


@app.delete("/api/festivals/{fid}")
def delete_festival(fid: int):
    with db.tx() as conn:
        conn.execute("DELETE FROM festivals WHERE id=?", (fid,))
    return {"deleted": fid}


def _check_date(value: str):
    from datetime import date
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, "date must be YYYY-MM-DD") from exc


# ----- settings -----

@app.get("/api/settings")
def get_settings():
    s = db.settings()
    s.pop("last_auto_run", None)
    return s


@app.put("/api/settings")
def put_settings(body: dict[str, str]):
    for k, v in body.items():
        if k not in DEFAULT_SETTINGS or k == "last_auto_run":
            raise HTTPException(422, f"unknown setting {k}")
        if k.endswith("_days") or k.endswith("_before"):
            if not str(v).isdigit():
                raise HTTPException(422, f"{k} must be a whole number")
        db.set_setting(k, v)
    return get_settings()


# ----- sending -----

@app.get("/api/campaigns/preview")
def preview():
    """What a run would send right now (does not send or write anything)."""
    jobs = dispatcher.build_jobs(dispatcher.today(), persist=False)
    return [{"client": j.client["name"], "channel": j.channel, "campaign": j.campaign,
             "to": j.to, "subject": j.subject, "text": j.text} for j in jobs]


@app.post("/api/campaigns/run", status_code=202)
def run_now(background: BackgroundTasks):
    if dispatcher.status["running"]:
        raise HTTPException(409, "a run is already in progress")
    background.add_task(dispatcher.run)
    return {"started": True}


@app.get("/api/messages")
def messages(limit: int = 200, status: str = ""):
    sql = ("SELECT m.*, c.name AS client_name FROM messages m JOIN clients c ON c.id = m.client_id "
           + ("WHERE m.status = ? " if status else "") + "ORDER BY m.id DESC LIMIT ?")
    return db.query(sql, ((status, limit) if status else (limit,)))
