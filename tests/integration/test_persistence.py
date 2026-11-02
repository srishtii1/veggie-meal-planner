"""Persistence tests: SQLite session backend keeps ``user:`` state across sessions."""

from __future__ import annotations

from pathlib import Path

import pytest
from google.adk.runners import Runner
from google.genai import types

from tests.conftest import ScriptedLlm, call, function_responses, text
from veggie_planner.agents import build_root_agent
from veggie_planner.config import Settings
from veggie_planner.memory import build_memory_service, build_session_service
from veggie_planner.models import DayPlan, MealPlan
from veggie_planner.storage import PlanStore


def test_plan_store_roundtrip(tmp_path: Path) -> None:
    store = PlanStore.from_path(str(tmp_path / "plans.db"))
    plan = MealPlan(
        plan_id="abc",
        user_id="u",
        servings=2,
        days=[DayPlan(day="Monday", meals={"dinner": "chana-masala"})],
    )
    store.save(plan)
    plan.notes = ["updated"]
    store.save(plan)  # upsert
    got = store.get("abc")
    assert got is not None and got.notes == ["updated"]
    assert store.get("missing") is None
    assert [p.plan_id for p in store.list_for_user("u")] == ["abc"]
    assert store.list_for_user("nobody") == []


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_user_state_persists_across_sessions(tmp_path: Path, backend: str) -> None:
    settings = Settings(
        session_backend=backend,  # type: ignore[arg-type]
        db_url=f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}",
        log_json=False,
    )
    session_service = build_session_service(settings)
    memory_service = build_memory_service(settings)

    def decider(req):  # type: ignore[no-untyped-def]
        if not function_responses(req):
            return call("update_user_profile", household_size=3, diet="lacto")
        return text("saved")

    agent = build_root_agent(model=ScriptedLlm(model="fake", decider=decider))
    runner = Runner(
        agent=agent,
        app_name="persist",
        session_service=session_service,
        memory_service=memory_service,
    )
    s1 = await session_service.create_session(app_name="persist", user_id="bob")
    msg = types.Content(role="user", parts=[types.Part(text="we are 3, eggless")])
    _ = [e async for e in runner.run_async(user_id="bob", session_id=s1.id, new_message=msg)]

    # A brand-new session for the same user sees the ``user:`` prefixed state.
    s2 = await session_service.create_session(app_name="persist", user_id="bob")
    assert s2.state["user:profile"]["household_size"] == 3
    assert s2.state["user:profile"]["diet"] == "lacto"
    # ...and a different user does not.
    s3 = await session_service.create_session(app_name="persist", user_id="alice")
    assert "user:profile" not in s3.state
    await runner.close()
