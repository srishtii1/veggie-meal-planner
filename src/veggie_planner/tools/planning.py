"""Meal-plan tools: draft, evaluate, save, fetch, swap, nutrition, cost."""

from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext

from veggie_planner import planning_engine as engine
from veggie_planner.data import load_catalog
from veggie_planner.models import DAY_NAMES, MEAL_SLOTS, DayPlan, MealPlan, MealSlot
from veggie_planner.storage import get_plan_store
from veggie_planner.tools.profile import load_profile, load_ratings

CURRENT_PLAN_KEY = "current_plan_id"
PLAN_HISTORY_KEY = "user:plan_ids"


def _user_id(tool_context: ToolContext) -> str:
    return getattr(tool_context, "user_id", None) or "anonymous"


def _remember_plan(tool_context: ToolContext, plan: MealPlan) -> None:
    tool_context.state[CURRENT_PLAN_KEY] = plan.plan_id
    history = list(tool_context.state.get(PLAN_HISTORY_KEY) or [])
    if plan.plan_id not in history:
        history.append(plan.plan_id)
    tool_context.state[PLAN_HISTORY_KEY] = history[-20:]


def _plan_view(plan: MealPlan) -> dict[str, Any]:
    catalog = load_catalog()
    days = []
    for d in plan.days:
        meals = {}
        for slot, rid in d.meals.items():
            r = catalog.get(rid)
            meals[slot] = {
                "recipe_id": rid,
                "name": r.name if r else "?",
                "cuisine": r.cuisine if r else "?",
                "prep_time_min": r.prep_time_min if r else None,
                "protein_g": r.nutrition.protein_g if r else None,
                "leftover_from": d.leftover_from.get(slot),
            }
        days.append({"day": d.day, "meals": meals})
    return {"plan_id": plan.plan_id, "servings": plan.servings, "days": days, "notes": plan.notes}


def _resolve_plan(tool_context: ToolContext, plan_id: str | None) -> MealPlan | None:
    pid = plan_id or tool_context.state.get(CURRENT_PLAN_KEY)
    return get_plan_store().get(pid) if pid else None


