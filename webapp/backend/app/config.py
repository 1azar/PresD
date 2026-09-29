import json
import re
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WEBAPP_", extra="ignore")

    database_url: str = "postgresql+psycopg://presentations:presentations@postgres/presentations"
    redis_url: str = "redis://redis:6379/0"
    data_root: Path = Path("/data")
    session_days: int = 7
    cookie_secure: bool = False
    allowed_origins: str = "http://localhost:8080"
    allowed_clients_json: str = ""
    max_template_bytes: int = 100 * 1024 * 1024
    max_file_bytes: int = 25 * 1024 * 1024
    max_files_bytes: int = 100 * 1024 * 1024
    max_extracted_chars: int = 50_000
    worker_queue: str = "generation"
    planner_max_workers: int = Field(default=4, ge=1, le=8)
    planner_strict_budget: int = Field(default=300, ge=60, le=600)
    planner_fast_budget: int = Field(default=135, ge=30, le=300)
    planner_outline_wave_budget: int = Field(default=105, ge=10, le=300)
    planner_slide_wave_budget: int = Field(default=105, ge=10, le=300)
    planner_critique_wave_budget: int = Field(default=75, ge=10, le=300)
    generation_job_timeout: int = Field(default=420, ge=120, le=1200)
    template_enrichment_workers: int = Field(default=4, ge=1, le=8)
    template_vlm_timeout: int = Field(default=90, ge=10, le=600)

    @property
    def origins(self) -> set[str]:
        return {item.strip().rstrip("/") for item in self.allowed_origins.split(",") if item.strip()}

    @property
    def allowed_clients(self) -> dict[str, str]:
        try:
            value = json.loads(self.allowed_clients_json)
        except json.JSONDecodeError as exc:
            raise ValueError("WEBAPP_ALLOWED_CLIENTS_JSON must be a JSON object") from exc
        if not isinstance(value, dict) or not value:
            raise ValueError("WEBAPP_ALLOWED_CLIENTS_JSON must contain at least one client")
        if not all(
            isinstance(login, str) and re.fullmatch(r"[\w.@+-]{3,80}", login.strip(), re.UNICODE)
            and isinstance(password, str) and 8 <= len(password) <= 200
            for login, password in value.items()
        ):
            raise ValueError("Allowed client logins or passwords are invalid")
        clients = {login.strip(): password for login, password in value.items()}
        if len(clients) != len(value):
            raise ValueError("Allowed client logins must be unique after trimming whitespace")
        return clients


settings = Settings()
