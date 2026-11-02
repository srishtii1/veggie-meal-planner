"""Typed, deterministic tools exposed to the agents."""

from veggie_planner.tools.planning import (
    build_grocery_list,
    compute_nutrition,
    draft_meal_plan,
    estimate_cost,
    evaluate_meal_plan,
    get_meal_plan,
    swap_meal,
)
from veggie_planner.tools.profile import get_user_profile, rate_meal, update_user_profile
from veggie_planner.tools.recipes import (
    get_recipe,
    list_diet_options,
    search_recipes,
    validate_vegetarian,
)

PROFILE_TOOLS = [get_user_profile, update_user_profile, rate_meal, list_diet_options]
RECIPE_TOOLS = [search_recipes, get_recipe, validate_vegetarian]
PLANNING_TOOLS = [
    draft_meal_plan,
    evaluate_meal_plan,
    get_meal_plan,
    swap_meal,
    compute_nutrition,
    estimate_cost,
]
GROCERY_TOOLS = [build_grocery_list, get_meal_plan, estimate_cost]

ALL_TOOLS = PROFILE_TOOLS + RECIPE_TOOLS + PLANNING_TOOLS + [build_grocery_list]

__all__ = [
    "ALL_TOOLS",
    "GROCERY_TOOLS",
    "PLANNING_TOOLS",
    "PROFILE_TOOLS",
    "RECIPE_TOOLS",
    "build_grocery_list",
    "compute_nutrition",
    "draft_meal_plan",
    "estimate_cost",
    "evaluate_meal_plan",
    "get_meal_plan",
    "get_recipe",
    "get_user_profile",
    "list_diet_options",
    "rate_meal",
    "search_recipes",
    "swap_meal",
    "update_user_profile",
    "validate_vegetarian",
]
