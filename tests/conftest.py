"""Shared fixtures: isolated plan store, in-memory telemetry, a scripted fake LLM."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types

from veggie_planner.config import Settings, get_settings
from veggie_planner.observability import configure_telemetry
from veggie_planner.storage import PlanStore, set_plan_store

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")


@pytest.fixture(scope="session", autouse=True)
def _telemetry() -> None:
    configure_telemetry(Settings(session_backend="memory", log_json=False), in_memory=True)


@pytest.fixture(autouse=True)
def plan_store() -> Iterator[PlanStore]:
    store = PlanStore.in_memory()
    set_plan_store(store)
    yield store
    set_plan_store(None)


@pytest.fixture
def settings() -> Settings:
    get_settings.cache_clear()
    return Settings(session_backend="memory", log_json=False, trace_console=False)


class FakeState:
    """Minimal stand-in for ``ToolContext`` in pure tool tests."""

    def __init__(self, state: dict[str, Any] | None = None, user_id: str = "u1") -> None:
        self.state: dict[str, Any] = dict(state or {})
        self.user_id = user_id
        self.function_call_id = "fc"
        self.agent_name = "test"


@pytest.fixture
def ctx() -> FakeState:
    return FakeState()


Decider = Callable[[LlmRequest], types.Part | None]


class ScriptedLlm(BaseLlm):
    """Fake model driven by a decision function over the request.

    The decider sees the tools offered and the conversation so far and returns either a
    ``Part`` with a function call or text. ``None`` falls back to a plain text reply.
    """

    decider: Any = None
    calls: int = 0

    async def generate_content_async(  # type: ignore[override]
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncIterator[LlmResponse]:
        self.calls += 1
        part = self.decider(llm_request) if self.decider else None
        if part is None:
            part = types.Part(text="ok")
        yield LlmResponse(
            content=types.Content(role="model", parts=[part]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=10, candidates_token_count=5, total_token_count=15
            ),
        )


def tool_names(req: LlmRequest) -> set[str]:
    return {
        d.name
        for t in (req.config.tools or [])
        for d in (getattr(t, "function_declarations", None) or [])
        if d.name
    }


def function_responses(req: LlmRequest) -> list[tuple[str, Any]]:
    out = []
    for c in req.contents or []:
        for p in c.parts or []:
            if p.function_response:
                out.append((p.function_response.name or "", p.function_response.response))
    return out


def call(name: str, **args: Any) -> types.Part:
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


def text(t: str) -> types.Part:
    return types.Part(text=t)


def happy_path_decider(req: LlmRequest) -> types.Part | None:
    """Coordinator sets profile then plans; planner drafts; critic evaluates then exits."""
    names = tool_names(req)
    responses = function_responses(req)
    if "draft_meal_plan" in names:  # planner
        return call("draft_meal_plan", days=3) if not responses else text("Drafted a 3-day plan.")
    if "exit_loop" in names:  # critic
        return call("evaluate_meal_plan") if not responses else call("exit_loop")
    if "build_grocery_list" in names and "planning_pipeline" not in names:  # grocery agent
        return call("build_grocery_list") if not responses else text("Grocery list ready.")
    if "planning_pipeline" in names:  # coordinator
        done = [n for n, _ in responses]
        if "update_user_profile" not in done:
            return call("update_user_profile", household_size=2, diet="vegan", allergens=["nuts"])
        if "planning_pipeline" not in done:
            return call("planning_pipeline", request="3 day vegan plan for two")
        if "grocery_agent" not in done:
            return call("grocery_agent", request="grocery list for the current plan")
        return text("Here is your plan and grocery list.")
    return None


@pytest.fixture
def fake_llm() -> ScriptedLlm:
    return ScriptedLlm(model="fake", decider=happy_path_decider)


async def run_agent(
    agent: Any,
    message: str,
    *,
    app_name: str = "test_app",
    user_id: str = "u1",
    state: dict[str, Any] | None = None,
) -> tuple[InMemoryRunner, Any, list[Any]]:
    runner = InMemoryRunner(agent=agent, app_name=app_name)
    session = await runner.session_service.create_session(
        app_name=app_name, user_id=user_id, state=state or {}
    )
    msg = types.Content(role="user", parts=[types.Part(text=message)])
    events = [
        e async for e in runner.run_async(user_id=user_id, session_id=session.id, new_message=msg)
    ]
    return runner, session, events
