"""Deterministic presenter: renders the current plan as markdown without an LLM call."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any, override

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from veggie_planner import planning_engine as engine
from veggie_planner.data import load_catalog
from veggie_planner.models import MEAL_SLOTS, MealPlan
from veggie_planner.storage import get_plan_store
from veggie_planner.tools.planning import CURRENT_PLAN_KEY
from veggie_planner.tools.profile import load_profile


def render_plan_markdown(plan: MealPlan, evaluation: dict[str, Any]) -> str:
    catalog = load_catalog()
    slots = [s for s in MEAL_SLOTS if any(s in d.meals for d in plan.days)]
    lines = [
        f"**Plan `{plan.plan_id}`** — {len(plan.days)} days, {plan.servings} servings",
        "",
        "| Day | " + " | ".join(s.title() for s in slots) + " |",
        "|---|" + "---|" * len(slots),
    ]
    for d in plan.days:
        cells = []
        for s in slots:
            rid = d.meals.get(s)
            r = catalog.get(rid) if rid else None
            if r is None:
                cells.append("—")
                continue
            label = f"{r.name} ({r.prep_time_min} min)"
            if s in d.leftover_from:
                label += " ♻️ leftovers"
            cells.append(label)
        lines.append(f"| {d.day} | " + " | ".join(cells) + " |")
    avg = evaluation["nutrition"]["daily_average"]
    lines += [
        "",
        f"Daily average per person: {avg['calories']:.0f} kcal · "
        f"{avg['protein_g']:.0f} g protein · {avg['fiber_g']:.0f} g fibre. "
        f"Estimated cost: ${evaluation['cost']['total_usd']:.2f} for {plan.servings} servings. "
        f"Variety: {evaluation['variety']['distinct_recipes']} recipes, "
        f"{evaluation['variety']['distinct_cuisines']} cuisines.",
    ]
    warnings = [i["message"] for i in evaluation["issues"] if i["severity"] != "info"]
    if warnings:
        lines += ["", "Notes:"] + [f"- {w}" for w in warnings[:6]]
    return "\n".join(lines)


class PlanPresenterAgent(BaseAgent):
    """Emits the current plan as a markdown message; the last step of the planning pipeline."""

    @override
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        plan_id = ctx.session.state.get(CURRENT_PLAN_KEY)
        plan = get_plan_store().get(plan_id) if plan_id else None
        if plan is None:
            text = "No plan was produced. Ask the user for household size and days, then retry."
        else:
            evaluation = engine.evaluate_plan(load_catalog(), load_profile(ctx.session.state), plan)
            text = render_plan_markdown(plan, evaluation)
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
        )
