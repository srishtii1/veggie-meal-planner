"""Recipe catalog: loads the seed JSON once and offers deterministic filtering."""

from __future__ import annotations

import json
from collections.abc import Iterable
from functools import lru_cache
from importlib import resources
from typing import Any

from veggie_planner.guardrails.vegetarian import validate_ingredients
from veggie_planner.models import Allergen, Diet, Ingredient, MealSlot, Recipe


class RecipeCatalog:
    def __init__(self, recipes: Iterable[Recipe]) -> None:
        self._by_id: dict[str, Recipe] = {}
        for r in recipes:
            result = validate_ingredients([i.name for i in r.ingredients] + [r.name])
            if not result.is_vegetarian:  # defence in depth: the seed data must be clean
                raise ValueError(f"Recipe {r.id} is not vegetarian: {result.violations}")
            self._by_id[r.id] = r

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, recipe_id: object) -> bool:
        return recipe_id in self._by_id

    def get(self, recipe_id: str) -> Recipe | None:
        return self._by_id.get(recipe_id)

    def all(self) -> list[Recipe]:
        return list(self._by_id.values())

    def search(
        self,
        *,
        meal_type: MealSlot | None = None,
        cuisine: str | None = None,
        diet: Diet = Diet.VEGETARIAN,
        exclude_allergens: Iterable[Allergen] = (),
        low_fodmap_only: bool = False,
        max_prep_time_min: int | None = None,
        exclude_ids: Iterable[str] = (),
        exclude_ingredients: Iterable[str] = (),
        prefer_ingredients: Iterable[str] = (),
        min_protein_g: float | None = None,
        max_cost_usd: float | None = None,
        batch_friendly_only: bool = False,
        limit: int = 20,
    ) -> list[Recipe]:
        """Filter then rank. Ranking prefers pantry overlap, then protein density."""
        excluded_allergens = set(exclude_allergens)
        if Allergen.NUTS in excluded_allergens:  # "nut-free" conservatively covers peanuts
            excluded_allergens.add(Allergen.PEANUT)
        excluded_ids = set(exclude_ids)
        excluded_ingredients = {s.lower() for s in exclude_ingredients}
        preferred = {s.lower() for s in prefer_ingredients}

        def ok(r: Recipe) -> bool:
            if r.id in excluded_ids or not r.allowed_for(diet):
                return False
            if excluded_allergens & set(r.contains):
                return False
            if meal_type and meal_type not in r.meal_types:
                return False
            if cuisine and r.cuisine.lower() != cuisine.lower():
                return False
            if low_fodmap_only and not r.low_fodmap:
                return False
            if max_prep_time_min is not None and r.prep_time_min > max_prep_time_min:
                return False
            if min_protein_g is not None and r.nutrition.protein_g < min_protein_g:
                return False
            if max_cost_usd is not None and r.cost_usd > max_cost_usd:
                return False
            if batch_friendly_only and not r.batch_friendly:
                return False
            names = " ".join(i.name.lower() for i in r.ingredients)
            return not any(bad in names for bad in excluded_ingredients)

        def pantry_overlap(r: Recipe) -> int:
            return sum(1 for i in r.ingredients if any(p in i.name.lower() for p in preferred))

        matches = [r for r in self._by_id.values() if ok(r)]
        matches.sort(key=lambda r: (-pantry_overlap(r), -r.nutrition.protein_g, r.cost_usd, r.id))
        return matches[:limit]


def _parse(raw: dict[str, Any]) -> Recipe:
    rows: list[list[Any]] = raw.pop("ingredients")
    ingredients = [
        Ingredient(name=str(n), quantity=float(q), unit=str(u), aisle=str(a)) for n, q, u, a in rows
    ]
    return Recipe(ingredients=ingredients, **raw)


@lru_cache(maxsize=1)
def load_catalog() -> RecipeCatalog:
    text = resources.files("veggie_planner.data").joinpath("recipes.json").read_text("utf-8")
    payload = json.loads(text)
    return RecipeCatalog(_parse(dict(r)) for r in payload["recipes"])
