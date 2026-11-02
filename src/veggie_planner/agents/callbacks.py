"""Guardrail callbacks attached to the agents.

* ``block_non_vegetarian_tool_args`` -- ``before_tool_callback``: any tool argument that
  mentions a non-vegetarian ingredient is rejected before the tool runs. The model cannot
  write "chicken" into the pantry, dislikes, or a recipe validation bypass.
* ``scrub_prompt_injection`` -- ``before_model_callback``: strips well-known jailbreak
  phrases from tool results / user text before they reach the model and refuses outright
  non-vegetarian requests deterministically.
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from veggie_planner.guardrails.vegetarian import find_violations

log = structlog.get_logger(__name__)

# Tools whose arguments legitimately contain non-veg words (they exist to detect them).
_VALIDATION_TOOLS = frozenset({"validate_vegetarian", "load_memory", "preload_memory"})

_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore (all )?(previous|prior|above) instructions?",
        r"disregard (all )?(previous|prior|above) (instructions?|rules?)",
        r"you are now (an?|the) [^.\n]{0,60}",
        r"system prompt:?",
        r"developer mode",
        r"jailbreak",
    )
]

_EXPLICIT_NON_VEG_REQUEST = re.compile(
    r"\b(add|include|put|want|give me|make|cook|plan|with|swap (?:\w+ ){0,3}for)\b[^.\n]{0,40}\b"
    r"(chicken|beef|pork|lamb|mutton|bacon|ham|turkey|fish|salmon|tuna|shrimp|prawns?|steak|meat)\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(
    r"\b(no|not|without|avoid|never|don'?t|allerg\w*|vegetarian|veg)\b", re.IGNORECASE
)

REFUSAL_TEXT = (
    "I'm a vegetarian-only meal planner, so I can't add meat, poultry or seafood to a plan. "
    "I'd be happy to suggest a protein-rich vegetarian alternative instead — for example "
    "paneer, tofu, tempeh, lentils or chickpeas. Want me to plan with one of those?"
)


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _walk_strings(v)]
    if isinstance(value, list | tuple | set):
        return [s for v in value for s in _walk_strings(v)]
    return []


def block_non_vegetarian_tool_args(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> dict[str, Any] | None:
    """Reject any tool call whose string arguments contain non-vegetarian terms."""
    if tool.name in _VALIDATION_TOOLS:
        return None
    violations = sorted({v for s in _walk_strings(args) for v in find_violations(s)})
    if not violations:
        return None
    log.warning("guardrail.blocked_tool_call", tool=tool.name, violations=violations)
    return {
        "status": "error",
        "error": (
            f"Blocked: '{', '.join(violations)}' is not vegetarian. This planner is "
            "vegetarian-only; tell the user and offer a vegetarian alternative."
        ),
        "guardrail": "vegetarian_only",
    }


def _scrub(text: str) -> str:
    cleaned = text
    for pat in _INJECTION_PATTERNS:
        cleaned = pat.sub("[removed]", cleaned)
    return cleaned


def scrub_prompt_injection(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    """Sanitise tool outputs and refuse explicit non-vegetarian requests before the model runs."""
    last_user_text = ""
    for content in llm_request.contents or []:
        for part in content.parts or []:
            if part.function_response and isinstance(part.function_response.response, dict):
                resp = part.function_response.response
                for key, value in list(resp.items()):
                    if isinstance(value, str) and value != _scrub(value):
                        resp[key] = _scrub(value)
                        log.warning("guardrail.scrubbed_tool_output", key=key)
            if part.text and content.role == "user":
                last_user_text = part.text
    if last_user_text:
        m = _EXPLICIT_NON_VEG_REQUEST.search(last_user_text)
        if m and not _NEGATION.search(last_user_text):
            log.warning("guardrail.refused_non_veg_request", agent=callback_context.agent_name)
            return LlmResponse(content=types.ModelContent(parts=[types.Part(text=REFUSAL_TEXT)]))
    return None
