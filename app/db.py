"""SQLite storage."""
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    dob         TEXT NOT NULL,              -- YYYY-MM-DD (age is derived from this)
    address     TEXT DEFAULT '',
    phone       TEXT DEFAULT '',
    email       TEXT DEFAULT '',
    opted_out   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_clients_phone ON clients(phone);
CREATE INDEX IF NOT EXISTS idx_clients_email ON clients(email);

CREATE TABLE IF NOT EXISTS festivals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    date       TEXT NOT NULL,               -- YYYY-MM-DD
    recurring  INTEGER NOT NULL DEFAULT 1,  -- 1 = same date every year
    offer      TEXT NOT NULL DEFAULT '',
    active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id     INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    channel       TEXT NOT NULL,            -- whatsapp | email
    campaign      TEXT NOT NULL,            -- birthday | festival
    campaign_key  TEXT NOT NULL,            -- e.g. birthday:2026 / festival:3:2026-12-25
    status        TEXT NOT NULL,            -- queued | sent | failed | skipped
    attempts      INTEGER NOT NULL DEFAULT 0,
    detail        TEXT DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at       TEXT,
    UNIQUE (client_id, channel, campaign_key)
);
"""

DEFAULT_SETTINGS = {
    "dashboard_window_days": "30",
    "birthday_days_before": "0",
    "festival_days_before": "1",
    "birthday_offer": "20% off anything in store this week",
    "birthday_whatsapp": "Happy birthday {first_name}! 🎉 From all of us at {business}, here's a gift: {offer}. Valid this week.",
    "birthday_email_subject": "Happy Birthday {first_name}! A gift from {business} 🎁",
    "birthday_email_body": "Hi {first_name},\n\nHappy birthday! To celebrate, we have a special offer just for you:\n\n{offer}\n\nWarm wishes,\n{business}",
    "festival_whatsapp": "Happy {festival}, {first_name}! 🎊 {business} has a special offer for you: {offer}",
    "festival_email_subject": "{festival} offer from {business}",
    "festival_email_body": "Hi {first_name},\n\nWishing you a joyful {festival}! Here's our special offer:\n\n{offer}\n\nBest,\n{business}",
    "last_auto_run": "",
}

DEFAULT_FESTIVALS = [
    ("New Year's Day", "2027-01-01", 1, "15% off to start the year right"),
    ("Valentine's Day", "2027-02-14", 1, "Buy one, get one 50% off"),
    ("Christmas", "2026-12-25", 1, "25% off storewide"),
]


class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            self._conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        if not self._conn.execute("SELECT 1 FROM festivals LIMIT 1").fetchone():
            self._conn.executemany(
                "INSERT INTO festivals(name, date, recurring, offer) VALUES (?, ?, ?, ?)", DEFAULT_FESTIVALS
            )
        self._conn.commit()

    @contextmanager
    def tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def query(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def settings(self) -> dict:
        return {r["key"]: r["value"] for r in self.query("SELECT key, value FROM settings")}

    def set_setting(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
