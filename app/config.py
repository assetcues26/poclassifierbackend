"""Load settings from environment only."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    azure_classify_url: str = ""
    azure_function_key: str = ""

    batch_size: int = 5
    max_excel_po_rows: int = 50
    azure_timeout_seconds: float = 300.0
    azure_max_retries: int = 3
    azure_retry_backoff_seconds: float = 2.0

    excel_po_column: str = "Purchase Order Number"
    excel_json_column: str = "ERP JSON"

    override_locationcode: str = "2701163"
    override_companyname: str = "Myntra Designs Private Limited"
    override_companycode: str = "502"

    jobs_dir: str = "jobs"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    host: str = "0.0.0.0"
    port: int = 8000

    # Auth — APP_USERS=username:bcrypt_hash (comma-separated for multiple)
    app_users: str = ""
    session_secret: str = ""
    session_cookie_name: str = "po_ui_session"
    session_max_age_hours: float = 8.0
    # Local: lax + false. Vercel↔Render: none + true
    cookie_samesite: str = "lax"
    cookie_secure: bool = False

    @property
    def jobs_path(self) -> Path:
        path = Path(self.jobs_dir)
        if not path.is_absolute():
            path = _BACKEND_ROOT / path
        return path

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
