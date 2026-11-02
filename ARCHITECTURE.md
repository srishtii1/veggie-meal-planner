# Architecture

This document explains *why* the planner is built the way it is, following the three layers
from the Google "Agents" whitepaper — **model**, **tools**, **orchestration** — plus the
production concerns around them.

## 1. Model layer

- Default `gemini-2.5-flash` for every agent (`VMP_MODEL`); the planner can be pointed at
  a stronger model independently (`VMP_PLANNER_MODEL`) because it does the most reasoning.
- The model never does arithmetic or compliance checks. Every number the user sees
  (calories, protein, cost, quantities) comes from `planning_engine.py`.
- All prompts live in `agents/prompts.py` and share one `VEGETARIAN_POLICY` block
  (vegetarian-only, medical disclaimer, ignore injected instructions).

## 2. Tool layer

Fourteen function tools, grouped by concern:

| Group | Tools | Notes |
|---|---|---|
| Profile | `get_user_profile`, `update_user_profile`, `rate_meal`, `list_diet_options` | Read/write `user:profile`, `user:ratings` |
| Recipes | `search_recipes`, `get_recipe`, `validate_vegetarian` | Search auto-applies the profile's hard filters |
| Planning | `draft_meal_plan`, `evaluate_meal_plan`, `get_meal_plan`, `swap_meal`, `compute_nutrition`, `estimate_cost` | Plans persisted in SQLite, id kept in session state |
| Grocery | `build_grocery_list` | Aggregates × servings, subtracts pantry, groups by aisle |

Design rules (enforced by tests):

1. **Typed + documented.** Signature and docstring *are* the schema the model sees.
2. **Structured results.** Always `{"status": "ok" | "error", ...}`; never raise for
   user-facing problems. The `error` text tells the model what to ask the user.
3. **Deterministic & idempotent.** Same inputs → same plan (ties broken by recipe id).
4. **Side effects are explicit.** Only `draft_meal_plan`, `swap_meal`, `update_user_profile`
   and `rate_meal` write; everything else is read-only.
5. **Context-lean.** Tools return `Recipe.summary()` (13 short fields) — full ingredient and
   instruction bodies only through `get_recipe`, on demand.

### Hard vegetarian guardrail

`guardrails/vegetarian.py` is a word-boundary deny-list with an allow-list for look-alikes
("oyster mushroom", "beefsteak tomato", "eggplant"). It is applied in four places so no
single layer is load-bearing:

1. **Catalog load** — a non-veg seed recipe raises and fails CI.
2. **`before_tool_callback`** — any tool argument containing a deny-term is rejected before
   the tool runs (e.g. `update_user_profile(pantry=["chicken"])`).
3. **`before_model_callback`** — explicit "add chicken…" requests get a deterministic refusal
   without a model round-trip; tool outputs are scrubbed for injection phrases.
4. **Critic** — `evaluate_meal_plan` re-validates every planned recipe.

## 3. Orchestration layer

```
meal_planner_coordinator (LlmAgent, chat)
├── profile / recipe / swap / rating / load_memory tools
├── AgentTool(planning_pipeline: SequentialAgent)
│     ├── planning_loop: LoopAgent(max_iterations=3)
│     │     ├── planner (LlmAgent)  → draft_meal_plan | swap_meal | search_recipes
│     │     └── critic  (LlmAgent)  → evaluate_meal_plan → exit_loop when clean
│     └── presenter (BaseAgent)     → deterministic markdown table of the plan
└── AgentTool(grocery_agent: LlmAgent) → build_grocery_list, estimate_cost
```

### The reasoning loop (plan → act → observe)

1. **Coordinator** reads the profile (`get_user_profile`). If `missing_essentials` is
   non-empty it asks one concise clarifying question instead of guessing.
2. It delegates to `planning_pipeline` with a natural-language request.
3. **Planner** calls `draft_meal_plan`, whose response already includes an evaluation.
4. **Critic** calls `evaluate_meal_plan`. If it passes it calls `exit_loop` (escalate);
   otherwise it writes numbered fixes to `critic_feedback` in state.
5. **Planner** (next iteration) reads `{critic_feedback?}` from its instruction template and
   applies targeted `swap_meal` calls — it never redrafts, so good meals are preserved.
