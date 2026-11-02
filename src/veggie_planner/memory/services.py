"""Factories for session / memory services, selected by ``VMP_SESSION_BACKEND``."""

from __future__ import annotations

from pathlib import Path

from google.adk.memory.base_memory_service import BaseMemoryService
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.sessions import BaseSessionService, InMemorySessionService

from veggie_planner.config import Settings, get_settings


def build_session_service(settings: Settings | None = None) -> BaseSessionService:
    settings = settings or get_settings()
    match settings.session_backend:
        case "memory":
            return InMemorySessionService()
        case "sqlite":
            from google.adk.sessions import DatabaseSessionService

            if settings.db_url.startswith("sqlite"):
                db_file = settings.db_url.split("///", 1)[-1]
                Path(db_file).parent.mkdir(parents=True, exist_ok=True)
            return DatabaseSessionService(db_url=settings.db_url)
        case "vertex":
            from google.adk.sessions import VertexAiSessionService

            return VertexAiSessionService()
    raise ValueError(f"unknown session backend {settings.session_backend!r}")  # pragma: no cover


def build_memory_service(settings: Settings | None = None) -> BaseMemoryService:
    settings = settings or get_settings()
    if settings.session_backend == "vertex":
        from google.adk.memory import VertexAiMemoryBankService

        return VertexAiMemoryBankService()
    # Local backends share one in-process memory; sessions are added to it after each turn.
    return InMemoryMemoryService()
