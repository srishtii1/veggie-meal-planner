"""PII redaction for logs, traces and long-term memory.

Regex-based, conservative scrubbing of the identifiers most likely to show up in a
meal-planning conversation (people paste contact details, addresses, card numbers for
"budget" questions, or API keys by accident). Applied to:

* every structlog event (``redact_processor``) and stdlib log record (``RedactingFilter``),
* every exported OpenTelemetry span / event attribute (``RedactingSpanExporter``),
* session text before it is written to the ``MemoryService`` (``redact_session``).

The model itself still sees the user's original message for the current turn; redaction
is about what we *persist* and *emit*.
"""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # Secrets first so their digits are not mistaken for phone/card numbers.
    (
        "SECRET",
        re.compile(
            r"\b(?:AIza[0-9A-Za-z_\-]{30,}|AQ\.[0-9A-Za-z_\-]{20,}|gh[opsu]_[0-9A-Za-z]{20,}"
            r"|sk-[0-9A-Za-z]{20,}|ya29\.[0-9A-Za-z_\-]{20,})"
        ),
    ),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    (
        "PHONE",
        re.compile(
            r"(?<!\w)(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)|\d{2,4})[\s.-]?\d{3,4}[\s.-]?\d{3,4}(?!\w)"
        ),
    ),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    (
        "ADDRESS",
        re.compile(
            r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s){1,3}(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Drive|Dr|"
            r"Boulevard|Blvd|Court|Ct|Way)\b\.?"
        ),
    ),
)

# Keys whose values are always fully masked, whatever they contain.
_SENSITIVE_KEYS = frozenset(
    {"api_key", "apikey", "authorization", "password", "token", "secret", "google_api_key"}
)


def redact_text(text: str) -> str:
    """Replace PII in ``text`` with ``[REDACTED_<KIND>]`` placeholders."""
    out = text
    for kind, pattern in _PATTERNS:
        out = pattern.sub(f"[REDACTED_{kind}]", out)
    return out


def redact_value(value: Any) -> Any:
    """Recursively redact strings inside dicts / lists / tuples."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            k: ("[REDACTED]" if str(k).lower() in _SENSITIVE_KEYS else redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(redact_value(v) for v in value)
    return value


# ---- structlog / stdlib logging --------------------------------------------------


def redact_processor(
    _: Any, __: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: scrub every field except correlation ids."""
    for key, value in list(event_dict.items()):
        if key in {"trace_id", "span_id", "session_id", "invocation_id", "timestamp", "level"}:
            continue
        event_dict[key] = "[REDACTED]" if key.lower() in _SENSITIVE_KEYS else redact_value(value)
    return event_dict


class RedactingFilter(logging.Filter):
    """stdlib logging filter (covers ADK / uvicorn / httpx loggers)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record, leave untouched
            return True
        redacted = redact_text(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


# ---- OpenTelemetry ------------------------------------------------------------------


def _redact_attrs(attrs: Mapping[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (attrs or {}).items():
        if isinstance(v, str):
            out[k] = redact_text(v)
        elif isinstance(v, Sequence) and not isinstance(v, bytes | str):
            out[k] = tuple(redact_text(x) if isinstance(x, str) else x for x in v)
        else:
            out[k] = v
    return out


def redact_span(span: ReadableSpan) -> ReadableSpan:
    """Return a copy of ``span`` with string attributes (span + events) redacted."""
    events = []
    for e in span.events:
        clone = copy.copy(e)
        object.__setattr__(clone, "_attributes", _redact_attrs(e.attributes))
        events.append(clone)
    scope = span.instrumentation_scope or InstrumentationScope("unknown")
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=_redact_attrs(span.attributes),
        events=events,
        links=span.links,
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=scope,
    )


class RedactingSpanExporter(SpanExporter):
    """Wraps any exporter and redacts PII before spans leave the process."""

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._inner.export([redact_span(s) for s in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


# ---- Memory ---------------------------------------------------------------------------


def redact_session(session: Any) -> Any:
    """Deep-copy an ADK ``Session`` with all text parts redacted (for MemoryService)."""
    clone = session.model_copy(deep=True)
    for event in clone.events:
        content = getattr(event, "content", None)
        for part in content.parts if content and content.parts else []:
            if part.text:
                part.text = redact_text(part.text)
    return clone
