"""FastAPI application: REST chat API + A2A server over one ADK Runner."""

from __future__ import annotations

import contextlib
import warnings
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from google.adk.apps import App
from google.adk.runners import Runner
from google.genai import types
from starlette.routing import Mount

from veggie_planner import __version__
from veggie_planner.agents import build_root_agent
from veggie_planner.api.schemas import (
    ChatRequest,
    ChatResponse,
    HealthResponse,
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
from veggie_planner.storage import get_plan_store
from veggie_planner.tools.planning import CURRENT_PLAN_KEY

log = structlog.get_logger(__name__)


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

    @app.post("/chat", response_model=ChatResponse, tags=["agent"])
    async def chat(body: ChatRequest, runner: Runner = Depends(runner_dep)) -> ChatResponse:  # noqa: B008
        session_service = runner.session_service
        session = None
        if body.session_id:
            session = await session_service.get_session(
                app_name=runner.app_name, user_id=body.user_id, session_id=body.session_id
            )
            if session is None:
                raise HTTPException(status_code=404, detail="session not found")
        else:
            session = await session_service.create_session(
                app_name=runner.app_name, user_id=body.user_id
            )

        structlog.contextvars.bind_contextvars(session_id=session.id, user_id=body.user_id)
        with get_tracer().start_as_current_span("POST /chat") as span:
            span.set_attribute("session.id", session.id)
            span.set_attribute("user.id", body.user_id)
            reply_parts: list[str] = []
            tool_calls: list[ToolCallSummary] = []
            message = types.Content(role="user", parts=[types.Part(text=body.message)])
            async for event in runner.run_async(
                user_id=body.user_id, session_id=session.id, new_message=message
            ):
                for part in event.content.parts if event.content and event.content.parts else []:
                    if part.function_call and part.function_call.name:
                        tool_calls.append(
                            ToolCallSummary(agent=event.author, tool=part.function_call.name)
                        )
                    if part.function_response:
                        resp = part.function_response.response
                        status = resp.get("status") if isinstance(resp, dict) else None
                        for tc in reversed(tool_calls):
                            if tc.tool == part.function_response.name and tc.status is None:
                                tc.status = str(status) if status is not None else "ok"
                                break
                if event.is_final_response() and event.content and event.content.parts:
                    reply_parts.extend(p.text for p in event.content.parts if p.text)
            trace_id = format(span.get_span_context().trace_id, "032x")

        updated = await session_service.get_session(
            app_name=runner.app_name, user_id=body.user_id, session_id=session.id
        )
        if updated is not None and runner.memory_service is not None:
            await runner.memory_service.add_session_to_memory(updated)
        structlog.contextvars.unbind_contextvars("session_id", "user_id")
        return ChatResponse(
            session_id=session.id,
            user_id=body.user_id,
            reply="\n".join(reply_parts).strip(),
            tool_calls=tool_calls,
            current_plan_id=(updated.state.get(CURRENT_PLAN_KEY) if updated else None),
            trace_id=trace_id,
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
