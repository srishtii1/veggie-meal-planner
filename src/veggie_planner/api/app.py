"""FastAPI application: REST chat API + A2A server over one ADK Runner."""

from __future__ import annotations

import contextlib
import warnings
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from google.adk.apps import App
from google.adk.flows.llm_flows.functions import REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
from google.adk.runners import Runner
from google.genai import types
from starlette.routing import Mount

from veggie_planner import __version__
from veggie_planner.agents import build_root_agent
from veggie_planner.api.schemas import (
    ChatRequest,
    ChatResponse,
    ConfirmRequest,
    HealthResponse,
    PendingConfirmation,
    SessionResponse,
    ToolCallSummary,
)
from veggie_planner.config import Settings, get_settings
from veggie_planner.data import load_catalog
from veggie_planner.memory import build_memory_service, build_session_service
from veggie_planner.observability import (
    MetricsPlugin,
    configure_logging,
    configure_telemetry,
    get_tracer,
    metrics_snapshot,
)
from veggie_planner.observability.redaction import redact_session, redact_value
from veggie_planner.storage import get_plan_store
from veggie_planner.tools.planning import CURRENT_PLAN_KEY

log = structlog.get_logger(__name__)


async def persist_memory(runner: Runner, *, user_id: str, session_id: str) -> None:
    """Background task: copy the (PII-redacted) session into long-term memory.

    Failures are logged, never raised -- memory is best-effort and must not affect chat.
    """
    if runner.memory_service is None:
        return
    try:
        session = await runner.session_service.get_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
        if session is None:
            return
        await runner.memory_service.add_session_to_memory(redact_session(session))
        log.info("memory.persisted", session_id=session_id, events=len(session.events))
    except Exception as exc:
        log.warning("memory.persist_failed", session_id=session_id, error=str(exc))


def _pending_from_call(confirmation_id: str, args: dict[str, Any]) -> PendingConfirmation:
    original = args.get("originalFunctionCall") or args.get("original_function_call") or {}
    conf = args.get("toolConfirmation") or args.get("tool_confirmation") or {}
    tool = str(original.get("name", "unknown"))
    tool_args = redact_value(dict(original.get("args") or {}))
    return PendingConfirmation(
        confirmation_id=confirmation_id,
        tool=tool,
        args=tool_args,
        hint=str(conf.get("hint", "")),
        summary=_describe(tool, tool_args),
    )


def _describe(tool: str, args: dict[str, Any]) -> str:
    if tool == "swap_meal":
        target = args.get("recipe_id") or "an automatically chosen recipe"
        limit = args.get("max_prep_time_min")
        extra = f" (max {limit} min)" if limit else ""
        return f"replace {args.get('day')} {args.get('slot')} with {target}{extra}"
    if tool == "rate_meal":
        return f"permanently ban recipe '{args.get('recipe_id')}' from future plans"
    if tool == "update_user_profile":
        changes = ", ".join(f"{k}={v}" for k, v in args.items())
        return f"update your profile ({changes}), which relaxes a safety constraint"
    return f"run {tool}"


def build_runner(settings: Settings, model: Any | None = None) -> Runner:
    app = App(
        name=settings.app_name,
        root_agent=build_root_agent(model=model),
        plugins=[MetricsPlugin()],
    )
    return Runner(
        app=app,
        session_service=build_session_service(settings),
        memory_service=build_memory_service(settings),
    )


