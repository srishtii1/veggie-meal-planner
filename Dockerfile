# syntax=docker/dockerfile:1.7
# ---- builder -----------------------------------------------------------------
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/
ARG UV_DEFAULT_INDEX=https://pypi.org/simple
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX}
WORKDIR /app
COPY pyproject.toml uv.lock ./
# Re-resolve the lock against the build's index (public PyPI by default) keeping the pins.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv lock && uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# ---- runtime -----------------------------------------------------------------
FROM python:3.12-slim AS runtime
RUN groupadd --system app && useradd --system --gid app --home /app app
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src
RUN mkdir -p /app/data && chown app:app /app/data
USER app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    VMP_HOST=0.0.0.0 \
    VMP_PORT=8080 \
    VMP_DB_URL=sqlite+aiosqlite:////app/data/veggie_planner.db \
    VMP_PLAN_DB_PATH=/app/data/veggie_planner_plans.db
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)"
CMD ["python", "-m", "veggie_planner"]
