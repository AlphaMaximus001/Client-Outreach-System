"""Business logic: client validation, CSV import, upcoming birthdays/festivals, message text."""
import csv
import io
import re
from datetime import date, datetime

from .config import Config
from .db import Database

# ---------- parsing / normalisation ----------

CSV_ALIASES = {
    "name": ["name", "full_name", "fullname", "client_name", "customer_name"],
    "dob": ["dob", "date_of_birth", "dateofbirth", "birthday", "birth_date", "birthdate"],
    "address": ["address", "home_address", "location"],
    "phone": ["phone", "phone_number", "mobile", "whatsapp", "whatsapp_number", "telephone"],
    "email": ["email", "email_address", "e-mail", "mail"],
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def parse_date(value: str, order: str = "DMY") -> date:
    value = (value or "").strip()
    if not value:
        raise ValueError("date of birth is required")
    formats = ["%Y-%m-%d", "%Y/%m/%d"]
    if order == "MDY":
        formats += ["%m/%d/%Y", "%m-%d-%Y", "%m.%d.%Y"]
    else:
        formats += ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"]
    formats += ["%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y"]
    for fmt in formats:
        try:
            d = datetime.strptime(value, fmt).date()
        except ValueError:
            continue
        if d > date.today():
            raise ValueError(f"date of birth {value!r} is in the future")
        return d
    raise ValueError(f"unrecognised date {value!r}")


def normalize_phone(phone: str, default_cc: str = "") -> str:
    """Return E.164-style digits with leading '+', or '' if empty."""
    phone = (phone or "").strip()
    if not phone:
        return ""
    has_plus = phone.startswith("+")
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return ""
    if has_plus:
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0"):
        if not default_cc:
            raise ValueError(f"phone {phone!r} needs a country code (or set DEFAULT_COUNTRY_CODE)")
        return "+" + default_cc + digits[1:]
    if default_cc and not digits.startswith(default_cc):
        return "+" + default_cc + digits
    return "+" + digits


def clean_client(data: dict, cfg: Config) -> dict:
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    dob = parse_date(str(data.get("dob") or ""), cfg.csv_date_order)
    email = (data.get("email") or "").strip().lower()
    if email and not EMAIL_RE.match(email):
        raise ValueError(f"invalid email {email!r}")
    phone = normalize_phone(str(data.get("phone") or ""), cfg.default_country_code)
    if not email and not phone:
        raise ValueError("a phone number or email is required")
    return {
        "name": name,
        "dob": dob.isoformat(),
        "address": (data.get("address") or "").strip(),
        "phone": phone,
        "email": email,
    }


def age_on(dob: date, on: date) -> int:
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def with_age(client: dict, today: date) -> dict:
    client = dict(client)
    client["age"] = age_on(date.fromisoformat(client["dob"]), today)
    return client


# ---------- clients ----------

def find_existing(db: Database, phone: str, email: str):
    if email:
        row = db.one("SELECT id FROM clients WHERE email = ?", (email,))
        if row:
            return row["id"]
    if phone:
        row = db.one("SELECT id FROM clients WHERE phone = ?", (phone,))
        if row:
            return row["id"]
    return None


def upsert_client(db: Database, c: dict) -> tuple[int, bool]:
    """Insert or update (matched on email, then phone). Returns (id, created)."""
    existing = find_existing(db, c["phone"], c["email"])
    with db.tx() as conn:
        if existing:
            conn.execute(
                "UPDATE clients SET name=?, dob=?, address=?, phone=?, email=? WHERE id=?",
                (c["name"], c["dob"], c["address"], c["phone"], c["email"], existing),
            )
            return existing, False
        cur = conn.execute(
            "INSERT INTO clients(name, dob, address, phone, email) VALUES (?, ?, ?, ?, ?)",
            (c["name"], c["dob"], c["address"], c["phone"], c["email"]),
        )
        return cur.lastrowid, True


def import_csv(db: Database, cfg: Config, raw: bytes) -> dict:
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV file is empty")
    header_map = {}
    for h in reader.fieldnames:
        key = (h or "").strip().lower().replace(" ", "_")
        for field, aliases in CSV_ALIASES.items():
            if key in aliases:
                header_map[h] = field
    missing = {"name", "dob"} - set(header_map.values())
    if missing:
        raise ValueError(f"CSV is missing required column(s): {', '.join(sorted(missing))}")

    created = updated = 0
    errors = []
    for i, row in enumerate(reader, start=2):  # row 1 is the header
        mapped = {header_map[k]: v for k, v in row.items() if k in header_map}
        if not any((v or "").strip() for v in mapped.values()):
            continue
        try:
            _, is_new = upsert_client(db, clean_client(mapped, cfg))
            created += is_new
            updated += not is_new
        except ValueError as exc:
            errors.append({"row": i, "error": str(exc)})
    return {"created": created, "updated": updated, "errors": errors}


# ---------- upcoming events ----------

def next_birthday(dob: date, today: date) -> date:
    for year in (today.year, today.year + 1):
        try:
            d = dob.replace(year=year)
        except ValueError:  # 29 Feb in a non-leap year -> 28 Feb
            d = date(year, 2, 28)
        if d >= today:
            return d
    raise AssertionError("unreachable")


def upcoming_birthdays(db: Database, today: date, window_days: int) -> list[dict]:
    out = []
    for c in db.query("SELECT * FROM clients WHERE opted_out = 0"):
        dob = date.fromisoformat(c["dob"])
        nb = next_birthday(dob, today)
        days = (nb - today).days
        if days <= window_days:
            c = with_age(c, today)
            c.update(birthday=nb.isoformat(), days_until=days, turning=age_on(dob, nb))
            out.append(c)
    return sorted(out, key=lambda c: (c["days_until"], c["name"]))


def next_festival_date(f: dict, today: date):
    d = date.fromisoformat(f["date"])
    if not f["recurring"]:
        return d if d >= today else None
    return next_birthday(d, today)  # same "next annual occurrence" logic


def upcoming_festivals(db: Database, today: date, window_days: int) -> list[dict]:
    out = []
    for f in db.query("SELECT * FROM festivals WHERE active = 1"):
        nd = next_festival_date(f, today)
        if nd and (nd - today).days <= window_days:
            f.update(next_date=nd.isoformat(), days_until=(nd - today).days)
            out.append(f)
    return sorted(out, key=lambda f: f["days_until"])


# ---------- message text ----------

class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(template: str, **values) -> str:
    return template.format_map(_SafeDict(values))
