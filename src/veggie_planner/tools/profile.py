"""User profile tools backed by ADK session state (``user:`` prefix = persists across sessions)."""

from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext
from pydantic import ValidationError

from veggie_planner.models import UserProfile

PROFILE_KEY = "user:profile"
RATINGS_KEY = "user:ratings"


def load_profile(state: Any) -> UserProfile:
    raw = state.get(PROFILE_KEY) if state is not None else None
    if not raw:
        return UserProfile()
    try:
        return UserProfile.model_validate(raw)
    except ValidationError:
        return UserProfile()


def store_profile(state: Any, profile: UserProfile) -> None:
    state[PROFILE_KEY] = profile.model_dump(mode="json")


def load_ratings(state: Any) -> dict[str, int]:
    raw = state.get(RATINGS_KEY) if state is not None else None
    return {str(k): int(v) for k, v in (raw or {}).items()}


def get_user_profile(tool_context: ToolContext) -> dict[str, Any]:
    """Return the user's saved meal-planning profile and which essentials are still missing.

    Call this before planning. If ``missing_essentials`` is non-empty, ask the user for those
    values instead of guessing.

    Returns:
        dict with ``status``, ``profile`` (household size, diet, allergens, preferences,
        pantry, budget, nutrition targets) and ``missing_essentials``.
    """
    profile = load_profile(tool_context.state)
    return {
        "status": "ok",
        "profile": profile.model_dump(mode="json"),
        "missing_essentials": profile.missing_essentials(),
        "ratings": load_ratings(tool_context.state),
    }


def update_user_profile(
    tool_context: ToolContext,
    household_size: int | None = None,
    diet: str | None = None,
    allergens: list[str] | None = None,
    low_fodmap: bool | None = None,
    liked_cuisines: list[str] | None = None,
    disliked_ingredients: list[str] | None = None,
    cooking_skill: str | None = None,
    max_prep_time_weekday_min: int | None = None,
    max_prep_time_weekend_min: int | None = None,
    weekly_budget_usd: float | None = None,
    calories_per_day: float | None = None,
    protein_g_per_day: float | None = None,
    pantry: list[str] | None = None,
) -> dict[str, Any]:
    """Create or update the user's persistent profile. Only provided fields are changed.

    Args:
        household_size: Number of people to cook for (1-20).
        diet: One of ``vegetarian`` (lacto-ovo), ``lacto`` (eggless), ``ovo`` (no dairy),
            ``vegan``, ``jain`` (no eggs, honey or root vegetables). Non-vegetarian diets
            are rejected.
        allergens: Hard exclusions from ``dairy, egg, gluten, nuts, peanut, soy, sesame, honey``.
        low_fodmap: Restrict to low-FODMAP recipes.
        liked_cuisines: Cuisines to favour (e.g. ``["Indian", "Thai"]``).
        disliked_ingredients: Ingredient words to avoid (e.g. ``["mushroom"]``).
        cooking_skill: ``beginner``, ``intermediate`` or ``advanced``.
        max_prep_time_weekday_min: Max minutes per meal on weekdays.
        max_prep_time_weekend_min: Max minutes per meal on weekends.
        weekly_budget_usd: Rough weekly grocery budget cap.
        calories_per_day: Daily calorie target per person.
        protein_g_per_day: Daily protein target per person (grams).
        pantry: Ingredients already on hand; planner prefers recipes that use them.

    Returns:
        dict with ``status`` (``ok`` or ``error``), ``profile`` and ``error`` message if any.
    """
    current = load_profile(tool_context.state)
    patch: dict[str, Any] = {
        k: v
        for k, v in {
            "household_size": household_size,
            "diet": diet.lower() if diet else None,
            "allergens": [a.lower() for a in allergens] if allergens is not None else None,
            "low_fodmap": low_fodmap,
            "liked_cuisines": liked_cuisines,
            "disliked_ingredients": disliked_ingredients,
            "cooking_skill": cooking_skill,
            "max_prep_time_weekday_min": max_prep_time_weekday_min,
            "max_prep_time_weekend_min": max_prep_time_weekend_min,
            "weekly_budget_usd": weekly_budget_usd,
            "pantry": pantry,
        }.items()
        if v is not None
    }
    targets = current.targets.model_dump()
    if calories_per_day is not None:
        targets["calories_per_day"] = calories_per_day
    if protein_g_per_day is not None:
        targets["protein_g_per_day"] = protein_g_per_day
    patch["targets"] = targets
    try:
        updated = UserProfile.model_validate({**current.model_dump(mode="json"), **patch})
    except ValidationError as exc:
        return {
            "status": "error",
            "error": "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()),
        }
    store_profile(tool_context.state, updated)
    return {
        "status": "ok",
        "profile": updated.model_dump(mode="json"),
        "missing_essentials": updated.missing_essentials(),
    }


def rate_meal(recipe_id: str, rating: int, tool_context: ToolContext) -> dict[str, Any]:
    """Record feedback on a recipe (1 = never again, 5 = loved it). Influences future ranking.

    Ratings of 1 also add the recipe to the disliked list so it is never planned again.

    Args:
        recipe_id: The recipe id from the plan.
        rating: Integer 1-5.
    """
    from veggie_planner.data import load_catalog

    if not 1 <= rating <= 5:
        return {"status": "error", "error": "rating must be between 1 and 5"}
    if recipe_id not in load_catalog():
        return {"status": "error", "error": f"unknown recipe id '{recipe_id}'"}
    ratings = load_ratings(tool_context.state)
    ratings[recipe_id] = rating
    tool_context.state[RATINGS_KEY] = ratings
    profile = load_profile(tool_context.state)
    disliked = set(profile.disliked_recipe_ids)
    if rating == 1:
        disliked.add(recipe_id)
    else:
        disliked.discard(recipe_id)
    store_profile(
        tool_context.state, profile.model_copy(update={"disliked_recipe_ids": sorted(disliked)})
    )
    return {"status": "ok", "recipe_id": recipe_id, "rating": rating, "total_rated": len(ratings)}
