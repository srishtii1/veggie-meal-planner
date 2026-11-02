"""Pydantic domain models shared by tools, agents and the API."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

MealSlot = Literal["breakfast", "lunch", "dinner", "snack"]
MEAL_SLOTS: tuple[MealSlot, ...] = ("breakfast", "lunch", "dinner", "snack")
DAY_NAMES: tuple[str, ...] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
WEEKEND_DAYS: frozenset[str] = frozenset({"Saturday", "Sunday"})


class Diet(StrEnum):
    """Vegetarian sub-diets. Everything here is vegetarian by construction."""

    VEGETARIAN = "vegetarian"  # lacto-ovo
    LACTO = "lacto"  # dairy ok, no eggs (a.k.a. eggless)
    OVO = "ovo"  # eggs ok, no dairy
    VEGAN = "vegan"
    JAIN = "jain"  # no eggs, no root vegetables, no honey


class Allergen(StrEnum):
    DAIRY = "dairy"
    EGG = "egg"
    GLUTEN = "gluten"
    NUTS = "nuts"
    PEANUT = "peanut"
    SOY = "soy"
    SESAME = "sesame"
    HONEY = "honey"


class Nutrition(BaseModel):
    calories: float = 0
    protein_g: float = 0
    carbs_g: float = 0
    fat_g: float = 0
    fiber_g: float = 0

    def __add__(self, other: Nutrition) -> Nutrition:
        return Nutrition(
            calories=self.calories + other.calories,
            protein_g=self.protein_g + other.protein_g,
            carbs_g=self.carbs_g + other.carbs_g,
            fat_g=self.fat_g + other.fat_g,
            fiber_g=self.fiber_g + other.fiber_g,
        )

    def scaled(self, factor: float) -> Nutrition:
        return Nutrition(**{k: round(v * factor, 1) for k, v in self.model_dump().items()})


class Ingredient(BaseModel):
    name: str
    quantity: float = Field(ge=0)
    unit: str
    aisle: str = "other"


class Recipe(BaseModel):
    id: str
    name: str
    cuisine: str
    meal_types: list[MealSlot]
    prep_time_min: int = Field(ge=0)
    contains: list[Allergen] = Field(default_factory=list)
    root_veg: bool = False
    low_fodmap: bool = False
    batch_friendly: bool = False
    ingredients: list[Ingredient]
    nutrition: Nutrition
    cost_usd: float = Field(ge=0, description="Estimated cost per serving")
    instructions: list[str] = Field(default_factory=list)

    @property
    def is_vegan(self) -> bool:
        return not ({Allergen.DAIRY, Allergen.EGG, Allergen.HONEY} & set(self.contains))

    def allowed_for(self, diet: Diet) -> bool:
        c = set(self.contains)
        match diet:
            case Diet.VEGETARIAN:
                return True
            case Diet.LACTO:
                return Allergen.EGG not in c
            case Diet.OVO:
                return Allergen.DAIRY not in c
            case Diet.VEGAN:
                return self.is_vegan
            case Diet.JAIN:
                return Allergen.EGG not in c and Allergen.HONEY not in c and not self.root_veg
        return False  # pragma: no cover - exhaustive

    def summary(self) -> dict[str, object]:
        """Compact representation for LLM context (no instructions / ingredient detail)."""
        return {
            "id": self.id,
            "name": self.name,
            "cuisine": self.cuisine,
            "meal_types": list(self.meal_types),
            "prep_time_min": self.prep_time_min,
            "contains": [a.value for a in self.contains],
            "vegan": self.is_vegan,
            "jain_friendly": self.allowed_for(Diet.JAIN),
            "low_fodmap": self.low_fodmap,
            "batch_friendly": self.batch_friendly,
            "calories": self.nutrition.calories,
            "protein_g": self.nutrition.protein_g,
            "cost_usd": self.cost_usd,
        }


class NutritionTargets(BaseModel):
    calories_per_day: float | None = Field(default=None, gt=0)
    protein_g_per_day: float | None = Field(default=None, gt=0)


class UserProfile(BaseModel):
    """Persistent user preferences. Stored under the ``user:profile`` state key."""

    household_size: int | None = Field(default=None, ge=1, le=20)
    diet: Diet = Diet.VEGETARIAN
    allergens: list[Allergen] = Field(default_factory=list)
    low_fodmap: bool = False
    liked_cuisines: list[str] = Field(default_factory=list)
    disliked_ingredients: list[str] = Field(default_factory=list)
    disliked_recipe_ids: list[str] = Field(default_factory=list)
    cooking_skill: Literal["beginner", "intermediate", "advanced"] = "intermediate"
    max_prep_time_weekday_min: int = Field(default=30, ge=5)
    max_prep_time_weekend_min: int = Field(default=60, ge=5)
    weekly_budget_usd: float | None = Field(default=None, gt=0)
    targets: NutritionTargets = Field(default_factory=NutritionTargets)
    pantry: list[str] = Field(default_factory=list)

    @field_validator("liked_cuisines", "disliked_ingredients", "pantry", mode="before")
    @classmethod
    def _normalise(cls, v: list[str] | None) -> list[str]:
        return sorted({s.strip().lower() for s in (v or []) if s and s.strip()})

    def missing_essentials(self) -> list[str]:
        """Fields the planner must ask about before generating a plan."""
        return [f for f, v in (("household_size", self.household_size),) if v is None]


class DayPlan(BaseModel):
    day: str
    meals: dict[MealSlot, str] = Field(description="slot -> recipe id")
    leftover_from: dict[MealSlot, str] = Field(
        default_factory=dict, description="slot -> 'Day/slot' this meal is a leftover of"
    )


class MealPlan(BaseModel):
    plan_id: str
    user_id: str
    servings: int = Field(ge=1)
    days: list[DayPlan]
    notes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def recipe_ids(self) -> list[str]:
        return [rid for d in self.days for rid in d.meals.values()]


class GroceryItem(BaseModel):
    name: str
    quantity: float
    unit: str
    aisle: str
    recipes: list[str] = Field(default_factory=list)
