from typing import Any

from tests.conftest import FakeState
from veggie_planner.tools import (
    build_grocery_list,
    compute_nutrition,
    draft_meal_plan,
    estimate_cost,
    evaluate_meal_plan,
    get_meal_plan,
    get_recipe,
    get_user_profile,
    list_diet_options,
    rate_meal,
    search_recipes,
    swap_meal,
    update_user_profile,
    validate_vegetarian,
)
from veggie_planner.tools.planning import CURRENT_PLAN_KEY, PLAN_HISTORY_KEY
from veggie_planner.tools.profile import PROFILE_KEY, RATINGS_KEY


def _ctx(**state: Any) -> Any:
    return FakeState(state)


# ---- profile -----------------------------------------------------------------


def test_profile_roundtrip_and_missing_essentials(ctx: Any) -> None:
    first = get_user_profile(ctx)
    assert first["status"] == "ok" and first["missing_essentials"] == ["household_size"]
    upd = update_user_profile(
        ctx,
        household_size=3,
        diet="JAIN",
        allergens=["Nuts"],
        pantry=["Rice", " rice "],
        protein_g_per_day=70,
    )
    assert upd["status"] == "ok" and upd["missing_essentials"] == []
    assert upd["profile"]["diet"] == "jain" and upd["profile"]["allergens"] == ["nuts"]
    assert (
        upd["profile"]["pantry"] == ["rice"]
        and upd["profile"]["targets"]["protein_g_per_day"] == 70
    )
    assert ctx.state[PROFILE_KEY]["household_size"] == 3
    # partial update keeps previous values
    upd2 = update_user_profile(ctx, liked_cuisines=["Thai"])
    assert upd2["profile"]["household_size"] == 3 and upd2["profile"]["liked_cuisines"] == ["thai"]


def test_profile_rejects_invalid(ctx: Any) -> None:
    assert update_user_profile(ctx, diet="pescatarian")["status"] == "error"
    assert update_user_profile(ctx, household_size=0)["status"] == "error"
    assert update_user_profile(ctx, cooking_skill="chef")["status"] == "error"


def test_rate_meal(ctx: Any) -> None:
    assert rate_meal("nope", 3, ctx)["status"] == "error"
    assert rate_meal("chana-masala", 9, ctx)["status"] == "error"
    assert rate_meal("chana-masala", 1, ctx)["status"] == "ok"
    assert ctx.state[RATINGS_KEY] == {"chana-masala": 1}
    assert "chana-masala" in ctx.state[PROFILE_KEY]["disliked_recipe_ids"]
    rate_meal("chana-masala", 5, ctx)
    assert "chana-masala" not in ctx.state[PROFILE_KEY]["disliked_recipe_ids"]


def test_list_diet_options() -> None:
    out = list_diet_options()
    assert "vegan" in out["diets"] and "gluten" in out["allergens"]


# ---- recipes -------------------------------------------------------------------


def test_search_recipes_applies_profile(ctx: Any) -> None:
    update_user_profile(
        ctx, household_size=1, diet="vegan", allergens=["soy"], disliked_ingredients=["mushroom"]
    )
    out = search_recipes(ctx, meal_type="dinner", limit=50)
    assert out["status"] == "ok" and out["count"] > 0
    for r in out["recipes"]:
        assert r["vegan"] and "soy" not in r["contains"]
    assert search_recipes(ctx, meal_type="brunch")["status"] == "error"
    assert search_recipes(ctx, limit=999)["count"] <= 25


def test_get_recipe_and_validate(ctx: Any) -> None:
    assert get_recipe("palak-paneer")["recipe"]["name"] == "Palak Paneer with Roti"
    assert get_recipe("x")["status"] == "error"
    bad = validate_vegetarian(["tofu", "fish sauce"], description="Thai chicken curry")
    assert bad["is_vegetarian"] is False and bad["violations"] == ["fish sauce", "chicken"]
    assert validate_vegetarian(["tofu", "rice"])["is_vegetarian"] is True


# ---- planning ------------------------------------------------------------------


