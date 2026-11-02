"""Persistent storage for meal plans (SQLite via SQLAlchemy Core).

Profiles and ratings live in ADK session state (``user:`` prefix) and are
persisted by the configured ``SessionService``; plans are larger documents that
also need to be fetched by id from the REST API, so they get their own table.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from sqlalchemy import Column, DateTime, MetaData, String, Table, Text, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from veggie_planner.models import MealPlan

_metadata = MetaData()
plans_table = Table(
    "meal_plans",
    _metadata,
    Column("plan_id", String(64), primary_key=True),
    Column("user_id", String(128), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("payload", Text, nullable=False),
)


class PlanStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        _metadata.create_all(engine)

    @classmethod
    def in_memory(cls) -> PlanStore:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        return cls(engine)

    @classmethod
    def from_path(cls, path: str) -> PlanStore:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
        return cls(engine)

    def save(self, plan: MealPlan) -> None:
        payload = plan.model_dump_json()
        with self._engine.begin() as conn:
            conn.execute(plans_table.delete().where(plans_table.c.plan_id == plan.plan_id))
            conn.execute(
                plans_table.insert().values(
                    plan_id=plan.plan_id,
                    user_id=plan.user_id,
                    created_at=plan.created_at,
                    payload=payload,
                )
            )

    def get(self, plan_id: str) -> MealPlan | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(plans_table.c.payload).where(plans_table.c.plan_id == plan_id)
            ).first()
        return MealPlan.model_validate(json.loads(row[0])) if row else None

    def list_for_user(self, user_id: str, limit: int = 10) -> list[MealPlan]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(plans_table.c.payload)
                .where(plans_table.c.user_id == user_id)
                .order_by(plans_table.c.created_at.desc())
                .limit(limit)
            ).all()
        return [MealPlan.model_validate(json.loads(r[0])) for r in rows]


_store: PlanStore | None = None
_lock = threading.Lock()


def get_plan_store() -> PlanStore:
    """Process-wide store; configured lazily from settings, overridable in tests."""
    global _store
    with _lock:
        if _store is None:
            from veggie_planner.config import get_settings

            _store = PlanStore.from_path(get_settings().plan_db_path)
        return _store


def set_plan_store(store: PlanStore | None) -> None:
    global _store
    with _lock:
        _store = store
