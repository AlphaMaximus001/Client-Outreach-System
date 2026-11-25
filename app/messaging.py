"""WhatsApp + email senders and the paced campaign dispatcher.

Each channel has its own queue processed strictly one message at a time, with a
random MIN_DELAY..MAX_DELAY second pause between messages (default 1-2s) so we
stay under provider rate limits. The two channels run in parallel threads.
"""
import json
import logging
import random
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from . import services
from .config import Config
from .db import Database

log = logging.getLogger("outreach")
MAX_ATTEMPTS = 3


class RateLimited(Exception):
    pass


# ---------- senders ----------

class WhatsAppSender:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def send(self, to: str, text: str, template: str = "", params: list[str] | None = None) -> str:
        to_digits = to.lstrip("+")
        if template:
            payload = {
                "messaging_product": "whatsapp",
                "to": to_digits,
                "type": "template",
                "template": {
                    "name": template,
                    "language": {"code": self.cfg.whatsapp_template_lang},
                    "components": [{
                        "type": "body",
                        "parameters": [{"type": "text", "text": p} for p in (params or [])],
                    }],
                },
            }
        else:
            payload = {"messaging_product": "whatsapp", "to": to_digits, "type": "text",
                       "text": {"body": text, "preview_url": False}}
        url = (f"https://graph.facebook.com/{self.cfg.whatsapp_api_version}/"
               f"{self.cfg.whatsapp_phone_number_id}/messages")
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.cfg.whatsapp_token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read() or b"{}")
                return (body.get("messages") or [{}])[0].get("id", "ok")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            if exc.code == 429 or '"code":130429' in detail or '"code":131056' in detail:
                raise RateLimited(detail) from exc
            raise RuntimeError(f"WhatsApp HTTP {exc.code}: {detail}") from exc


class EmailSender:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def send(self, to: str, subject: str, body: str) -> str:
        msg = EmailMessage()
        msg["From"] = self.cfg.email_from
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=30) as s:
                if self.cfg.smtp_use_tls:
                    s.starttls(context=ssl.create_default_context())
                if self.cfg.smtp_user:
                    s.login(self.cfg.smtp_user, self.cfg.smtp_password)
                s.send_message(msg)
        except smtplib.SMTPResponseException as exc:
            if exc.smtp_code in (421, 450, 451, 452):
                raise RateLimited(f"SMTP {exc.smtp_code}: {exc.smtp_error!r}") from exc
            raise
        return "ok"


# ---------- jobs ----------

@dataclass
class Job:
    message_id: int
    client: dict
    channel: str
    campaign: str
    to: str
    text: str = ""
    subject: str = ""
    template: str = ""
    params: tuple = ()


