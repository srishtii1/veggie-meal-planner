"""OpenTelemetry + structlog setup.

ADK emits spans for every agent invocation, model call and tool call through the global
tracer provider. We install that provider here (OTLP/HTTP when
``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, e.g. to Jaeger; console exporter with
``VMP_TRACE_CONSOLE=true``) and add a custom span per REST
request so a trace looks like: ``POST /chat -> invoke_agent -> call_llm -> execute_tool``.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    InMemoryMetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from veggie_planner import __version__
from veggie_planner.config import Settings, get_settings
from veggie_planner.observability.redaction import (
    RedactingFilter,
    RedactingSpanExporter,
    redact_processor,
)

_configured = False
_in_memory_spans: InMemorySpanExporter | None = None
_metric_reader: InMemoryMetricReader | None = None


def _add_trace_context(
    _: Any, __: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def configure_logging(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    level = logging.getLevelName(settings.log_level.upper())
    logging.basicConfig(level=level, stream=sys.stdout, format="%(message)s")
    if settings.redact_pii:
        for handler in logging.getLogger().handlers:
            if not any(isinstance(f, RedactingFilter) for f in handler.filters):
                handler.addFilter(RedactingFilter())
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _add_trace_context,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            *([redact_processor] if settings.redact_pii else []),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=False,
    )


def configure_telemetry(settings: Settings | None = None, *, in_memory: bool = False) -> None:
    """Install tracer/meter providers once. ``in_memory=True`` is for tests."""
    global _configured, _in_memory_spans, _metric_reader
    if _configured:
        return
    settings = settings or get_settings()
    # ADK puts prompts/responses/tool args into span attributes unless told otherwise.
    # Privacy by default: off unless VMP_CAPTURE_CONTENT_IN_SPANS=true. Anything that is
    # captured is still passed through the redacting exporter below.
    os.environ.setdefault(
        "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS",
        "true" if settings.capture_content_in_spans else "false",
    )

    def wrap(exporter: Any) -> Any:
        return RedactingSpanExporter(exporter) if settings.redact_pii else exporter

    resource = Resource.create(
        {
            SERVICE_NAME: os.getenv("OTEL_SERVICE_NAME", "veggie-meal-planner"),
            SERVICE_VERSION: __version__,
        }
    )
    tracer_provider = TracerProvider(resource=resource)
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if in_memory:
        _in_memory_spans = InMemorySpanExporter()
        tracer_provider.add_span_processor(SimpleSpanProcessor(wrap(_in_memory_spans)))
    else:
        if endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            tracer_provider.add_span_processor(BatchSpanProcessor(wrap(OTLPSpanExporter())))
        if settings.trace_console:
            tracer_provider.add_span_processor(
                BatchSpanProcessor(wrap(ConsoleSpanExporter(out=sys.stderr)))
            )
    trace.set_tracer_provider(tracer_provider)

    readers: list[Any] = []
    _metric_reader = InMemoryMetricReader()
    readers.append(_metric_reader)
    if not in_memory and endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

        readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))
    elif not in_memory and settings.trace_console:
        readers.append(PeriodicExportingMetricReader(ConsoleMetricExporter(out=sys.stderr)))
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))
    _configured = True
    structlog.get_logger(__name__).info(
        "telemetry.configured",
        otlp_endpoint=endpoint,
        console=settings.trace_console,
        redact_pii=settings.redact_pii,
        capture_content=settings.capture_content_in_spans,
    )


def get_tracer() -> trace.Tracer:
    return trace.get_tracer("veggie_planner", __version__)


def finished_spans() -> list[Any]:
    """Spans captured by the in-memory exporter (tests only)."""
    return list(_in_memory_spans.get_finished_spans()) if _in_memory_spans else []


def metrics_snapshot() -> dict[str, Any]:
    """Current metric values as a plain dict (served by ``GET /metrics``)."""
    if _metric_reader is None:
        return {}
    data = _metric_reader.get_metrics_data()
    out: dict[str, Any] = {}
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                points = []
                for dp in metric.data.data_points:
                    value: Any = getattr(dp, "value", None)
                    if value is None and hasattr(dp, "sum"):
                        value = {k: getattr(dp, k, None) for k in ("sum", "count", "min", "max")}
                    points.append({"attributes": dict(dp.attributes or {}), "value": value})
                out[metric.name] = {
                    "unit": metric.unit,
                    "description": metric.description,
                    "points": points,
                }
    return out