def test_draft_requires_household_size(ctx: Any) -> None:
    out = draft_meal_plan(ctx, days=3)
    assert out["status"] == "error" and "household_size" in out["missing_essentials"]
    assert draft_meal_plan(ctx, days=3, servings=2)["status"] == "ok"
    assert draft_meal_plan(ctx, days=0, servings=2)["status"] == "error"
    assert draft_meal_plan(ctx, days=2, servings=2, slots=["brunch"])["status"] == "error"
    assert draft_meal_plan(ctx, days=2, servings=2, start_day="Someday")["status"] == "error"


def test_draft_saves_plan_and_state(ctx: Any) -> None:
    update_user_profile(ctx, household_size=2)
    out = draft_meal_plan(ctx, days=3, slots=["lunch", "dinner"])
    assert out["status"] == "ok"
    plan_id = out["plan"]["plan_id"]
    assert ctx.state[CURRENT_PLAN_KEY] == plan_id and ctx.state[PLAN_HISTORY_KEY] == [plan_id]
    assert len(out["plan"]["days"]) == 3 and set(out["plan"]["days"][0]["meals"]) == {
        "lunch",
        "dinner",
    }
    assert out["evaluation"]["passed"]
    fetched = get_meal_plan(ctx)
    assert fetched["plan"]["plan_id"] == plan_id
    assert get_meal_plan(ctx, plan_id="missing")["status"] == "error"
    assert evaluate_meal_plan(ctx)["passed"] and compute_nutrition(ctx)["per_day"]
    assert estimate_cost(ctx)["total_usd"] > 0


def test_tools_without_plan_error(ctx: Any) -> None:
    for fn in (
        evaluate_meal_plan,
        get_meal_plan,
        compute_nutrition,
        estimate_cost,
        build_grocery_list,
    ):
        assert fn(ctx)["status"] == "error"
    assert swap_meal(ctx, "Monday", "dinner")["status"] == "error"


def test_swap_meal_auto_and_explicit(ctx: Any) -> None:
    update_user_profile(ctx, household_size=2, diet="vegan")
    draft_meal_plan(ctx, days=2)
    before = get_meal_plan(ctx)["plan"]["days"][1]["meals"]["dinner"]["recipe_id"]
    out = swap_meal(ctx, "tuesday", "dinner", max_prep_time_min=30)
    assert out["status"] == "ok" and out["replaced"] == before
    assert out["new_recipe"]["prep_time_min"] <= 30 and out["new_recipe"]["vegan"]
    assert out["new_recipe"]["id"] != before
    assert swap_meal(ctx, "Tuesday", "dinner", max_prep_time_min=1)["status"] == "error"
    # explicit non-compliant recipe is refused (contains dairy, profile is vegan)
    assert swap_meal(ctx, "Tuesday", "dinner", recipe_id="palak-paneer")["status"] == "error"
    ok = swap_meal(ctx, "Tuesday", "dinner", recipe_id="chana-masala")
    assert ok["status"] == "ok" and ok["new_recipe"]["id"] == "chana-masala"
    assert swap_meal(ctx, "Sunday", "dinner")["status"] == "error"
    assert swap_meal(ctx, "Monday", "tea")["status"] == "error"


def test_swap_origin_breaks_leftover_link(ctx: Any) -> None:
    update_user_profile(ctx, household_size=2)
    draft_meal_plan(ctx, days=7)
    plan = get_meal_plan(ctx)["plan"]
    leftover_day = next(d for d in plan["days"] if d["meals"]["lunch"]["leftover_from"])
    origin_day = leftover_day["meals"]["lunch"]["leftover_from"].split("/")[0]
    swap_meal(ctx, origin_day, "dinner")
    plan = get_meal_plan(ctx)["plan"]
    refreshed = next(d for d in plan["days"] if d["day"] == leftover_day["day"])
    assert refreshed["meals"]["lunch"]["leftover_from"] is None


def test_grocery_list_tool(ctx: Any) -> None:
    update_user_profile(ctx, household_size=4, pantry=["olive oil"])
    draft_meal_plan(ctx, days=2)
    out = build_grocery_list(ctx)
    assert out["status"] == "ok" and out["servings"] == 4 and out["item_count"] > 10
    assert "olive oil" in out["already_have"] or not any(
        i["name"] == "olive oil" for aisle in out["by_aisle"].values() for i in aisle
    )
    override = build_grocery_list(ctx, pantry=[])
    assert override["already_have"] == []