class Dispatcher:
    def __init__(self, db: Database, cfg: Config, whatsapp=None, email=None, sleep=time.sleep):
        self.db = db
        self.cfg = cfg
        self.whatsapp = whatsapp or WhatsAppSender(cfg)
        self.email = email or EmailSender(cfg)
        self.sleep = sleep
        self._run_lock = threading.Lock()
        self._count_lock = threading.Lock()
        self.status = {"running": False, "last_started": None, "last_finished": None, "last_result": None}

    def today(self) -> date:
        return datetime.now(ZoneInfo(self.cfg.timezone)).date()

    # --- building the queue ---

    def _queue(self, client: dict, channel: str, campaign: str, key: str, persist: bool = True) -> int | None:
        """Create (or reuse a retryable) message row. Returns id, or None if already done.
        With persist=False nothing is written (used for previews)."""
        with self.db.tx() as c:
            if persist:
                c.execute(
                    "INSERT OR IGNORE INTO messages(client_id, channel, campaign, campaign_key, status) "
                    "VALUES (?, ?, ?, ?, 'queued')", (client["id"], channel, campaign, key))
            row = c.execute(
                "SELECT id, status, attempts FROM messages WHERE client_id=? AND channel=? AND campaign_key=?",
                (client["id"], channel, key)).fetchone()
        if row is None:
            return -1
        if row["status"] in ("sent", "skipped") or row["attempts"] >= MAX_ATTEMPTS:
            return None
        return row["id"]

    def _jobs_for(self, client: dict, campaign: str, key: str, values: dict, s: dict,
                  persist: bool = True) -> list[Job]:
        jobs = []
        wa_template = (self.cfg.whatsapp_template_birthday if campaign == "birthday"
                       else self.cfg.whatsapp_template_festival)
        if client["phone"]:
            mid = self._queue(client, "whatsapp", campaign, key, persist)
            if mid:
                jobs.append(Job(mid, client, "whatsapp", campaign, client["phone"],
                                text=services.render(s[f"{campaign}_whatsapp"], **values),
                                template=wa_template, params=(values["first_name"], values["offer"])))
        if client["email"]:
            mid = self._queue(client, "email", campaign, key, persist)
            if mid:
                jobs.append(Job(mid, client, "email", campaign, client["email"],
                                subject=services.render(s[f"{campaign}_email_subject"], **values),
                                text=services.render(s[f"{campaign}_email_body"], **values)))
        return jobs

    def build_jobs(self, today: date, persist: bool = True) -> list[Job]:
        s = self.db.settings()
        jobs: list[Job] = []
        base = {"business": self.cfg.business_name}

        # Birthdays: send `birthday_days_before` days ahead (0 = on the day). Anything inside
        # the lead window that hasn't been sent yet is caught up; the per-year key prevents repeats.
        lead = int(s["birthday_days_before"])
        for c in services.upcoming_birthdays(self.db, today, lead):
            values = {**base, "name": c["name"], "first_name": c["name"].split()[0],
                      "age": c["turning"], "date": c["birthday"], "offer": s["birthday_offer"], "festival": ""}
            jobs += self._jobs_for(c, "birthday", f"birthday:{c['birthday'][:4]}", values, s, persist)

        # Festivals: everyone gets the offer `festival_days_before` days ahead
        flead = int(s["festival_days_before"])
        festivals = services.upcoming_festivals(self.db, today, flead)
        if festivals:
            clients = [services.with_age(c, today) for c in self.db.query("SELECT * FROM clients WHERE opted_out = 0")]
            for f in festivals:
                for c in clients:
                    values = {**base, "name": c["name"], "first_name": c["name"].split()[0], "age": c["age"],
                              "festival": f["name"], "date": f["next_date"], "offer": f["offer"]}
                    jobs += self._jobs_for(c, "festival", f"festival:{f['id']}:{f['next_date']}", values, s, persist)
        return jobs

    # --- sending ---

    def _send_one(self, job: Job) -> str:
        if self.cfg.dry_run:
            return f"dry-run: would send to {job.to}"
        if job.channel == "whatsapp":
            if not self.cfg.whatsapp_configured:
                raise RuntimeError("WhatsApp is not configured")
            return self.whatsapp.send(job.to, job.text, job.template, list(job.params))
        if not self.cfg.email_configured:
            raise RuntimeError("Email (SMTP) is not configured")
        return self.email.send(job.to, job.subject, job.text)

    def _mark(self, job: Job, status: str, detail: str) -> None:
        with self.db.tx() as c:
            c.execute(
                "UPDATE messages SET status=?, detail=?, attempts=attempts+1, "
                "sent_at=CASE WHEN ?='sent' THEN datetime('now') ELSE sent_at END WHERE id=?",
                (status, detail[:1000], status, job.message_id))

    def _pause(self, extra: float = 0) -> None:
        self.sleep(random.uniform(self.cfg.min_delay, self.cfg.max_delay) + extra)

    def _count(self, counts: dict, key: str) -> None:
        with self._count_lock:
            counts[key] += 1

    def _worker(self, jobs: list[Job], counts: dict) -> None:
        for i, job in enumerate(jobs):
            backoff = 5.0
            for attempt in range(3):
                try:
                    detail = self._send_one(job)
                    self._mark(job, "sent", detail)
                    self._count(counts, "sent")
                    break
                except RateLimited as exc:
                    log.warning("%s rate limited, backing off %.0fs: %s", job.channel, backoff, exc)
                    if attempt == 2:
                        self._mark(job, "failed", f"rate limited: {exc}")
                        self._count(counts, "failed")
                    else:
                        self._pause(backoff)
                        backoff *= 2
                except Exception as exc:  # noqa: BLE001 - record any provider error and move on
                    log.exception("send failed")
                    self._mark(job, "failed", str(exc))
                    self._count(counts, "failed")
                    break
            if i < len(jobs) - 1:
                self._pause()  # 1-2s between each message on this channel

    def run(self, today: date | None = None) -> dict:
        if not self._run_lock.acquire(blocking=False):
            return {"error": "a run is already in progress"}
        try:
            today = today or self.today()
            self.status.update(running=True, last_started=datetime.now().isoformat(timespec="seconds"))
            jobs = self.build_jobs(today)
            counts = {"sent": 0, "failed": 0}
            threads = [threading.Thread(target=self._worker, args=([j for j in jobs if j.channel == ch], counts))
                       for ch in ("whatsapp", "email")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            result = {"date": today.isoformat(), "queued": len(jobs), **counts, "dry_run": self.cfg.dry_run}
            self.status.update(last_result=result)
            return result
        finally:
            self.status.update(running=False, last_finished=datetime.now().isoformat(timespec="seconds"))
            self._run_lock.release()


# ---------- daily scheduler ----------

def start_scheduler(dispatcher: Dispatcher, stop: threading.Event) -> threading.Thread:
    """Checks every minute; runs once per day at/after SEND_HOUR (catches up after restarts)."""
    def loop():
        while not stop.is_set():
            try:
                now = datetime.now(ZoneInfo(dispatcher.cfg.timezone))
                today = now.date().isoformat()
                if now.hour >= dispatcher.cfg.send_hour and dispatcher.db.settings().get("last_auto_run") != today:
                    dispatcher.db.set_setting("last_auto_run", today)
                    log.info("daily run: %s", dispatcher.run(now.date()))
            except Exception:  # noqa: BLE001
                log.exception("scheduler error")
            stop.wait(60)

    t = threading.Thread(target=loop, name="scheduler", daemon=True)
    t.start()
    return t

