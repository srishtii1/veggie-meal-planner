"""Deploy the planner to Vertex AI Agent Engine.

Usage (needs ``uv sync --group deploy`` and ADC credentials)::

    GOOGLE_CLOUD_PROJECT=my-proj GOOGLE_CLOUD_LOCATION=us-central1 \\
    AGENT_ENGINE_STAGING_BUCKET=gs://my-bucket python deployment/deploy_agent_engine.py

Agent Engine provides managed sessions and memory; set ``VMP_SESSION_BACKEND=vertex`` in
the deployed environment so the runner uses ``VertexAiSessionService`` /
``VertexAiMemoryBankService`` instead of SQLite.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    bucket = os.environ.get("AGENT_ENGINE_STAGING_BUCKET")
    if not project or not bucket:
        print("GOOGLE_CLOUD_PROJECT and AGENT_ENGINE_STAGING_BUCKET are required", file=sys.stderr)
        return 2

    import vertexai
    from vertexai import agent_engines
    from vertexai.preview.reasoning_engines import AdkApp

    from veggie_planner.agents import root_agent

    vertexai.init(project=project, location=location, staging_bucket=bucket)
    app = AdkApp(agent=root_agent, enable_tracing=True)
    remote = agent_engines.create(
        agent_engine=app,
        display_name="veggie-meal-planner",
        description="Vegetarian meal-planning agent (Google ADK)",
        requirements=["veggie-meal-planner @ ."],
        extra_packages=["src/veggie_planner"],
        env_vars={
            "VMP_SESSION_BACKEND": "vertex",
            "VMP_MODEL": os.environ.get("VMP_MODEL", "gemini-2.5-flash"),
        },
    )
    print(f"Deployed: {remote.resource_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
