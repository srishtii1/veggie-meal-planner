import logging
from typing import Any

import structlog
from google.adk.apps import App
from google.adk.events import Event
from google.adk.runners import InMemoryRunner
from google.adk.sessions import Session
from google.genai import types
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.conftest import ScriptedLlm, call, function_responses, run_agent, text
from veggie_planner.agents import build_root_agent
from veggie_planner.observability import MetricsPlugin, finished_spans
from veggie_planner.observability.redaction import (
    RedactingFilter,
    RedactingSpanExporter,
    redact_processor,
    redact_session,
    redact_text,
    redact_value,
)


def test_redact_text_covers_common_pii() -> None:
    cases = {
        "mail jane.doe@example.com": "[REDACTED_EMAIL]",
        "call +1 (415) 555-0134": "[REDACTED_PHONE]",
        "card 4111 1111 1111 1111": "[REDACTED_CARD]",
        "ssn 123-45-6789": "[REDACTED_SSN]",
        # Fake key-shaped strings are assembled at runtime so no secret-like literal is committed.
        "key " + "AI" + "za" + "x" * 35: "[REDACTED_SECRET]",
        "token " + "gh" + "p_" + "y" * 30: "[REDACTED_SECRET]",
        "I live at 221 Baker Street": "[REDACTED_ADDRESS]",
        "from 10.0.0.12": "[REDACTED_IP]",
    }
    for raw, marker in cases.items():
        out = redact_text(raw)
        assert marker in out, (raw, out)


def test_redact_text_keeps_domain_numbers() -> None:
    s = "Plan 5 days for 2, 30 min, $60 budget, 2000 kcal, 70 g protein, plan e523376517"
    assert redact_text(s) == s


def test_redact_value_recurses_and_masks_sensitive_keys() -> None:
    out = redact_value({"api_key": "abc", "pantry": ["rice", "me@x.io"], "n": 3, "t": ("a@b.co",)})
    assert out == {
        "api_key": "[REDACTED]",
        "pantry": ["rice", "[REDACTED_EMAIL]"],
        "n": 3,
        "t": ("[REDACTED_EMAIL]",),
    }


def test_structlog_processor() -> None:
    event = {"event": "x", "user_email": "a@b.co", "password": "hunter2", "trace_id": "abc123"}
    out = redact_processor(None, "info", dict(event))
    assert out["user_email"] == "[REDACTED_EMAIL]" and out["password"] == "[REDACTED]"
    assert out["trace_id"] == "abc123"


def test_stdlib_logging_filter() -> None:
    record = logging.LogRecord(
        "adk", logging.INFO, __file__, 1, "user said %s", ("bob@x.io",), None
    )
    assert RedactingFilter().filter(record)
    assert record.getMessage() == "user said [REDACTED_EMAIL]"


def test_redacting_span_exporter() -> None:
    inner = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(RedactingSpanExporter(inner)))
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("s") as span:
        span.set_attribute("gen_ai.prompt", "email me at a@b.co")
        span.set_attribute("list", ["call 415-555-0134", "ok"])
        span.set_attribute("n", 5)
        span.add_event("e", {"msg": "card 4111 1111 1111 1111"})
    (s,) = inner.get_finished_spans()
    assert s.attributes["gen_ai.prompt"] == "email me at [REDACTED_EMAIL]"
    assert s.attributes["list"] == ("call [REDACTED_PHONE]", "ok") and s.attributes["n"] == 5
    assert s.events[0].attributes["msg"] == "card [REDACTED_CARD]"
    assert RedactingSpanExporter(inner).force_flush()


def test_redact_session_does_not_mutate_original() -> None:
    session = Session(
        id="s",
        app_name="a",
        user_id="u",
        events=[
            Event(
                author="user",
                content=types.Content(role="user", parts=[types.Part(text="I'm a@b.co")]),
            )
        ],
    )
    clone = redact_session(session)
    assert clone.events[0].content.parts[0].text == "I'm [REDACTED_EMAIL]"
    assert session.events[0].content.parts[0].text == "I'm a@b.co"


# ---- intended-action audit logging ------------------------------------------------


async def test_tool_intent_logged_before_execution_with_redacted_args() -> None:
    def decider(req: Any) -> Any:
        if not function_responses(req):
            return call("update_user_profile", household_size=2, pantry=["rice", "note: bob@x.io"])
        return text("saved")

    agent = build_root_agent(model=ScriptedLlm(model="fake", decider=decider))
    before_spans = len(finished_spans())
    runner = InMemoryRunner(app=App(name="intent", root_agent=agent, plugins=[MetricsPlugin()]))
    session = await runner.session_service.create_session(app_name="intent", user_id="u1")
    msg = types.Content(role="user", parts=[types.Part(text="save my profile")])
    with structlog.testing.capture_logs() as logs:
        _ = [
            e async for e in runner.run_async(user_id="u1", session_id=session.id, new_message=msg)
        ]
    events = [entry["event"] for entry in logs]
    assert "tool.intent" in events and "tool.call" in events
    assert events.index("tool.intent") < events.index("tool.call")
    intent = next(entry for entry in logs if entry["event"] == "tool.intent")
    assert intent["tool"] == "update_user_profile" and intent["side_effect"] is True
    assert intent["requires_confirmation"] is False
    assert "[REDACTED_EMAIL]" in str(intent["args"]) and "bob@x.io" not in str(intent["args"])
    span_events = [
        e for s in finished_spans()[before_spans:] for e in s.events if e.name == "tool.intent"
    ]
    assert span_events and span_events[0].attributes["tool"] == "update_user_profile"


async def test_intent_logged_even_when_guardrail_blocks() -> None:
    def decider(req: Any) -> Any:
        if not function_responses(req):
            return call("update_user_profile", pantry=["chicken"])
        return text("no")

    agent = build_root_agent(model=ScriptedLlm(model="fake", decider=decider))
    with structlog.testing.capture_logs() as logs:
        await run_agent(agent, "pantry", app_name="intent_blocked")
    names = [entry["event"] for entry in logs]
    assert "tool.intent" in names and "guardrail.blocked_tool_call" in names