6. **Presenter** renders the saved plan from the store (no LLM call), which becomes the
   `AgentTool` result the coordinator relays.

Why `AgentTool` rather than `sub_agents` transfer: the user keeps talking to one agent, the
nested run's state deltas (`current_plan_id`, `user:plan_ids`) flow back automatically, and
the result is a single string the coordinator can summarise.

Why `LoopAgent`/`SequentialAgent` although deprecated: ADK's newer `Workflow` graph API is
the recommended replacement, but a `Workflow` is not a `BaseAgent`, so it cannot be wrapped in
an `AgentTool` or used as a sub-agent today. The loop is small and the migration is
mechanical once that lands.

### Context engineering

| Scope | Key | Content | Lifetime |
|---|---|---|---|
| user | `user:profile` | `UserProfile` JSON | across sessions |
| user | `user:ratings`, `user:plan_ids` | feedback, history | across sessions |
| session | `current_plan_id` | id only — the plan body lives in SQLite | session |
| session | `planner_summary`, `critic_feedback` | loop hand-off | session |
| memory | `MemoryService` | past sessions, searchable via `load_memory` | long-term |

The prompt therefore carries the profile (≈300 tokens) and at most a plan *id*; the presenter
and tools fetch the bodies.

## 4. Interfaces

- **REST** — `POST /chat` (`message`, `user_id`, optional `session_id`) returns the reply,
  tool-call trajectory, `current_plan_id` and `trace_id`. `GET /sessions/{id}`,
  `GET /plans/{id}`, `GET /recipes/{id}`, `GET /healthz`, `GET /metrics`, OpenAPI at `/docs`.
- **A2A** — `to_a2a(root_agent, runner=...)` mounted under `/a2a`; Agent Card at
  `/a2a/.well-known/agent-card.json` lists every tool as a skill. Both surfaces share one
  `Runner`, so a session started over REST is visible to A2A and vice versa.
- **ADK dev UI** — `adk web src` for tracing individual turns during development.

## 5. Observability

- `configure_telemetry()` installs a `TracerProvider`/`MeterProvider` (console by default,
  OTLP/HTTP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set). ADK then emits
  `invoke_agent` → `call_llm` / `execute_tool` spans with GenAI semantic conventions; the API
  adds a parent `POST /chat` span so a whole turn is one trace.
- `MetricsPlugin` (an ADK `BasePlugin`, so it covers nested `AgentTool` runs) records
  `agent.llm_calls`, `agent.tokens{kind}`, `agent.llm_latency`, `agent.tool_calls{tool}`,
  `agent.tool_errors`, `agent.tool_latency`, `agent.guardrail_blocks`. `GET /metrics` dumps
  the in-memory reader for quick inspection.
- structlog JSON logs carry `trace_id`, `span_id`, `session_id`, `invocation_id`.

## 6. Infrastructure

- `uv` with a committed lockfile; `ruff` + `mypy --strict` gate CI.
- Multi-stage, non-root Docker image with a health check; `docker compose` adds Jaeger.
- GitHub Actions: `ci.yml` (lint → tests on 3.12/3.13 → image build + container smoke test),
  `release.yml` (semver tag → GHCR + GitHub release), `deploy.yml` (manual → Vertex AI Agent
  Engine, with `VertexAiSessionService`/`VertexAiMemoryBankService` swapped in by config).

## 7. Testing strategy

| Layer | Approach |
|---|---|
| Guardrail | Parametrised positives/negatives incl. plural forms and look-alikes |
| Engine | Property-style checks: every diet × slot has options, no repeated mains, allergen compliance, budget monotonicity |
| Tools | Direct calls with a fake `ToolContext` (state dict) |
| Agents | Scripted `BaseLlm` that picks tool calls from the offered declarations — verifies trajectories, loop iteration, state propagation, refusal paths, spans |
| API / A2A | `httpx` against the ASGI app with the fake model |
| Evals | `adk eval` set in `tests/evals/` for prompt regressions against a real model |

## 8. Known gaps

- Nutrition values are per recipe-serving constants, not computed from ingredients.
- Leftover logic is a simple every-other-day heuristic.
- No streaming on `/chat` (A2A streaming is available through the SDK).