def draft_meal_plan(
    tool_context: ToolContext,
    days: int = 7,
    slots: list[str] | None = None,
    servings: int | None = None,
    start_day: str = "Monday",
    use_leftovers: bool = True,
    exclude_recipe_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Generate and save a draft meal plan that satisfies the user's profile constraints.

    Deterministically picks catalog recipes respecting diet, allergens, dislikes, prep-time
    limits (weekday vs weekend), pantry preference, protein targets, cuisine variety and
    budget. Batch-friendly dinners are reused as next-day lunches when ``use_leftovers``.
    The draft is saved and becomes the current plan; review it with ``evaluate_meal_plan``.

    Args:
        days: Number of days to plan (1-14).
        slots: Subset of ``breakfast, lunch, dinner, snack``; defaults to all four.
        servings: People per meal; defaults to the profile's household size.
        start_day: Weekday name the plan starts on.
        use_leftovers: Cook-once-eat-twice for batch-friendly dinners.
        exclude_recipe_ids: Recipes to avoid (e.g. last week's mains).

    Returns:
        dict with ``status``, ``plan`` (per-day meals with recipe ids), ``evaluation``
        (issues, nutrition, cost) and ``notes``.
    """
    if not 1 <= days <= 14:
        return {"status": "error", "error": "days must be between 1 and 14"}
    chosen: tuple[MealSlot, ...] = tuple(s for s in MEAL_SLOTS if s in (slots or MEAL_SLOTS))
    if slots and len(chosen) != len(set(slots)):
        return {"status": "error", "error": f"slots must be from {list(MEAL_SLOTS)}"}
    if start_day not in DAY_NAMES:
        return {"status": "error", "error": f"start_day must be one of {list(DAY_NAMES)}"}
    profile = load_profile(tool_context.state)
    if servings is None and profile.household_size is None:
        return {
            "status": "error",
            "error": "household_size is unknown; ask the user how many people to cook for",
            "missing_essentials": profile.missing_essentials(),
        }
    catalog = load_catalog()
    plan, notes = engine.draft_plan(
        catalog,
        profile,
        user_id=_user_id(tool_context),
        days=days,
        slots=chosen,
        servings=servings,
        ratings=load_ratings(tool_context.state),
        start_day=start_day,
        use_leftovers=use_leftovers,
        exclude_ids=tuple(exclude_recipe_ids or []),
    )
    plan.notes = [n.message for n in notes]
    get_plan_store().save(plan)
    _remember_plan(tool_context, plan)
    return {
        "status": "ok",
        "plan": _plan_view(plan),
        "evaluation": engine.evaluate_plan(catalog, profile, plan),
    }


def evaluate_meal_plan(tool_context: ToolContext, plan_id: str | None = None) -> dict[str, Any]:
    """Critically check a saved plan: vegetarian/diet/allergen compliance, repeats, prep
    time, protein and calorie targets, budget, variety.

    Args:
        plan_id: Plan to check; defaults to the current plan in this session.

    Returns:
        dict with ``status``, ``passed`` (no errors), ``issues`` (severity, code, message,
        day, slot), ``nutrition``, ``cost`` and ``variety``.
    """
    plan = _resolve_plan(tool_context, plan_id)
    if plan is None:
        return {"status": "error", "error": "no plan found; draft one first"}
    result = engine.evaluate_plan(load_catalog(), load_profile(tool_context.state), plan)
    return {"status": "ok", "plan_id": plan.plan_id, **result}


def get_meal_plan(tool_context: ToolContext, plan_id: str | None = None) -> dict[str, Any]:
    """Fetch a saved meal plan (defaults to the current one) in a readable form.

    Args:
        plan_id: The plan id, or omit for the session's current plan.
    """
    plan = _resolve_plan(tool_context, plan_id)
    if plan is None:
        return {"status": "error", "error": "no plan found"}
    return {"status": "ok", "plan": _plan_view(plan)}


def swap_meal(
    tool_context: ToolContext,
    day: str,
    slot: str,
    recipe_id: str | None = None,
    max_prep_time_min: int | None = None,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Replace one meal in the plan, either with a specific recipe or an automatic pick.

    The replacement must pass the user's diet/allergen filters; the tool refuses otherwise.
    Automatic picks avoid recipes already in the plan and honour ``max_prep_time_min``.

    Args:
        day: Weekday name, e.g. ``Tuesday``.
        slot: ``breakfast``, ``lunch``, ``dinner`` or ``snack``.
        recipe_id: Specific replacement; omit to let the planner choose.
        max_prep_time_min: Upper bound for an automatic pick (e.g. "something faster").
        plan_id: Plan to modify; defaults to the current plan.
    """
    if slot not in MEAL_SLOTS:
        return {"status": "error", "error": f"slot must be one of {list(MEAL_SLOTS)}"}
    plan = _resolve_plan(tool_context, plan_id)
    if plan is None:
        return {"status": "error", "error": "no plan found; draft one first"}
    day_plan: DayPlan | None = next((d for d in plan.days if d.day.lower() == day.lower()), None)
    if day_plan is None:
        return {"status": "error", "error": f"{day} is not in this plan"}
    catalog = load_catalog()
    profile = load_profile(tool_context.state)
    old_id = day_plan.meals.get(slot)
    if recipe_id:
        allowed = catalog.search(
            meal_type=slot,
            diet=profile.diet,
            exclude_allergens=profile.allergens,
            low_fodmap_only=profile.low_fodmap,
            exclude_ingredients=profile.disliked_ingredients,
            limit=1000,
        )
        if recipe_id not in {r.id for r in allowed}:
            return {
                "status": "error",
                "error": f"'{recipe_id}' is unknown or violates the user's constraints for {slot}",
            }
        new_id = recipe_id
    else:
        picks = catalog.search(
            meal_type=slot,
            diet=profile.diet,
            exclude_allergens=profile.allergens,
            low_fodmap_only=profile.low_fodmap,
            exclude_ingredients=profile.disliked_ingredients,
            max_prep_time_min=max_prep_time_min,
            exclude_ids=set(plan.recipe_ids()) | set(profile.disliked_recipe_ids),
            prefer_ingredients=profile.pantry,
            limit=1,
        )
        if not picks:
            return {"status": "error", "error": "no alternative recipe satisfies the constraints"}
        new_id = picks[0].id
    day_plan.meals[slot] = new_id
    day_plan.leftover_from.pop(slot, None)
    for d in plan.days:  # a swapped origin meal breaks its leftover link
        for ls in [
            s for s, origin in d.leftover_from.items() if origin == f"{day_plan.day}/{slot}"
        ]:
            d.leftover_from.pop(ls)
    get_plan_store().save(plan)
    _remember_plan(tool_context, plan)
    new = catalog.get(new_id)
    return {
        "status": "ok",
        "day": day_plan.day,
        "slot": slot,
        "replaced": old_id,
        "new_recipe": new.summary() if new else None,
        "evaluation": engine.evaluate_plan(catalog, profile, plan),
    }


def compute_nutrition(tool_context: ToolContext, plan_id: str | None = None) -> dict[str, Any]:
    """Per-day and average per-person nutrition (calories, protein, carbs, fat, fibre) for a plan.

    Args:
        plan_id: Plan id; defaults to the current plan.
    """
    plan = _resolve_plan(tool_context, plan_id)
    if plan is None:
        return {"status": "error", "error": "no plan found"}
    return {"status": "ok", "plan_id": plan.plan_id, **engine.plan_nutrition(load_catalog(), plan)}


def estimate_cost(tool_context: ToolContext, plan_id: str | None = None) -> dict[str, Any]:
    """Rough grocery cost (USD) of a plan for the configured servings, per day and total.

    Args:
        plan_id: Plan id; defaults to the current plan.
    """
    plan = _resolve_plan(tool_context, plan_id)
    if plan is None:
        return {"status": "error", "error": "no plan found"}
    return {"status": "ok", "plan_id": plan.plan_id, **engine.estimate_cost(load_catalog(), plan)}


def build_grocery_list(
    tool_context: ToolContext, plan_id: str | None = None, pantry: list[str] | None = None
) -> dict[str, Any]:
    """Aggregate all ingredients of a plan into a grocery list grouped by aisle.

    Quantities are scaled by servings and summed across recipes; items matching the pantry
    (argument, else the profile's pantry) are listed under ``already_have`` instead.

    Args:
        plan_id: Plan id; defaults to the current plan.
        pantry: Items on hand to subtract; defaults to the profile pantry.
    """
    plan = _resolve_plan(tool_context, plan_id)
    if plan is None:
        return {"status": "error", "error": "no plan found; draft one first"}
    profile = load_profile(tool_context.state)
    result = engine.grocery_list(
        load_catalog(), plan, pantry if pantry is not None else profile.pantry
    )
    return {"status": "ok", "plan_id": plan.plan_id, **result}
