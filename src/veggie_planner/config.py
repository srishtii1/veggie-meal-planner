"""Application settings (environment-driven, prefix ``VMP_``)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Export GOOGLE_API_KEY / GOOGLE_GENAI_* etc. from .env so the google-genai client sees them.
# Real environment variables always win (override=False).
load_dotenv(override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VMP_", env_file=".env", extra="ignore")

    app_name: str = "veggie_meal_planner"
    model: str = "gemini-3.5-flash"
    planner_model: str | None = None  # defaults to ``model``
    session_backend: Literal["memory", "sqlite", "vertex"] = "sqlite"
    db_url: str = "sqlite+aiosqlite:///./data/veggie_planner.db"
    plan_db_path: str = "./data/veggie_planner_plans.db"
    log_level: str = "INFO"
    log_json: bool = True
    trace_console: bool = False
    max_loop_iterations: int = 3
    # Human-in-the-loop: pause and ask before plan edits, permanent bans, or loosening
    # allergen/diet constraints. Disable only for batch/eval runs.
    require_confirmation: bool = True
    # Privacy
    redact_pii: bool = True
    capture_content_in_spans: bool = False
    host: str = "0.0.0.0"
    port: int = 8080
    public_host: str = "localhost"  # advertised in the A2A agent card

    @property
    def effective_planner_model(self) -> str:
        return self.planner_model or self.model


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
