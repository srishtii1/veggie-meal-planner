"""Pre-execution audit trail for tool calls ("what did the agent *intend* to do?").

``log_tool_intent`` is installed as the **first** ``before_tool_callback`` on every agent, so
it runs before guardrails and before ADK's confirmation gate. It therefore records attempts
that are later blocked, paused for a human, or rejected — not just successful outcomes,
which ``MetricsPlugin.after_tool_callback`` records as ``tool.call``.

Each intent produces:
* a structlog ``tool.intent`` line (redacted args, side-effect flag, confirmation state),
* a ``tool.intent`` event on the current OTel span (visible in Jaeger),
* an ``agent.tool_intents`` counter increment (and ``agent.confirmations`` on decisions).
"""

from __future__ import annotations

from typing import Any

import structlog
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from opentelemetry import metrics, trace

from veggie_planner.observability.redaction import redact_value

log = structlog.get_logger("veggie_planner.audit")

# Tools that change persisted state (plans, profile, ratings). Everything else is read-only.
SIDE_EFFECT_TOOLS = frozenset({"draft_meal_plan", "swap_meal", "update_user_profile", "rate_meal"})

_meter = metrics.get_meter("veggie_planner.audit")
_intents = _meter.create_counter(
    "agent.tool_intents", description="Tool calls the model attempted (pre-execution)"
)
_confirmations = _meter.create_counter(
    "agent.confirmations", description="Human-in-the-loop confirmation outcomes"
)


async def _needs_confirmation(tool: BaseTool, args: dict[str, Any], ctx: ToolContext) -> bool:
    check = getattr(tool, "check_require_confirmation", None)
    if check is None:
        return False
    try:
        return bool(await check(args, ctx))
    except Exception:  # auditing must never break the tool call
        return False


async def log_tool_intent(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> dict[str, Any] | None:
    """Record the intended action. Always returns ``None`` (never alters the call)."""
    side_effect = tool.name in SIDE_EFFECT_TOOLS
    needs_confirmation = await _needs_confirmation(tool, args, tool_context)
    confirmation = getattr(tool_context, "tool_confirmation", None)
    decision = (
        None if confirmation is None else ("approved" if confirmation.confirmed else "rejected")
    )
    safe_args = redact_value(dict(args))
    agent = getattr(tool_context, "agent_name", None) or "unknown"
    attrs = {"tool": tool.name, "agent": agent}
    _intents.add(1, {**attrs, "side_effect": side_effect})
    if decision:
        _confirmations.add(1, {**attrs, "decision": decision})
    log.info(
        "tool.intent",
        tool=tool.name,
        agent=agent,
        function_call_id=getattr(tool_context, "function_call_id", None),
        args=safe_args,
        side_effect=side_effect,
        requires_confirmation=needs_confirmation,
        confirmation=decision,
    )
    trace.get_current_span().add_event(
        "tool.intent",
        {
            "tool": tool.name,
            "agent": agent,
            "side_effect": side_effect,
            "requires_confirmation": needs_confirmation,
            "confirmation": decision or "",
            "args": str(safe_args)[:1000],
        },
    )
    return None
