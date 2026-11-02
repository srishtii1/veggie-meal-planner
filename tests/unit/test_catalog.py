from veggie_planner.data import load_catalog
from veggie_planner.models import Allergen, Diet


def test_catalog_loads_and_is_vegetarian() -> None:
    catalog = load_catalog()
    assert len(catalog) >= 60
    ids = [r.id for r in catalog.all()]
    assert len(ids) == len(set(ids)), "duplicate recipe ids"
    for r in catalog.all():
        assert r.meal_types and r.ingredients and r.nutrition.calories > 0
        assert r.instructions


def test_every_slot_has_options_for_every_diet() -> None:
    catalog = load_catalog()
    for diet in Diet:
        for slot in ("breakfast", "lunch", "dinner", "snack"):
            assert catalog.search(meal_type=slot, diet=diet, limit=1), f"{diet}/{slot}"


def test_search_filters() -> None:
    catalog = load_catalog()
    vegan_gf = catalog.search(diet=Diet.VEGAN, exclude_allergens=[Allergen.GLUTEN], limit=100)
    assert vegan_gf
    for r in vegan_gf:
        assert r.is_vegan and Allergen.GLUTEN not in r.contains
    quick = catalog.search(max_prep_time_min=10, limit=100)
    assert all(r.prep_time_min <= 10 for r in quick)
    indian = catalog.search(cuisine="indian", limit=100)
    assert indian and all(r.cuisine == "Indian" for r in indian)
    no_mushroom = catalog.search(exclude_ingredients=["mushroom"], limit=100)
    assert all("mushroom" not in i.name.lower() for r in no_mushroom for i in r.ingredients)


def test_nut_free_excludes_peanut_too() -> None:
    catalog = load_catalog()
    for r in catalog.search(exclude_allergens=[Allergen.NUTS], limit=100):
        assert Allergen.PEANUT not in r.contains


def test_pantry_ranking_prefers_overlap() -> None:
    catalog = load_catalog()
    top = catalog.search(meal_type="dinner", prefer_ingredients=["tofu"], limit=3)
    assert any("tofu" in i.name for i in top[0].ingredients)


def test_jain_excludes_root_veg_and_eggs() -> None:
    catalog = load_catalog()
    for r in catalog.search(diet=Diet.JAIN, limit=100):
        assert not r.root_veg and Allergen.EGG not in r.contains


def test_recipe_summary_is_compact() -> None:
    r = load_catalog().get("chana-masala")
    assert r is not None
    s = r.summary()
    assert "instructions" not in s and s["id"] == "chana-masala" and s["vegan"] is True
