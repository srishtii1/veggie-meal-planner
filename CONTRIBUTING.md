# Contributing

Thanks for helping make vegetarian meal planning better.

## Dev setup

```bash
uv sync --all-groups && uv run pre-commit install
cp .env.example .env   # add your GOOGLE_API_KEY
make test              # fast, no network
make playground        # adk web UI on http://localhost:8000
```

## Ground rules

1. **Vegetarian is non-negotiable.** Seed recipes are validated at load time against the
   deny-list in `guardrails/vegetarian.py`; a non-veg ingredient fails the test suite.
2. **Tools are deterministic and typed.** Return `{"status": "ok" | "error", ...}`; never
   raise for user-facing errors. Every tool gets unit tests in `tests/unit/test_tools.py`.
3. **Prompts live in `agents/prompts.py`.** If you change one, run `make eval` with a real
   key and paste the result in the PR.
4. **Keep the LLM out of math.** Nutrition, cost, compliance and grocery aggregation are
   computed in `planning_engine.py`, not by the model.

## Adding a recipe

Append to `src/veggie_planner/data/recipes.json` following the `_schema` comment at the top.
Required: per-serving nutrition, `contains` allergens, `root_veg`, `low_fodmap`, cost.
Run `make test` — `test_catalog.py` checks every diet/slot combination still has options.

## Lockfile

`uv.lock` pins exact versions. If your machine uses a private package proxy, the lock
records that proxy's URLs; CI and the Dockerfile set `UV_DEFAULT_INDEX=https://pypi.org/simple`
and run `uv lock` first, which re-points the same pins at public PyPI before `uv sync --locked`.

## Commit style

Conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`). Squash on merge.