def create_app(
    settings: Settings | None = None, *, model: Any | None = None, enable_a2a: bool = True
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    configure_telemetry(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runner = build_runner(settings, model=model)
        app.state.runner = runner
        app.state.settings = settings
        get_plan_store()  # fail fast if the plan DB path is unwritable
        log.info(
            "app.start", version=__version__, model=settings.model, backend=settings.session_backend
        )
        if enable_a2a:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from google.adk.a2a.utils.agent_to_a2a import to_a2a

                a2a_app = to_a2a(
                    runner.agent,  # type: ignore[arg-type]
                    runner=runner,
                    host=settings.public_host,
                    port=settings.port,
                    rpc_path="a2a",
                )
            app.router.routes.append(Mount("/", app=a2a_app))
            async with a2a_app.router.lifespan_context(a2a_app):
                yield
        else:
            yield
        await runner.close()

    app = FastAPI(
        title="Veggie Meal Planner",
        version=__version__,
        description="Vegetarian meal-planning agent (Google ADK). REST at /chat, A2A at /a2a.",
        lifespan=lifespan,
    )

    def runner_dep(request: Request) -> Runner:
        return request.app.state.runner  # type: ignore[no-any-return]

    @app.get("/healthz", response_model=HealthResponse, tags=["ops"])
    async def healthz() -> HealthResponse:
        return HealthResponse(
            status="ok", version=__version__, model=settings.model, recipes=len(load_catalog())
        )

    @app.get("/metrics", tags=["ops"])
    async def metrics() -> JSONResponse:
        return JSONResponse(metrics_snapshot())

    async def run_turn(
        runner: Runner,
        *,
        user_id: str,
        session_id: str,
        message: types.Content,
        span_name: str,
        background: BackgroundTasks,
    ) -> ChatResponse:
        """Run one agent turn, collect reply / tool trajectory / pending confirmations."""
        structlog.contextvars.bind_contextvars(session_id=session_id, user_id=user_id)
        with get_tracer().start_as_current_span(span_name) as span:
            span.set_attribute("session.id", session_id)
            span.set_attribute("user.id", user_id)
            reply_parts: list[str] = []
            tool_calls: list[ToolCallSummary] = []
            pending: dict[str, PendingConfirmation] = {}
            async for event in runner.run_async(
                user_id=user_id, session_id=session_id, new_message=message
            ):
                for part in event.content.parts if event.content and event.content.parts else []:
                    fc = part.function_call
                    if fc and fc.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME and fc.id:
                        pending[fc.id] = _pending_from_call(fc.id, dict(fc.args or {}))
                    elif fc and fc.name:
                        tool_calls.append(ToolCallSummary(agent=event.author, tool=fc.name))
                    fr = part.function_response
                    if fr and fr.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME and fr.id:
                        pending.pop(fr.id, None)
                    elif fr:
                        resp = fr.response
                        status = resp.get("status") if isinstance(resp, dict) else None
                        if isinstance(resp, dict) and "requires confirmation" in str(
                            resp.get("error", "")
                        ):
                            status = "awaiting_confirmation"
                        for tc in reversed(tool_calls):
                            if tc.tool == fr.name and tc.status is None:
                                tc.status = str(status) if status is not None else "ok"
                                break
                if event.is_final_response() and event.content and event.content.parts:
                    reply_parts.extend(p.text for p in event.content.parts if p.text)
            span.set_attribute("hitl.pending_confirmations", len(pending))
            trace_id = format(span.get_span_context().trace_id, "032x")

        updated = await runner.session_service.get_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
        # Long-term memory ingestion is slow (and remote for Vertex Memory Bank): never
        # make the user wait for it. Runs after the response is sent.
        background.add_task(persist_memory, runner, user_id=user_id, session_id=session_id)
        structlog.contextvars.unbind_contextvars("session_id", "user_id")
        reply = "\n".join(reply_parts).strip()
        if pending and not reply:
            reply = "I need your approval before I continue: " + "; ".join(
                p.summary for p in pending.values()
            )
        return ChatResponse(
            session_id=session_id,
            user_id=user_id,
            reply=reply,
            tool_calls=tool_calls,
            pending_confirmations=list(pending.values()),
            current_plan_id=(updated.state.get(CURRENT_PLAN_KEY) if updated else None),
            trace_id=trace_id,
        )

    async def resolve_session(runner: Runner, user_id: str, session_id: str | None) -> str:
        if session_id:
            session = await runner.session_service.get_session(
                app_name=runner.app_name, user_id=user_id, session_id=session_id
            )
            if session is None:
                raise HTTPException(status_code=404, detail="session not found")
            return session.id
        created = await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id
        )
        return created.id

    @app.post("/chat", response_model=ChatResponse, tags=["agent"])
    async def chat(
        body: ChatRequest,
        background: BackgroundTasks,
        runner: Runner = Depends(runner_dep),  # noqa: B008
    ) -> ChatResponse:
        """Send a user message. If an action needs approval, ``pending_confirmations`` is set
        and the action has **not** run yet; answer it with ``POST /chat/confirm``."""
        session_id = await resolve_session(runner, body.user_id, body.session_id)
        message = types.Content(role="user", parts=[types.Part(text=body.message)])
        return await run_turn(
            runner,
            user_id=body.user_id,
            session_id=session_id,
            message=message,
            span_name="POST /chat",
            background=background,
        )

    @app.post("/chat/confirm", response_model=ChatResponse, tags=["agent"])
    async def confirm(
        body: ConfirmRequest,
        background: BackgroundTasks,
        runner: Runner = Depends(runner_dep),  # noqa: B008
    ) -> ChatResponse:
        """Approve or reject a pending action (human-in-the-loop)."""
        session_id = await resolve_session(runner, body.user_id, body.session_id)
        log.info(
            "hitl.decision",
            confirmation_id=body.confirmation_id,
            approved=body.approved,
            session_id=session_id,
        )
        response = types.FunctionResponse(
            id=body.confirmation_id,
            name=REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
            response={"confirmed": body.approved, "payload": {"reason": body.reason}},
        )
        message = types.Content(role="user", parts=[types.Part(function_response=response)])
        return await run_turn(
            runner,
            user_id=body.user_id,
            session_id=session_id,
            message=message,
            span_name="POST /chat/confirm",
            background=background,
        )

    @app.get("/sessions/{session_id}", response_model=SessionResponse, tags=["agent"])
    async def get_session(
        session_id: str,
        user_id: str = "demo-user",
        runner: Runner = Depends(runner_dep),  # noqa: B008
    ) -> SessionResponse:
        session = await runner.session_service.get_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return SessionResponse(
            session_id=session.id,
            user_id=session.user_id,
            state=dict(session.state),
            event_count=len(session.events),
        )

    @app.get("/plans/{plan_id}", tags=["plans"])
    async def get_plan(plan_id: str) -> dict[str, Any]:
        plan = get_plan_store().get(plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="plan not found")
        return plan.model_dump(mode="json")

    @app.get("/recipes/{recipe_id}", tags=["plans"])
    async def get_recipe(recipe_id: str) -> dict[str, Any]:
        recipe = load_catalog().get(recipe_id)
        if recipe is None:
            raise HTTPException(status_code=404, detail="recipe not found")
        return recipe.model_dump(mode="json")

    return app


def main() -> None:  # pragma: no cover - entrypoint
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
