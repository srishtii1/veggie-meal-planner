"""REST human-in-the-loop flow and background memory persistence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from tests.conftest import ScriptedLlm, call, happy_path_decider, text
from veggie_planner.api import create_app
from veggie_planner.api.app import persist_memory
from veggie_planner.config import Settings


def _turn_responses(req: Any) -> tuple[str, list[tuple[str, Any]]]:
    """Latest user text and the function responses that came after it."""
    last_text, after = "", []
    for c in req.contents or []:
        for p in c.parts or []:
            if c.role == "user" and p.text:
                last_text, after = p.text, []
            if p.function_response:
                after.append((p.function_response.name or "", p.function_response.response))
    return last_text, after


def decider(req: Any) -> Any:
    user_text, responses = _turn_responses(req)
    if "swap" not in user_text.lower():
        return happy_path_decider(req)
    if "planning_pipeline" not in {
        d.name for t in req.config.tools or [] for d in t.function_declarations or []
    }:
        return happy_path_decider(req)
    if not responses:
        return call("swap_meal", day="Monday", slot="dinner", recipe_id="chana-masala")
    last = responses[-1][1]
    err = str(last.get("error", "")) if isinstance(last, dict) else ""
    if "requires confirmation" in err:
        return text("")  # stay quiet; API synthesises the approval prompt
    if "rejected" in err:
        return text("Okay, I kept Monday dinner unchanged.")
    return text("Swapped Monday dinner to Chana Masala.")


@pytest.fixture
async def app_and_client(settings: Settings) -> AsyncIterator[tuple[Any, httpx.AsyncClient]]:
    app = create_app(settings, model=ScriptedLlm(model="fake", decider=decider), enable_a2a=False)
    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
    ):
        yield app, c


async def _plan(client: httpx.AsyncClient) -> dict[str, Any]:
    r = await client.post(
        "/chat", json={"message": "Plan 3 vegan days for 2, I'm jane@example.com", "user_id": "u"}
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _dinner(client: httpx.AsyncClient, plan_id: str) -> str:
    plan = (await client.get(f"/plans/{plan_id}")).json()
    return str(plan["days"][0]["meals"]["dinner"])


async def test_swap_requires_approval_then_executes(app_and_client: Any) -> None:
    _, client = app_and_client
    first = await _plan(client)
    plan_id, sid = first["current_plan_id"], first["session_id"]
    original = await _dinner(client, plan_id)

    r = await client.post(
        "/chat", json={"message": "swap Monday dinner", "user_id": "u", "session_id": sid}
    )
    body = r.json()
    assert r.status_code == 200 and len(body["pending_confirmations"]) == 1
    pending = body["pending_confirmations"][0]
    assert pending["tool"] == "swap_meal" and pending["args"]["day"] == "Monday"
    assert "replace Monday dinner with chana-masala" in pending["summary"]
    assert "approval" in body["reply"]
    assert any(
        t["tool"] == "swap_meal" and t["status"] == "awaiting_confirmation"
        for t in body["tool_calls"]
    )
    assert await _dinner(client, plan_id) == original  # nothing executed yet

    ok = await client.post(
        "/chat/confirm",
        json={
            "user_id": "u",
            "session_id": sid,
            "confirmation_id": pending["confirmation_id"],
            "approved": True,
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["pending_confirmations"] == []
    assert ok.json()["reply"] == "Swapped Monday dinner to Chana Masala."
    assert await _dinner(client, plan_id) == "chana-masala"

    metrics = (await client.get("/metrics")).json()
    decisions = metrics["agent.confirmations"]["points"]
    assert any(p["attributes"]["decision"] == "approved" for p in decisions)


async def test_swap_rejected(app_and_client: Any) -> None:
    _, client = app_and_client
    first = await _plan(client)
    plan_id, sid = first["current_plan_id"], first["session_id"]
    original = await _dinner(client, plan_id)
    body = (
        await client.post(
            "/chat", json={"message": "swap Monday dinner", "user_id": "u", "session_id": sid}
        )
    ).json()
    cid = body["pending_confirmations"][0]["confirmation_id"]
    r = await client.post(
        "/chat/confirm",
        json={
            "user_id": "u",
            "session_id": sid,
            "confirmation_id": cid,
            "approved": False,
            "reason": "I like it",
        },
    )
    assert r.json()["reply"] == "Okay, I kept Monday dinner unchanged."
    assert await _dinner(client, plan_id) == original


async def test_confirm_unknown_session_404(app_and_client: Any) -> None:
    _, client = app_and_client
    r = await client.post(
        "/chat/confirm",
        json={"user_id": "u", "session_id": "nope", "confirmation_id": "x", "approved": True},
    )
    assert r.status_code == 404


async def test_memory_persisted_in_background_and_redacted(app_and_client: Any) -> None:
    app, client = app_and_client
    first = await _plan(client)
    runner = app.state.runner
    # BackgroundTasks run after the response body is sent; ASGITransport waits for them.
    result = await runner.memory_service.search_memory(
        app_name=runner.app_name, user_id="u", query="vegan"
    )
    texts = " ".join(p.text or "" for m in result.memories for p in (m.content.parts or []))
    assert "vegan" in texts.lower()
    assert "jane@example.com" not in texts and "[REDACTED_EMAIL]" in texts
    assert first["session_id"]


async def test_chat_does_not_await_memory(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The memory write is scheduled as a background task, not awaited in the handler."""
    from fastapi import BackgroundTasks

    scheduled: list[Any] = []
    original_add = BackgroundTasks.add_task

    def spy(self: BackgroundTasks, func: Any, *args: Any, **kwargs: Any) -> None:
        scheduled.append(func)
        original_add(self, func, *args, **kwargs)

    monkeypatch.setattr(BackgroundTasks, "add_task", spy)
    app, client = app_and_client
    calls: list[str] = []
    real = app.state.runner.memory_service.add_session_to_memory

    async def tracking(session: Any) -> None:
        calls.append(session.id)
        await real(session)

    monkeypatch.setattr(app.state.runner.memory_service, "add_session_to_memory", tracking)
    await _plan(client)
    assert persist_memory in scheduled
    assert len(calls) == 1


async def test_memory_failure_does_not_break_chat(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = app_and_client

    async def boom(session: Any) -> None:
        raise RuntimeError("memory bank down")

    monkeypatch.setattr(app.state.runner.memory_service, "add_session_to_memory", boom)
    body = await _plan(client)
    assert body["reply"]
