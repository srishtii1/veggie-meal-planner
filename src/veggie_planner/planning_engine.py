"""Deterministic planning engine: drafting, evaluation, nutrition, cost, grocery.

Pure functions over the catalog and the user profile. The LLM agents call these via
tools and reason about the structured results; they never compute nutrition or
decide vegetarian compliance themselves.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from veggie_planner.data.catalog import RecipeCatalog
from veggie_planner.guardrails.vegetarian import validate_ingredients
from veggie_planner.models import (
    DAY_NAMES,
    MEAL_SLOTS,
    WEEKEND_DAYS,
    DayPlan,
    GroceryItem,
    MealPlan,
    MealSlot,
    Nutrition,
    Recipe,
    UserProfile,
)

DEFAULT_PROTEIN_FLOOR_G = 50.0  # flag days below this even without an explicit target
CALORIE_TOLERANCE = 0.20


@dataclass(frozen=True)
class Issue:
    severity: str  # "error" | "warning" | "info"
    code: str
    message: str
    day: str | None = None
    slot: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _max_prep(profile: UserProfile, day: str) -> int:
    return (
        profile.max_prep_time_weekend_min
        if day in WEEKEND_DAYS
        else profile.max_prep_time_weekday_min
    )


def _days(start_day: str, n: int) -> list[str]:
    start = DAY_NAMES.index(start_day) if start_day in DAY_NAMES else 0
    return [DAY_NAMES[(start + i) % 7] for i in range(n)]


def _score(
    r: Recipe,
    *,
    profile: UserProfile,
    ratings: dict[str, int],
    protein_gap: float,
    prev_cuisine: str | None,
    used_cuisines: dict[str, int],
) -> float:
    s = 0.0
    s += 2.0 * sum(1 for i in r.ingredients if any(p in i.name.lower() for p in profile.pantry))
    if profile.liked_cuisines and r.cuisine.lower() in profile.liked_cuisines:
        s += 3.0
    s += (ratings.get(r.id, 3) - 3) * 2.0
    if protein_gap > 0:
        s += min(r.nutrition.protein_g, protein_gap) / 10.0
    if prev_cuisine and r.cuisine == prev_cuisine:
        s -= 2.5
    s -= 0.5 * used_cuisines.get(r.cuisine, 0)
    if profile.weekly_budget_usd:
        s -= r.cost_usd
    return s


def draft_plan(
    catalog: RecipeCatalog,
    profile: UserProfile,
    *,
    user_id: str,
    days: int = 7,
    slots: tuple[MealSlot, ...] = MEAL_SLOTS,
    servings: int | None = None,
    ratings: dict[str, int] | None = None,
    start_day: str = "Monday",
    use_leftovers: bool = True,
    exclude_ids: tuple[str, ...] = (),
) -> tuple[MealPlan, list[Issue]]:
    """Greedy, constraint-respecting draft. Returns the plan and non-fatal notes."""
    ratings = ratings or {}
    servings = servings or profile.household_size or 2
    day_names = _days(start_day, days)
    plan_days: list[DayPlan] = []
    used: set[str] = set(exclude_ids)
    used_cuisines: dict[str, int] = defaultdict(int)
    notes: list[Issue] = []
    target_protein = profile.targets.protein_g_per_day or DEFAULT_PROTEIN_FLOOR_G
    pending_leftover: tuple[str, str] | None = None  # (recipe_id, "Day/dinner")

    for idx, day in enumerate(day_names):
        meals: dict[MealSlot, str] = {}
        leftover_from: dict[MealSlot, str] = {}
        day_protein = 0.0
        for slot in slots:
            if slot == "lunch" and pending_leftover:
                rid, origin = pending_leftover
                meals[slot] = rid
                leftover_from[slot] = origin
                recipe = catalog.get(rid)
                day_protein += recipe.nutrition.protein_g if recipe else 0
                pending_leftover = None
                continue
            candidates = catalog.search(
                meal_type=slot,
                diet=profile.diet,
                exclude_allergens=profile.allergens,
                low_fodmap_only=profile.low_fodmap,
                max_prep_time_min=_max_prep(profile, day),
                exclude_ids=used | set(profile.disliked_recipe_ids),
                exclude_ingredients=profile.disliked_ingredients,
                prefer_ingredients=profile.pantry,
                limit=100,
            )
            if not candidates:  # relax the no-repeat rule rather than leave a hole
                candidates = catalog.search(
                    meal_type=slot,
                    diet=profile.diet,
                    exclude_allergens=profile.allergens,
                    low_fodmap_only=profile.low_fodmap,
                    max_prep_time_min=_max_prep(profile, day),
                    exclude_ids=set(profile.disliked_recipe_ids),
                    exclude_ingredients=profile.disliked_ingredients,
                    limit=100,
                )
                if candidates:
                    notes.append(
                        Issue(
                            "info",
                            "repeat_allowed",
                            f"Repeating a {slot} recipe on {day}: catalog exhausted "
                            "under current constraints",
                            day,
                            slot,
                        )
                    )
            if not candidates:
                notes.append(
                    Issue(
                        "warning",
                        "no_candidate",
                        f"No {slot} recipe satisfies the constraints on {day}",
                        day,
                        slot,
                    )
                )
                continue
            prev_cuisine = None
            if plan_days and slot in plan_days[-1].meals:
                prev = catalog.get(plan_days[-1].meals[slot])
                prev_cuisine = prev.cuisine if prev else None
            remaining_slots = max(1, len([s for s in slots if s not in meals]))
            protein_gap = max(0.0, target_protein - day_protein) / remaining_slots
            best = max(
                candidates,
                key=lambda r: (
                    _score(
                        r,
                        profile=profile,
                        ratings=ratings,
                        protein_gap=protein_gap,
                        prev_cuisine=prev_cuisine,
                        used_cuisines=used_cuisines,
                    ),
                    r.id,
                ),
            )
            meals[slot] = best.id
            used.add(best.id)
            used_cuisines[best.cuisine] += 1
            day_protein += best.nutrition.protein_g
            if (
                use_leftovers
                and slot == "dinner"
                and "lunch" in slots
                and best.batch_friendly
                and idx + 1 < len(day_names)
                and idx % 2 == 0
            ):
                pending_leftover = (best.id, f"{day}/dinner")
        plan_days.append(DayPlan(day=day, meals=meals, leftover_from=leftover_from))

    plan = MealPlan(
        plan_id=uuid.uuid4().hex[:10], user_id=user_id, servings=servings, days=plan_days
    )
    if profile.weekly_budget_usd:
        plan = _fit_budget(catalog, profile, plan, notes)
    return plan, notes


def _fit_budget(
    catalog: RecipeCatalog, profile: UserProfile, plan: MealPlan, notes: list[Issue]
) -> MealPlan:
    """Swap the priciest non-leftover meals for cheaper compliant ones until under budget."""
    budget = profile.weekly_budget_usd or 0
    for _ in range(len(plan.days) * len(MEAL_SLOTS)):
        if estimate_cost(catalog, plan)["total_usd"] <= budget:
            return plan
        candidates = [
            (catalog.get(rid).cost_usd, d_idx, slot, rid)  # type: ignore[union-attr]
            for d_idx, d in enumerate(plan.days)
            for slot, rid in d.meals.items()
            if slot not in d.leftover_from and catalog.get(rid)
        ]
        if not candidates:
            break
        candidates.sort(reverse=True)
        swapped = False
        for cost, d_idx, slot, _rid in candidates:
            day = plan.days[d_idx]
            cheaper = catalog.search(
                meal_type=slot,
                diet=profile.diet,
                exclude_allergens=profile.allergens,
                low_fodmap_only=profile.low_fodmap,
                max_prep_time_min=_max_prep(profile, day.day),
                exclude_ids=set(plan.recipe_ids()) | set(profile.disliked_recipe_ids),
                exclude_ingredients=profile.disliked_ingredients,
                max_cost_usd=cost - 0.05,
                limit=1,
            )
            if cheaper:
                day.meals[slot] = cheaper[0].id
                for d in plan.days:  # propagate to leftovers of this meal
                    for ls, origin in d.leftover_from.items():
                        if origin == f"{day.day}/{slot}":
                            d.meals[ls] = cheaper[0].id
                swapped = True
                break
        if not swapped:
            break
    notes.append(
        Issue(
            "warning",
            "over_budget",
            f"Could not fit plan under ${budget:.2f}; cheapest achievable is "
            f"${estimate_cost(catalog, plan)['total_usd']:.2f}",
        )
    )
    return plan


def plan_nutrition(catalog: RecipeCatalog, plan: MealPlan) -> dict[str, Any]:
    """Per-person nutrition per day plus weekly averages."""
    per_day: list[dict[str, Any]] = []
    total = Nutrition()
    for d in plan.days:
        day_total = Nutrition()
        for rid in d.meals.values():
            r = catalog.get(rid)
            if r:
                day_total = day_total + r.nutrition
        per_day.append({"day": d.day, **day_total.model_dump()})
        total = total + day_total
    n = max(1, len(plan.days))
    return {"per_day": per_day, "daily_average": total.scaled(1 / n).model_dump()}


def estimate_cost(catalog: RecipeCatalog, plan: MealPlan) -> dict[str, Any]:
    per_day = []
    total = 0.0
    for d in plan.days:
        day_cost = 0.0
        for rid in d.meals.values():
            r = catalog.get(rid)
            day_cost += r.cost_usd if r else 0.0
        day_cost *= plan.servings
        per_day.append({"day": d.day, "usd": round(day_cost, 2)})
        total += day_cost
    return {"total_usd": round(total, 2), "servings": plan.servings, "per_day": per_day}


def evaluate_plan(catalog: RecipeCatalog, profile: UserProfile, plan: MealPlan) -> dict[str, Any]:
    """Critic checks. ``passed`` is False only for errors; warnings are advisory."""
    issues: list[Issue] = []
    seen: dict[str, str] = {}
    for d in plan.days:
        for slot, rid in d.meals.items():
            r = catalog.get(rid)
            if r is None:
                issues.append(
                    Issue("error", "unknown_recipe", f"Unknown recipe id '{rid}'", d.day, slot)
                )
                continue
            veg = validate_ingredients([i.name for i in r.ingredients] + [r.name])
            if not veg.is_vegetarian:
                issues.append(
                    Issue(
                        "error",
                        "non_vegetarian",
                        f"{r.name} contains {', '.join(veg.violations)}",
                        d.day,
                        slot,
                    )
                )
            if not r.allowed_for(profile.diet):
                issues.append(
                    Issue(
                        "error",
                        "diet_violation",
                        f"{r.name} is not {profile.diet.value}",
                        d.day,
                        slot,
                    )
                )
            clash = set(r.contains) & set(profile.allergens)
            if clash:
                issues.append(
                    Issue(
                        "error",
                        "allergen",
                        f"{r.name} contains {', '.join(sorted(a.value for a in clash))}",
                        d.day,
                        slot,
                    )
                )
            if profile.low_fodmap and not r.low_fodmap:
                issues.append(Issue("error", "fodmap", f"{r.name} is not low-FODMAP", d.day, slot))
            if rid in profile.disliked_recipe_ids:
                issues.append(
                    Issue(
                        "error",
                        "disliked",
                        f"{r.name} was previously rejected by the user",
                        d.day,
                        slot,
                    )
                )
            if slot not in d.leftover_from and r.prep_time_min > _max_prep(profile, d.day):
                issues.append(
                    Issue(
                        "warning",
                        "prep_time",
                        f"{r.name} takes {r.prep_time_min} min, over the "
                        f"{_max_prep(profile, d.day)} min limit",
                        d.day,
                        slot,
                    )
                )
            key = rid
            if slot in {"lunch", "dinner"} and slot not in d.leftover_from:
                if key in seen:
                    issues.append(
                        Issue(
                            "warning",
                            "repeat",
                            f"{r.name} already planned on {seen[key]}",
                            d.day,
                            slot,
                        )
                    )
                seen[key] = f"{d.day}/{slot}"
    nutrition = plan_nutrition(catalog, plan)
    protein_target = profile.targets.protein_g_per_day or DEFAULT_PROTEIN_FLOOR_G
    for row in nutrition["per_day"]:
        if row["protein_g"] < protein_target:
            issues.append(
                Issue(
                    "warning",
                    "low_protein",
                    f"{row['day']} has {row['protein_g']:.0f} g protein vs target "
                    f"{protein_target:.0f} g; add legumes, tofu, tempeh or paneer",
                    row["day"],
                )
            )
        cal_target = profile.targets.calories_per_day
        if cal_target and abs(row["calories"] - cal_target) / cal_target > CALORIE_TOLERANCE:
            issues.append(
                Issue(
                    "warning",
                    "calories",
                    f"{row['day']} has {row['calories']:.0f} kcal vs target {cal_target:.0f}",
                    row["day"],
                )
            )
    cost = estimate_cost(catalog, plan)
    if profile.weekly_budget_usd and cost["total_usd"] > profile.weekly_budget_usd:
        issues.append(
            Issue(
                "warning",
                "over_budget",
                f"Estimated ${cost['total_usd']:.2f} exceeds budget "
                f"${profile.weekly_budget_usd:.2f}",
            )
        )
    cuisines = {catalog.get(rid).cuisine for rid in plan.recipe_ids() if catalog.get(rid)}  # type: ignore[union-attr]
    return {
        "passed": not any(i.severity == "error" for i in issues),
        "issues": [i.as_dict() for i in issues],
        "nutrition": nutrition,
        "cost": cost,
        "variety": {
            "distinct_recipes": len(set(plan.recipe_ids())),
            "distinct_cuisines": len(cuisines),
        },
    }


def grocery_list(catalog: RecipeCatalog, plan: MealPlan, pantry: list[str]) -> dict[str, Any]:
    """Aggregate ingredients across the plan, scale by servings, subtract pantry, group by aisle."""
    agg: dict[tuple[str, str], GroceryItem] = {}
    for d in plan.days:
        for rid in d.meals.values():
            r = catalog.get(rid)
            if r is None:
                continue
            # Leftover slots still consume ingredients (cooked double at the origin meal),
            # so every slot contributes ``servings`` portions.
            multiplier = plan.servings
            for ing in r.ingredients:
                k = (ing.name.lower(), ing.unit)
                item = agg.get(k)
                if item is None:
                    agg[k] = GroceryItem(name=ing.name, quantity=0, unit=ing.unit, aisle=ing.aisle)
                    item = agg[k]
                item.quantity = round(item.quantity + ing.quantity * multiplier, 2)
                if r.name not in item.recipes:
                    item.recipes.append(r.name)
    pantry_l = [p.lower() for p in pantry]
    have = [i for i in agg.values() if any(p in i.name.lower() for p in pantry_l)]
    need = [i for i in agg.values() if i not in have]
    by_aisle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for i in sorted(need, key=lambda x: (x.aisle, x.name)):
        by_aisle[i.aisle].append(i.model_dump())
    return {
        "servings": plan.servings,
        "item_count": len(need),
        "by_aisle": dict(by_aisle),
        "already_have": sorted({i.name for i in have}),
    }
