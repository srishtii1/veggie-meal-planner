"""ADK plugin recording tokens, latency and tool-call counts as OTel metrics + structlog."""

from __future__ import annotations

import time
from typing import Any

import structlog
from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from opentelemetry import metrics

log = structlog.get_logger("veggie_planner.metrics")


class MetricsPlugin(BasePlugin):
    """App-wide hooks: every agent, model and tool call under the Runner is counted."""

    def __init__(self) -> None:
        super().__init__(name="veggie_metrics")
        meter = metrics.get_meter("veggie_planner")
        self.tool_calls = meter.create_counter("agent.tool_calls", description="Tool invocations")
        self.tool_errors = meter.create_counter(
            "agent.tool_errors", description="Tool results with status=error"
        )
        self.llm_calls = meter.create_counter("agent.llm_calls", description="Model invocations")
        self.tokens = meter.create_counter(
            "agent.tokens", unit="token", description="Prompt/response tokens"
        )
        self.tool_latency = meter.create_histogram("agent.tool_latency", unit="ms")
        self.llm_latency = meter.create_histogram("agent.llm_latency", unit="ms")
        self.guardrail_blocks = meter.create_counter("agent.guardrail_blocks")
        self._tool_started: dict[str, float] = {}
        self._llm_started: dict[str, float] = {}

    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> None:
        structlog.contextvars.bind_contextvars(
            session_id=callback_context.session.id, invocation_id=callback_context.invocation_id
        )
        log.info("agent.start", agent=agent.name)
        return None

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> None:
        self._llm_started[callback_context.invocation_id + callback_context.agent_name] = (
            time.perf_counter()
        )
        return None

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> None:
        attrs = {
            "agent": callback_context.agent_name,
            "model": str(llm_response.model_version or ""),
        }
        self.llm_calls.add(1, attrs)
        key = callback_context.invocation_id + callback_context.agent_name
        started = self._llm_started.pop(key, None)
        latency_ms = (time.perf_counter() - started) * 1000 if started else None
        if latency_ms is not None:
            self.llm_latency.record(latency_ms, attrs)
        usage = llm_response.usage_metadata
        prompt_t = getattr(usage, "prompt_token_count", None) or 0
        out_t = getattr(usage, "candidates_token_count", None) or 0
        if prompt_t:
            self.tokens.add(prompt_t, {**attrs, "kind": "prompt"})
        if out_t:
            self.tokens.add(out_t, {**attrs, "kind": "response"})
        log.info(
            "llm.call",
            agent=callback_context.agent_name,
            prompt_tokens=prompt_t,
            response_tokens=out_t,
            latency_ms=round(latency_ms, 1) if latency_ms else None,
        )
        return None

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> None:
        self._tool_started[tool_context.function_call_id or tool.name] = time.perf_counter()
        return None

    async def after_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext, result: Any
    ) -> None:
        attrs = {"tool": tool.name, "agent": tool_context.agent_name}
        self.tool_calls.add(1, attrs)
        started = self._tool_started.pop(tool_context.function_call_id or tool.name, None)
        latency_ms = (time.perf_counter() - started) * 1000 if started else None
        if latency_ms is not None:
            self.tool_latency.record(latency_ms, attrs)
        status = result.get("status") if isinstance(result, dict) else None
        if status == "error":
            self.tool_errors.add(1, attrs)
        if isinstance(result, dict) and result.get("guardrail"):
            self.guardrail_blocks.add(1, {**attrs, "guardrail": str(result["guardrail"])})
        log.info(
            "tool.call",
            tool=tool.name,
            agent=tool_context.agent_name,
            status=status,
            latency_ms=round(latency_ms, 1) if latency_ms else None,
        )
        return None
