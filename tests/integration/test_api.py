"""REST + A2A integration tests against the FastAPI app with a fake model."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport

from tests.conftest import ScriptedLlm, happy_path_decider
from veggie_planner.api import create_app
from veggie_planner.config import Settings


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings, model=ScriptedLlm(model="fake", decider=happy_path_decider))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_healthz_and_openapi(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["recipes"] > 0
    spec = (await client.get("/openapi.json")).json()
    assert {"/chat", "/sessions/{session_id}", "/plans/{plan_id}", "/healthz", "/metrics"} <= set(
        spec["paths"]
    )


async def test_chat_flow_sessions_plans_metrics(client: httpx.AsyncClient) -> None:
    r = await client.post("/chat", json={"message": "Plan 3 vegan days for 2", "user_id": "alice"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"] == "Here is your plan and grocery list."
    assert [t["tool"] for t in body["tool_calls"]] == [
        "update_user_profile",
        "planning_pipeline",
        "grocery_agent",
    ]
    assert all(t["status"] for t in body["tool_calls"])
    assert body["current_plan_id"] and len(body["trace_id"]) == 32

    s = await client.get(f"/sessions/{body['session_id']}", params={"user_id": "alice"})
    assert s.status_code == 200 and s.json()["state"]["user:profile"]["diet"] == "vegan"
    assert s.json()["event_count"] > 3

    p = await client.get(f"/plans/{body['current_plan_id']}")
    assert p.status_code == 200 and p.json()["user_id"] == "alice" and len(p.json()["days"]) == 3
    assert (await client.get("/plans/nope")).status_code == 404
    assert (await client.get("/recipes/chana-masala")).status_code == 200
    assert (await client.get("/recipes/nope")).status_code == 404

    # continuing the same session works; unknown session is 404
    again = await client.post(
        "/chat", json={"message": "thanks", "user_id": "alice", "session_id": body["session_id"]}
    )
    assert again.status_code == 200 and again.json()["session_id"] == body["session_id"]
    missing = await client.post(
        "/chat", json={"message": "hi", "user_id": "alice", "session_id": "nope"}
    )
    assert missing.status_code == 404

    m = (await client.get("/metrics")).json()
    assert "agent.tool_calls" in m and "agent.llm_calls" in m and "agent.tokens" in m
    assert sum(p["value"] for p in m["agent.tool_calls"]["points"]) >= 5


async def test_chat_validation(client: httpx.AsyncClient) -> None:
    assert (await client.post("/chat", json={"message": ""})).status_code == 422


async def test_a2a_agent_card_and_message(client: httpx.AsyncClient) -> None:
    card = await client.get("/a2a/.well-known/agent-card.json")
    assert card.status_code == 200, card.text
    data = card.json()
    assert data["name"] == "meal_planner_coordinator"
    assert data["supportedInterfaces"][0]["url"].endswith("/a2a")
    assert data["skills"]

    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": "m1",
                "parts": [{"kind": "text", "text": "Plan 3 vegan days for 2"}],
            }
        },
    }
    r = await client.post("/a2a", json=payload)
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    dumped = str(result)
    assert "Here is your plan and grocery list." in dumped
