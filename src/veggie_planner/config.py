"""Application settings (environment-driven, prefix ``VMP_``)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VMP_", env_file=".env", extra="ignore")

    app_name: str = "veggie_meal_planner"
    model: str = "gemini-2.5-flash"
    planner_model: str | None = None  # defaults to ``model``
    session_backend: Literal["memory", "sqlite", "vertex"] = "sqlite"
    db_url: str = "sqlite+aiosqlite:///./data/veggie_planner.db"
    plan_db_path: str = "./data/veggie_planner_plans.db"
    log_level: str = "INFO"
    log_json: bool = True
    trace_console: bool = False
    max_loop_iterations: int = 3
    host: str = "0.0.0.0"
    port: int = 8080
    public_host: str = "localhost"  # advertised in the A2A agent card

    @property
    def effective_planner_model(self) -> str:
        return self.planner_model or self.model


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
