"""Configuration loaded from environment variables (and an optional .env file)."""
import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    database_path: str
    timezone: str
    business_name: str
    dashboard_user: str
    dashboard_password: str
    send_hour: int
    min_delay: float
    max_delay: float
    default_country_code: str
    csv_date_order: str
    whatsapp_token: str
    whatsapp_phone_number_id: str
    whatsapp_api_version: str
    whatsapp_template_birthday: str
    whatsapp_template_festival: str
    whatsapp_template_lang: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_use_tls: bool
    email_from: str
    dry_run: bool

    @property
    def whatsapp_configured(self) -> bool:
        return bool(self.whatsapp_token and self.whatsapp_phone_number_id)

    @property
    def email_configured(self) -> bool:
        return bool(self.smtp_host and self.email_from)


def load_config() -> Config:
    _load_dotenv()
    e = os.getenv
    return Config(
        database_path=e("DATABASE_PATH", "outreach.db"),
        timezone=e("TIMEZONE", "UTC"),
        business_name=e("BUSINESS_NAME", "Our Store"),
        dashboard_user=e("DASHBOARD_USER", ""),
        dashboard_password=e("DASHBOARD_PASSWORD", ""),
        send_hour=int(e("SEND_HOUR", "9")),
        min_delay=float(e("MIN_DELAY_SECONDS", "1")),
        max_delay=float(e("MAX_DELAY_SECONDS", "2")),
        default_country_code=e("DEFAULT_COUNTRY_CODE", "").lstrip("+"),
        csv_date_order=e("CSV_DATE_ORDER", "DMY").upper(),
        whatsapp_token=e("WHATSAPP_TOKEN", ""),
        whatsapp_phone_number_id=e("WHATSAPP_PHONE_NUMBER_ID", ""),
        whatsapp_api_version=e("WHATSAPP_API_VERSION", "v21.0"),
        whatsapp_template_birthday=e("WHATSAPP_TEMPLATE_BIRTHDAY", ""),
        whatsapp_template_festival=e("WHATSAPP_TEMPLATE_FESTIVAL", ""),
        whatsapp_template_lang=e("WHATSAPP_TEMPLATE_LANG", "en"),
        smtp_host=e("SMTP_HOST", ""),
        smtp_port=int(e("SMTP_PORT", "587")),
        smtp_user=e("SMTP_USER", ""),
        smtp_password=e("SMTP_PASSWORD", ""),
        smtp_use_tls=_bool("SMTP_USE_TLS", True),
        email_from=e("EMAIL_FROM", ""),
        dry_run=_bool("DRY_RUN", True),
    )
