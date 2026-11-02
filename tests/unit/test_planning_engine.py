from veggie_planner import planning_engine as engine
from veggie_planner.data import load_catalog
from veggie_planner.models import DayPlan, Diet, MealPlan, UserProfile


def _profile(**kw: object) -> UserProfile:
    base: dict[str, object] = {"household_size": 2}
    base.update(kw)
    return UserProfile.model_validate(base)


def test_draft_full_week_has_all_slots_and_variety() -> None:
    catalog = load_catalog()
    plan, notes = engine.draft_plan(catalog, _profile(), user_id="u")
    assert len(plan.days) == 7 and plan.servings == 2
    assert all(len(d.meals) == 4 for d in plan.days)
    mains = [
        rid
        for d in plan.days
        for s, rid in d.meals.items()
        if s in {"lunch", "dinner"} and s not in d.leftover_from
    ]
    assert len(mains) == len(set(mains)), "repeated main without leftover marker"
    assert any(d.leftover_from for d in plan.days), "expected cook-once-eat-twice lunches"
    assert not [n for n in notes if n.severity == "warning"]


def test_draft_respects_diet_allergens_and_prep_time() -> None:
    catalog = load_catalog()
    profile = _profile(
        diet="vegan",
        allergens=["soy", "gluten"],
        max_prep_time_weekday_min=25,
        max_prep_time_weekend_min=40,
    )
    plan, _ = engine.draft_plan(catalog, profile, user_id="u")
    evaluation = engine.evaluate_plan(catalog, profile, plan)
    assert evaluation["passed"]
    assert not [
        i for i in evaluation["issues"] if i["code"] in {"allergen", "diet_violation", "prep_time"}
    ]
    for d in plan.days:
        for rid in d.meals.values():
            r = catalog.get(rid)
            assert r is not None and r.is_vegan


def test_draft_without_leftovers_and_custom_slots() -> None:
    catalog = load_catalog()
    plan, _ = engine.draft_plan(
        catalog,
        _profile(),
        user_id="u",
        days=4,
        slots=("dinner",),
        use_leftovers=False,
        start_day="Friday",
    )
    assert [d.day for d in plan.days] == ["Friday", "Saturday", "Sunday", "Monday"]
    assert all(list(d.meals) == ["dinner"] for d in plan.days)
    assert not any(d.leftover_from for d in plan.days)


def test_draft_requires_servings_or_household() -> None:
    catalog = load_catalog()
    plan, _ = engine.draft_plan(catalog, UserProfile(), user_id="u", days=1)
    assert plan.servings == 2  # engine default; the tool layer asks the user instead


def test_budget_fitting_swaps_to_cheaper_meals() -> None:
    catalog = load_catalog()
    rich, _ = engine.draft_plan(catalog, _profile(), user_id="u", days=5)
    rich_cost = engine.estimate_cost(catalog, rich)["total_usd"]
    tight, _notes = engine.draft_plan(
        catalog, _profile(weekly_budget_usd=rich_cost * 0.8), user_id="u", days=5
    )
    assert engine.estimate_cost(catalog, tight)["total_usd"] < rich_cost


def test_ratings_and_dislikes_influence_choice() -> None:
    catalog = load_catalog()
    liked = catalog.search(meal_type="dinner", limit=1)[0].id
    plan, _ = engine.draft_plan(
        catalog, _profile(), user_id="u", days=1, slots=("dinner",), ratings={liked: 5}
    )
    assert plan.days[0].meals["dinner"] == liked
    profile = _profile(disliked_recipe_ids=[liked])
    plan2, _ = engine.draft_plan(catalog, profile, user_id="u", days=7)
    assert liked not in plan2.recipe_ids()


def test_evaluate_flags_errors_for_bad_plan() -> None:
    catalog = load_catalog()
    profile = _profile(diet=Diet.VEGAN, allergens=["nuts"])
    plan = MealPlan(
        plan_id="p",
        user_id="u",
        servings=2,
        days=[
            DayPlan(day="Monday", meals={"breakfast": "greek-yogurt-parfait", "dinner": "nope"}),
            DayPlan(day="Tuesday", meals={"dinner": "lentil-bolognese"}),
            DayPlan(day="Wednesday", meals={"dinner": "lentil-bolognese"}),
        ],
    )
    ev = engine.evaluate_plan(catalog, profile, plan)
    codes = {i["code"] for i in ev["issues"]}
    assert not ev["passed"]
    assert {"unknown_recipe", "diet_violation", "allergen", "repeat", "prep_time"} <= codes


def test_nutrition_and_low_protein_warning() -> None:
    catalog = load_catalog()
    profile = _profile(targets={"protein_g_per_day": 200, "calories_per_day": 2000})
    plan, _ = engine.draft_plan(catalog, profile, user_id="u", days=2)
    ev = engine.evaluate_plan(catalog, profile, plan)
    assert any(i["code"] == "low_protein" for i in ev["issues"])
    nut = engine.plan_nutrition(catalog, plan)
    assert len(nut["per_day"]) == 2 and nut["daily_average"]["protein_g"] > 0


def test_grocery_list_aggregates_scales_and_subtracts_pantry() -> None:
    catalog = load_catalog()
    plan = MealPlan(
        plan_id="p",
        user_id="u",
        servings=3,
        days=[
            DayPlan(day="Monday", meals={"dinner": "chana-masala"}),
            DayPlan(
                day="Tuesday",
                meals={"lunch": "chana-masala"},
                leftover_from={"lunch": "Monday/dinner"},
            ),
        ],
    )
    out = engine.grocery_list(catalog, plan, pantry=["brown rice"])
    assert out["servings"] == 3
    assert "brown rice" in out["already_have"]
    pantry_items = [
        i for aisle in out["by_aisle"].values() for i in aisle if i["name"] == "chickpeas (cooked)"
    ]
    assert pantry_items and pantry_items[0]["quantity"] == 200 * 3 * 2
    assert set(out["by_aisle"]) <= {"produce", "spices", "pantry", "grains"}


def test_budget_fitting_swaps_until_under_or_reports() -> None:
    catalog = load_catalog()
    tight, notes = engine.draft_plan(
        catalog, _profile(weekly_budget_usd=25), user_id="u", days=7, use_leftovers=False
    )
    cost = engine.estimate_cost(catalog, tight)["total_usd"]
    assert cost <= 25 or any(n.code == "over_budget" for n in notes)
    # every meal is still compliant after swapping
    assert engine.evaluate_plan(catalog, _profile(weekly_budget_usd=25), tight)["passed"]


def test_budget_swap_propagates_to_leftover() -> None:
    catalog = load_catalog()
    profile = _profile(weekly_budget_usd=30)
    plan, _ = engine.draft_plan(catalog, profile, user_id="u", days=7)
    for d in plan.days:
        for slot, origin in d.leftover_from.items():
            day, oslot = origin.split("/")
            origin_day = next(x for x in plan.days if x.day == day)
            assert origin_day.meals[oslot] == d.meals[slot]  # type: ignore[index]
