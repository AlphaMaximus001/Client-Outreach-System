from dataclasses import replace
from datetime import date

import pytest

from app import services
from app.config import load_config
from app.db import Database
from app.messaging import Dispatcher, RateLimited


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no .env
    c = load_config()
    return replace(c, database_path=str(tmp_path / "t.db"), default_country_code="234",
                   csv_date_order="DMY", dry_run=False, whatsapp_token="t", whatsapp_phone_number_id="1",
                   smtp_host="smtp", email_from="shop@example.com", min_delay=1, max_delay=2)


@pytest.fixture
def db(cfg):
    d = Database(cfg.database_path)
    with d.tx() as c:
        c.execute("DELETE FROM festivals")
    return d


class FakeSender:
    def __init__(self, fail_first=0):
        self.sent, self.fail_first = [], fail_first

    def send(self, to, *args, **kw):
        if self.fail_first:
            self.fail_first -= 1
            raise RateLimited("slow down")
        self.sent.append((to, args))
        return "ok"


def test_phone_normalisation():
    assert services.normalize_phone("0803 123 4567", "234") == "+2348031234567"
    assert services.normalize_phone("+44 7700 900123", "234") == "+447700900123"
    assert services.normalize_phone("", "234") == ""
    with pytest.raises(ValueError, match="country code"):
        services.normalize_phone("08031234567", "")


def test_age_and_leap_birthday():
    assert services.age_on(date(1990, 12, 1), date(2026, 11, 30)) == 35
    assert services.age_on(date(1990, 12, 1), date(2026, 12, 1)) == 36
    assert services.next_birthday(date(2000, 2, 29), date(2027, 1, 1)) == date(2027, 2, 28)
    assert services.next_birthday(date(2000, 2, 29), date(2028, 1, 1)) == date(2028, 2, 29)
    assert services.next_birthday(date(2000, 1, 5), date(2026, 11, 25)) == date(2027, 1, 5)


def test_csv_import_upserts_and_reports_errors(db, cfg):
    csv = (b"Name,Date of Birth,Address,Phone Number,Email\n"
           b"Ada Obi,01/12/1990,Lagos,08031234567,ADA@example.com\n"
           b"No Dob,,Lagos,0803,\n"
           b"Bad Email,1990-01-01,,,not-an-email\n")
    r = services.import_csv(db, cfg, csv)
    assert r["created"] == 1 and len(r["errors"]) == 2
    c = db.one("SELECT * FROM clients")
    assert c["dob"] == "1990-12-01" and c["email"] == "ada@example.com" and c["phone"] == "+2348031234567"
    # re-import same person with new address -> update, not duplicate
    r = services.import_csv(db, cfg, b"name,dob,address,phone,email\nAda Obi,1990-12-01,Abuja,,ada@example.com\n")
    assert r == {"created": 0, "updated": 1, "errors": []}
    assert db.one("SELECT address FROM clients")["address"] == "Abuja"


def test_csv_missing_columns(db, cfg):
    with pytest.raises(ValueError, match="dob"):
        services.import_csv(db, cfg, b"name,email\nA,a@b.co\n")


def test_birthday_run_paces_messages_and_is_idempotent(db, cfg):
    today = date(2026, 11, 25)
    for i in range(3):
        services.upsert_client(db, services.clean_client(
            {"name": f"Client {i}", "dob": "1990-11-25", "phone": f"080300000{i}", "email": f"c{i}@x.com"}, cfg))
    services.upsert_client(db, services.clean_client({"name": "Later", "dob": "1990-12-20", "email": "l@x.com"}, cfg))

    sleeps, wa, em = [], FakeSender(), FakeSender()
    d = Dispatcher(db, cfg, whatsapp=wa, email=em, sleep=sleeps.append)
    result = d.run(today)

    assert result["queued"] == 6 and result["sent"] == 6 and result["failed"] == 0
    assert len(wa.sent) == 3 and len(em.sent) == 3
    assert wa.sent[0][1][0].startswith("Happy birthday Client!")
    # 2 pauses per channel (between 3 messages), each 1-2s
    assert len(sleeps) == 4 and all(1 <= s <= 2 for s in sleeps)

    # Running again the same day sends nothing new
    assert d.run(today)["queued"] == 0


def test_rate_limit_backoff_then_success(db, cfg):
    services.upsert_client(db, services.clean_client({"name": "A", "dob": "1990-11-25", "phone": "0803"}, cfg))
    sleeps, wa = [], FakeSender(fail_first=1)
    d = Dispatcher(db, cfg, whatsapp=wa, email=FakeSender(), sleep=sleeps.append)
    assert d.run(date(2026, 11, 25))["sent"] == 1
    assert sleeps and sleeps[0] >= 5  # backed off before retrying


def test_festival_goes_to_everyone_n_days_before(db, cfg):
    with db.tx() as c:
        c.execute("INSERT INTO festivals(name, date, recurring, offer) VALUES ('Christmas','2020-12-25',1,'25% off')")
    db.set_setting("festival_days_before", "2")
    for i in range(2):
        services.upsert_client(db, services.clean_client({"name": f"P{i}", "dob": "1980-06-01", "email": f"p{i}@x.com"}, cfg))
    em = FakeSender()
    d = Dispatcher(db, cfg, whatsapp=FakeSender(), email=em, sleep=lambda s: None)
    assert d.run(date(2026, 12, 22))["queued"] == 0   # 3 days before: not yet
    assert d.run(date(2026, 12, 23))["sent"] == 2     # 2 days before: send
    subject = em.sent[0][1][0]
    assert "Christmas" in subject


def test_opted_out_clients_are_skipped(db, cfg):
    cid, _ = services.upsert_client(db, services.clean_client({"name": "A", "dob": "1990-11-25", "email": "a@x.com"}, cfg))
    with db.tx() as c:
        c.execute("UPDATE clients SET opted_out=1 WHERE id=?", (cid,))
    d = Dispatcher(db, cfg, whatsapp=FakeSender(), email=FakeSender(), sleep=lambda s: None)
    assert d.run(date(2026, 11, 25))["queued"] == 0


def test_api_smoke(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    import importlib
    from fastapi.testclient import TestClient
    import app.main as m
    importlib.reload(m)
    client = TestClient(m.app)
    assert client.get("/api/overview").status_code == 401
    client.auth = ("admin", "pw")
    assert client.post("/api/clients", json={"name": "Ada", "dob": "1990-01-01", "email": "a@x.com"}).status_code == 201
    assert client.post("/api/clients", json={"name": "X", "dob": "1990-01-01"}).status_code == 422
    r = client.post("/api/clients/upload", files={"file": ("c.csv", b"name,dob,email\nBo,1991-02-02,b@x.com\n", "text/csv")})
    assert r.json()["created"] == 1
    clients = client.get("/api/clients").json()
    assert len(clients) == 2 and all("age" in c for c in clients)
    assert client.get("/").status_code == 200
    assert client.get("/api/campaigns/preview").status_code == 200
