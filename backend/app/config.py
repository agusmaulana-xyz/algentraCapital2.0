from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    app_secret_key: SecretStr = Field(validation_alias="APP_SECRET_KEY")
    admin_username: str = Field(default="admin", validation_alias="ADMIN_USERNAME")
    admin_password: SecretStr = Field(validation_alias="ADMIN_PASSWORD")
    app_env: str = Field(default="development", validation_alias="APP_ENV")
    cookie_secure: bool = Field(default=True, validation_alias="COOKIE_SECURE")
    gemini_api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-3.8-flash", validation_alias="GEMINI_MODEL")
    gemini_cs_api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_CS_API_KEY")
    gemini_cs_model: str = Field(default="gemini-3.8-flash", validation_alias="GEMINI_CS_MODEL")
    gemini_timeout_seconds: float = Field(default=25.0, validation_alias="GEMINI_TIMEOUT_SECONDS", gt=0, le=120)
    gemini_cs_timeout_seconds: float = Field(default=60.0, validation_alias="GEMINI_CS_TIMEOUT_SECONDS", ge=15, le=180)
    gemini_retry_attempts: int = Field(default=3, validation_alias="GEMINI_RETRY_ATTEMPTS", ge=1, le=6)
    enable_regex_fallback: bool = Field(default=False, validation_alias="ENABLE_REGEX_FALLBACK")
    telegram_api_id: int | None = Field(default=None, validation_alias="TELEGRAM_API_ID", gt=0)
    telegram_api_hash: SecretStr | None = Field(default=None, validation_alias="TELEGRAM_API_HASH")
    telegram_bot_token: SecretStr | None = Field(default=None, validation_alias="TELEGRAM_BOT_TOKEN")
    telegram_admin_chat_id: str | None = Field(default=None, validation_alias="TELEGRAM_ADMIN_CHAT_ID")
    ea_api_key: SecretStr | None = Field(default=None, validation_alias="EA_API_KEY")
    copier_worker_api_key: SecretStr | None = Field(default=None, validation_alias="COPIER_WORKER_API_KEY")
    email_smtp_host: str | None = Field(default=None, validation_alias="EMAIL_SMTP_HOST")
    email_smtp_port: int = Field(default=587, validation_alias="EMAIL_SMTP_PORT", ge=1, le=65535)
    email_smtp_username: str | None = Field(default=None, validation_alias="EMAIL_SMTP_USERNAME")
    email_smtp_password: SecretStr | None = Field(default=None, validation_alias="EMAIL_SMTP_PASSWORD")
    email_from: str | None = Field(default=None, validation_alias="EMAIL_FROM")
    email_smtp_starttls: bool = Field(default=True, validation_alias="EMAIL_SMTP_STARTTLS")
    contact_person_name: str | None = Field(default=None, validation_alias="CONTACT_PERSON_NAME")
    contact_whatsapp: str | None = Field(default=None, validation_alias="CONTACT_WHATSAPP")
    contact_email: str | None = Field(default=None, validation_alias="CONTACT_EMAIL")
    contact_instagram: str | None = Field(default=None, validation_alias="CONTACT_INSTAGRAM")
    contact_telegram: str | None = Field(default=None, validation_alias="CONTACT_TELEGRAM")
    contact_telegram_admin: str | None = Field(default=None, validation_alias="CONTACT_TELEGRAM_ADMIN")
    contact_website: str | None = Field(default=None, validation_alias="CONTACT_WEBSITE")
    contact_facebook: str | None = Field(default=None, validation_alias="CONTACT_FACEBOOK")
    contact_linkedin: str | None = Field(default=None, validation_alias="CONTACT_LINKEDIN")
    contact_x: str | None = Field(default=None, validation_alias="CONTACT_X")
    contact_youtube: str | None = Field(default=None, validation_alias="CONTACT_YOUTUBE")
    contact_tiktok: str | None = Field(default=None, validation_alias="CONTACT_TIKTOK")
    database_url: str | None = Field(default=None, validation_alias="DATABASE_URL")
    airpay_base_url: str | None = Field(default=None, validation_alias="AIRPAY_BASE_URL")
    airpay_api_key: SecretStr | None = Field(default=None, validation_alias="AIRPAY_API_KEY")
    airpay_merchant_uid: str | None = Field(default=None, validation_alias="AIRPAY_MERCHANT_UID")

    @model_validator(mode="after")
    def validate_credentials(self) -> "Settings":
        if self.app_env not in {"production", "development"}:
            raise ValueError("APP_ENV harus production atau development")
        if self.app_env == "production" and not self.cookie_secure:
            raise ValueError("COOKIE_SECURE tidak boleh false di production")
        if self.app_env == "production" and not self.database_url:
            raise ValueError("DATABASE_URL wajib diisi di production")
        app_secret = self.app_secret_key.get_secret_value()
        admin_password = self.admin_password.get_secret_value()
        if len(app_secret) < 32:
            raise ValueError("APP_SECRET_KEY must contain at least 32 characters")
        if any(marker in app_secret.casefold() for marker in ("replace-with", "change-this", "changethis", "your-secret")):
            raise ValueError("APP_SECRET_KEY masih memakai placeholder; ganti dengan secret acak")
        if len(admin_password) < 12:
            raise ValueError("ADMIN_PASSWORD must contain at least 12 characters")
        if any(marker in admin_password.casefold() for marker in ("replace-with", "change-this", "changethis", "your-password")):
            raise ValueError("ADMIN_PASSWORD masih memakai placeholder; ganti sebelum menjalankan aplikasi")
        if not self.admin_username.strip():
            raise ValueError("ADMIN_USERNAME cannot be empty")
        if not self.gemini_model.strip():
            raise ValueError("GEMINI_MODEL cannot be empty")
        if not self.gemini_cs_model.strip():
            raise ValueError("GEMINI_CS_MODEL cannot be empty")
        if self.airpay_base_url:
            parsed_airpay_url = urlsplit(self.airpay_base_url.strip())
            if (
                parsed_airpay_url.scheme not in {"https", "http"}
                or not parsed_airpay_url.hostname
                or parsed_airpay_url.username
                or parsed_airpay_url.password
                or parsed_airpay_url.path not in {"", "/"}
                or parsed_airpay_url.query
                or parsed_airpay_url.fragment
                or (
                    parsed_airpay_url.scheme != "https"
                    and parsed_airpay_url.hostname.casefold() not in {"localhost", "127.0.0.1", "::1"}
                )
            ):
                raise ValueError("AIRPAY_BASE_URL harus HTTPS atau HTTP localhost tanpa kredensial/path query")
        if (self.telegram_api_id is None) != (self.telegram_api_hash is None):
            raise ValueError("TELEGRAM_API_ID dan TELEGRAM_API_HASH harus diisi bersamaan")
        if (self.telegram_bot_token is None) != (self.telegram_admin_chat_id is None):
            raise ValueError("TELEGRAM_BOT_TOKEN dan TELEGRAM_ADMIN_CHAT_ID harus diisi bersamaan")
        if self.telegram_admin_chat_id is not None:
            chat_id = self.telegram_admin_chat_id.strip()
            if not chat_id.isdigit() or int(chat_id) < 1:
                raise ValueError("TELEGRAM_ADMIN_CHAT_ID harus berupa ID chat pribadi admin yang positif")
        if self.database_url and not self.database_url.startswith("sqlite:"):
            raise ValueError("DATABASE_URL harus memakai SQLite, contoh: sqlite:///backend/data/app.db")
        if self.app_env == "production" and self.database_url:
            database_path = self.database_url.removeprefix("sqlite:///")
            if not Path(database_path).is_absolute():
                raise ValueError("DATABASE_URL production harus memakai jalur SQLite absolut")
        if self.ea_api_key is not None:
            ea_key = self.ea_api_key.get_secret_value()
            if len(ea_key) < 24:
                raise ValueError("EA_API_KEY must contain at least 24 characters")
            if any(marker in ea_key.casefold() for marker in ("replace-with", "change-this", "changethis", "your-api-key")):
                raise ValueError("EA_API_KEY masih memakai placeholder; ganti sebelum menjalankan EA")
        if self.copier_worker_api_key is not None:
            worker_key = self.copier_worker_api_key.get_secret_value()
            if len(worker_key) < 32:
                raise ValueError("COPIER_WORKER_API_KEY minimal 32 karakter")
            if any(marker in worker_key.casefold() for marker in ("replace-with", "change-this", "your-api-key")):
                raise ValueError("COPIER_WORKER_API_KEY masih memakai placeholder")
        smtp_values = (self.email_smtp_host, self.email_smtp_username, self.email_smtp_password, self.email_from)
        if any(smtp_values) and not all(smtp_values):
            raise ValueError("EMAIL_SMTP_HOST, EMAIL_SMTP_USERNAME, EMAIL_SMTP_PASSWORD, dan EMAIL_FROM harus diisi bersama")
        return self

    @property
    def email_configured(self) -> bool:
        return all((self.email_smtp_host, self.email_smtp_username, self.email_smtp_password, self.email_from))

    @property
    def airpay_configured(self) -> bool:
        api_key = self.airpay_api_key.get_secret_value().strip() if self.airpay_api_key else ""
        return bool(
            self.airpay_base_url
            and api_key
            and api_key.casefold() != "qp-key-your_secret_key"
        )

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(PROJECT_ROOT / 'backend' / 'data' / 'app.db').as_posix()}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
