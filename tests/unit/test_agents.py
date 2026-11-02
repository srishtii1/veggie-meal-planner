"""Agent-tree tests with a scripted fake model (no network)."""

from __future__ import annotations

from typing import Any

from google.adk.models.llm_request import LlmRequest
from google.genai import types

from tests.conftest import (
    ScriptedLlm,
    call,
    function_responses,
    happy_path_decider,
    run_agent,
    text,
    tool_names,
)
from veggie_planner.agents import build_root_agent
from veggie_planner.agents.callbacks import REFUSAL_TEXT
from veggie_planner.observability import finished_spans
from veggie_planner.tools.planning import CURRENT_PLAN_KEY


def _texts(events: list[Any]) -> list[str]:
    return [p.text for e in events if e.content for p in (e.content.parts or []) if p.text]


def _calls(events: list[Any]) -> list[str]:
    return [
        p.function_call.name
        for e in events
        if e.content
        for p in (e.content.parts or [])
        if p.function_call
    ]


async def test_happy_path_profile_plan_and_grocery(fake_llm: ScriptedLlm) -> None:
    agent = build_root_agent(model=fake_llm)
    runner, session, events = await run_agent(
        agent, "Plan 3 vegan days for two of us, nut-free", app_name="happy"
    )
    calls = _calls(events)
    assert calls == ["update_user_profile", "planning_pipeline", "grocery_agent"]
    after = await runner.session_service.get_session(
        app_name="happy", user_id="u1", session_id=session.id
    )
    assert after is not None
    assert after.state["user:profile"]["diet"] == "vegan" and after.state["user:profile"][
        "allergens"
    ] == ["nuts"]
    assert after.state[CURRENT_PLAN_KEY]
    # the pipeline tool result is the presenter's markdown table
    pipeline_result = next(
        p.function_response.response
        for e in events
        if e.content
        for p in (e.content.parts or [])
        if p.function_response and p.function_response.name == "planning_pipeline"
    )
    rendered = str(pipeline_result.get("result", pipeline_result))
    assert "| Day |" in rendered and after.state[CURRENT_PLAN_KEY] in rendered
    assert (
        events[-1].is_final_response()
        and _texts(events)[-1] == "Here is your plan and grocery list."
    )


async def test_critic_feedback_triggers_fix_iteration() -> None:
    """Critic rejects once; planner must then swap instead of redrafting; critic approves."""
    seen: dict[str, int] = {"critic": 0}

    def decider(req: LlmRequest) -> types.Part | None:
        names = tool_names(req)
        responses = function_responses(req)
        if "draft_meal_plan" in names:
            if any(n == "swap_meal" for n, _ in responses):
                return text("Swapped Tuesday dinner.")
            if "critic_feedback" in str(req.config.system_instruction) and "Tuesday dinner" in str(
                req.config.system_instruction
            ):
                return call("swap_meal", day="Tuesday", slot="dinner", max_prep_time_min=20)
            return call("draft_meal_plan", days=3) if not responses else text("Drafted.")
        if "exit_loop" in names:
            if not responses:
                return call("evaluate_meal_plan")
            seen["critic"] += 1
            if seen["critic"] == 1:
                return text("1. Tuesday dinner: too slow -> swap for something under 20 min")
            return call("exit_loop")
        if "planning_pipeline" in names:
            done = [n for n, _ in responses]
            if "planning_pipeline" not in done:
                return call("planning_pipeline", request="3 days")
            return text("done")
        return None

    agent = build_root_agent(model=ScriptedLlm(model="fake", decider=decider))
    _, _, events = await run_agent(
        agent, "plan", app_name="loop", state={"user:profile": {"household_size": 2}}
    )
    assert "planning_pipeline" in _calls(events)
    assert seen["critic"] == 2
    assert _texts(events)[-1] == "done"


async def test_non_veg_request_is_refused_without_model_call() -> None:
    llm = ScriptedLlm(model="fake", decider=happy_path_decider)
    agent = build_root_agent(model=llm)
    _, _, events = await run_agent(
        agent, "Add grilled chicken to my Tuesday dinner", app_name="refuse"
    )
    assert _texts(events) == [REFUSAL_TEXT]
    assert llm.calls == 0


async def test_tool_guardrail_blocks_meat_in_pantry() -> None:
    def decider(req: LlmRequest) -> types.Part | None:
        responses = function_responses(req)
        if not responses:
            return call("update_user_profile", household_size=2, pantry=["rice", "chicken"])
        assert responses[0][1]["guardrail"] == "vegetarian_only"
        return text("Sorry, I can't add chicken; shall I add tofu instead?")

    agent = build_root_agent(model=ScriptedLlm(model="fake", decider=decider))
    runner, session, events = await run_agent(
        agent, "My pantry has rice and leftover roast", app_name="guard"
    )
    after = await runner.session_service.get_session(
        app_name="guard", user_id="u1", session_id=session.id
    )
    assert after is not None and "user:profile" not in after.state
    assert _texts(events)[-1].startswith("Sorry")


async def test_presenter_without_plan() -> None:
    from veggie_planner.agents.presenter import PlanPresenterAgent

    _, _, events = await run_agent(PlanPresenterAgent(name="presenter"), "x", app_name="presenter")
    assert "No plan was produced" in _texts(events)[0]


async def test_spans_emitted_for_agent_model_and_tools(fake_llm: ScriptedLlm) -> None:
    before = len(finished_spans())
    agent = build_root_agent(model=fake_llm)
    await run_agent(agent, "Plan 3 days", app_name="spans")
    names = [s.name for s in finished_spans()[before:]]
    assert any("invoke_agent" in n or "agent" in n for n in names)
    assert any("execute_tool" in n or "tool" in n for n in names), names
    assert any("call_llm" in n or "generate_content" in n for n in names), names
