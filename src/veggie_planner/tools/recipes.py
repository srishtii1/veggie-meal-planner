"""Recipe lookup tools."""

from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext

from veggie_planner.data import load_catalog
from veggie_planner.guardrails.vegetarian import validate_ingredients, validate_text
from veggie_planner.models import MEAL_SLOTS, Allergen, Diet
from veggie_planner.tools.profile import load_profile


def _parse_allergens(values: list[str] | None) -> list[Allergen]:
    out: list[Allergen] = []
    for v in values or []:
        try:
            out.append(Allergen(v.lower()))
        except ValueError:
            continue
    return out


def search_recipes(
    tool_context: ToolContext,
    meal_type: str | None = None,
    cuisine: str | None = None,
    max_prep_time_min: int | None = None,
    min_protein_g: float | None = None,
    exclude_ids: list[str] | None = None,
    prefer_ingredients: list[str] | None = None,
    batch_friendly_only: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    """Search the vegetarian recipe catalog, automatically honouring the user's profile.

    The user's diet, allergens, low-FODMAP flag, disliked ingredients and disliked recipes
    are always applied as hard filters. Results are ranked by pantry overlap, then protein.

    Args:
        meal_type: ``breakfast``, ``lunch``, ``dinner`` or ``snack``.
        cuisine: e.g. ``Indian``, ``Italian``, ``Thai``, ``Mexican``.
        max_prep_time_min: Only recipes at or under this many minutes.
        min_protein_g: Minimum protein per serving in grams.
        exclude_ids: Recipe ids to skip (e.g. already used this week).
        prefer_ingredients: Ingredients to favour (defaults to the user's pantry).
        batch_friendly_only: Only recipes that keep well as leftovers.
        limit: Max results (1-25).

    Returns:
        dict with ``status``, ``count`` and ``recipes`` (compact summaries with ids).
    """
    if meal_type is not None and meal_type not in MEAL_SLOTS:
        return {"status": "error", "error": f"meal_type must be one of {list(MEAL_SLOTS)}"}
    profile = load_profile(tool_context.state)
    recipes = load_catalog().search(
        meal_type=meal_type,
        cuisine=cuisine,
        diet=profile.diet,
        exclude_allergens=profile.allergens,
        low_fodmap_only=profile.low_fodmap,
        max_prep_time_min=max_prep_time_min,
        min_protein_g=min_protein_g,
        exclude_ids=list(exclude_ids or []) + profile.disliked_recipe_ids,
        exclude_ingredients=profile.disliked_ingredients,
        prefer_ingredients=prefer_ingredients if prefer_ingredients is not None else profile.pantry,
        batch_friendly_only=batch_friendly_only,
        limit=max(1, min(limit, 25)),
    )
    return {"status": "ok", "count": len(recipes), "recipes": [r.summary() for r in recipes]}


def get_recipe(recipe_id: str) -> dict[str, Any]:
    """Return the full recipe (ingredients per serving, nutrition, instructions) for an id.

    Args:
        recipe_id: Recipe id as returned by ``search_recipes`` or a meal plan.
    """
    recipe = load_catalog().get(recipe_id)
    if recipe is None:
        return {"status": "error", "error": f"unknown recipe id '{recipe_id}'"}
    return {"status": "ok", "recipe": recipe.model_dump(mode="json")}


def validate_vegetarian(ingredients: list[str], description: str = "") -> dict[str, Any]:
    """Check whether a list of ingredients (and optional free text) is strictly vegetarian.

    Flags meat, poultry, seafood, and hidden animal products (gelatin, rennet, fish sauce,
    Worcestershire sauce, etc.). Use it before suggesting any recipe that is not from the
    catalog, e.g. a user-supplied recipe.

    Args:
        ingredients: Ingredient names or lines.
        description: Optional recipe title/description to scan as well.

    Returns:
        dict with ``status``, ``is_vegetarian`` and ``violations``.
    """
    result = validate_ingredients(ingredients)
    text_result = validate_text(description) if description else None
    violations = list(result.violations)
    if text_result:
        violations += [v for v in text_result.violations if v not in violations]
    return {"status": "ok", "is_vegetarian": not violations, "violations": violations}


def list_diet_options() -> dict[str, Any]:
    """List the supported vegetarian sub-diets and allergen exclusions."""
    return {
        "status": "ok",
        "diets": {
            Diet.VEGETARIAN: "lacto-ovo vegetarian (dairy and eggs allowed)",
            Diet.LACTO: "eggless vegetarian (dairy allowed)",
            Diet.OVO: "eggs allowed, no dairy",
            Diet.VEGAN: "no animal products at all",
            Diet.JAIN: "no eggs, honey, or root vegetables (onion, garlic, potato, carrot...)",
        },
        "allergens": [a.value for a in Allergen],
    }
