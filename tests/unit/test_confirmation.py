"""Human-in-the-loop confirmation: policies and agent-level pause/approve/reject."""

from __future__ import annotations

from typing import Any

from google.adk.flows.llm_flows.functions import REQUEST_CONFIRMATION_FUNCTION_CALL_NAME as RC
from google.adk.runners import InMemoryRunner
from google.genai import types

from tests.conftest import FakeState, ScriptedLlm, call, function_responses, text
from veggie_planner.agents import build_root_agent
from veggie_planner.agents.confirmation import (
    confirm_profile_change,
    confirm_rating,
    confirm_swap,
)
from veggie_planner.tools import draft_meal_plan, get_meal_plan, update_user_profile

# ---- policies -----------------------------------------------------------------------


def test_policies() -> None:
    assert confirm_swap(day="Monday", slot="dinner")
    assert confirm_rating(rating=1) and not confirm_rating(rating=2) and not confirm_rating()

    ctx: Any = FakeState()
    # first-time setup is never gated
    assert not confirm_profile_change(tool_context=ctx, diet="vegan", allergens=["nuts"])
    update_user_profile(ctx, household_size=2, diet="vegan", allergens=["nuts", "soy"])
    assert not confirm_profile_change(
        tool_context=ctx, allergens=["nuts", "soy", "gluten"]
    )  # tighter
    assert confirm_profile_change(tool_context=ctx, allergens=["nuts"])  # removes soy
    assert confirm_profile_change(tool_context=ctx, diet="vegetarian")  # diet change
    assert not confirm_profile_change(tool_context=ctx, diet="VEGAN")  # same diet
    assert not confirm_profile_change(tool_context=ctx)


# ---- agent-level flow ---------------------------------------------------------------


def _last_response(req: Any) -> dict[str, Any]:
    responses = function_responses(req)
    return responses[-1][1] if responses else {}


def _swap_decider(req: Any) -> Any:
    last = _last_response(req)
    if not last:
        return call("swap_meal", day="Monday", slot="dinner", recipe_id="chana-masala")
    if "requires confirmation" in str(last.get("error", "")):
        return text("I'd like to swap Monday dinner for Chana Masala - approve?")
    if "rejected" in str(last.get("error", "")):
        return text("OK, I left Monday dinner as it was.")
    return text("Done, Monday dinner is now Chana Masala.")


async def _setup(app: str) -> tuple[InMemoryRunner, Any, str]:
    ctx: Any = FakeState()
    update_user_profile(ctx, household_size=2)
    draft_meal_plan(ctx, days=2)
    agent = build_root_agent(model=ScriptedLlm(model="fake", decider=_swap_decider))
    runner = InMemoryRunner(agent=agent, app_name=app)
    session = await runner.session_service.create_session(
        app_name=app, user_id="u1", state=ctx.state
    )
    original = get_meal_plan(ctx)["plan"]["days"][0]["meals"]["dinner"]["recipe_id"]
    assert original != "chana-masala"
    return runner, session, original


async def _send(runner: InMemoryRunner, session: Any, message: types.Content) -> list[Any]:
    return [
        e async for e in runner.run_async(user_id="u1", session_id=session.id, new_message=message)
    ]


def _confirmation_call(events: list[Any]) -> Any:
    for e in events:
        for p in e.content.parts if e.content and e.content.parts else []:
            if p.function_call and p.function_call.name == RC:
                return p.function_call
    return None


async def _dinner(runner: InMemoryRunner, session: Any) -> str:
    s = await runner.session_service.get_session(
        app_name=session.app_name, user_id="u1", session_id=session.id
    )
    assert s is not None
    return str(
        get_meal_plan(FakeState(dict(s.state)))["plan"]["days"][0]["meals"]["dinner"]["recipe_id"]
    )


async def test_swap_pauses_then_runs_after_approval() -> None:
    runner, session, original = await _setup("hitl_ok")
    events = await _send(
        runner, session, types.Content(role="user", parts=[types.Part(text="swap monday dinner")])
    )
    fc = _confirmation_call(events)
    assert fc is not None and fc.args["originalFunctionCall"]["name"] == "swap_meal"
    assert await _dinner(runner, session) == original  # not executed yet

    approve = types.Part(
        function_response=types.FunctionResponse(id=fc.id, name=RC, response={"confirmed": True})
    )
    events = await _send(runner, session, types.Content(role="user", parts=[approve]))
    assert await _dinner(runner, session) == "chana-masala"
    texts = [p.text for e in events if e.content for p in (e.content.parts or []) if p.text]
    assert texts[-1].startswith("Done")


async def test_swap_rejected_leaves_plan_unchanged() -> None:
    runner, session, original = await _setup("hitl_no")
    events = await _send(
        runner, session, types.Content(role="user", parts=[types.Part(text="swap monday dinner")])
    )
    fc = _confirmation_call(events)
    reject = types.Part(
        function_response=types.FunctionResponse(id=fc.id, name=RC, response={"confirmed": False})
    )
    events = await _send(runner, session, types.Content(role="user", parts=[reject]))
    assert await _dinner(runner, session) == original
    texts = [p.text for e in events if e.content for p in (e.content.parts or []) if p.text]
    assert texts[-1].startswith("OK, I left")


async def test_confirmation_can_be_disabled() -> None:
    agent = build_root_agent(
        model=ScriptedLlm(model="fake", decider=_swap_decider), require_confirmation=False
    )
    ctx: Any = FakeState()
    update_user_profile(ctx, household_size=2)
    draft_meal_plan(ctx, days=2)
    runner = InMemoryRunner(agent=agent, app_name="hitl_off")
    session = await runner.session_service.create_session(
        app_name="hitl_off", user_id="u1", state=ctx.state
    )
    events = await _send(
        runner, session, types.Content(role="user", parts=[types.Part(text="swap")])
    )
    assert _confirmation_call(events) is None
    assert await _dinner(runner, session) == "chana-masala"
