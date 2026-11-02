import pytest

from veggie_planner.guardrails import find_violations, validate_ingredients, validate_text


@pytest.mark.parametrize(
    "ingredient",
    [
        "chicken breast",
        "Beef mince",
        "fish sauce",
        "oyster sauce",
        "gelatin",
        "rennet",
        "bacon bits",
        "anchovies",
        "prawns",
        "lard",
        "Worcestershire sauce",
        "dashi stock",
        "bone broth",
        "shrimp paste",
        "pepperoni",
    ],
)
def test_non_vegetarian_detected(ingredient: str) -> None:
    assert find_violations(ingredient), ingredient


@pytest.mark.parametrize(
    "ingredient",
    [
        "oyster mushroom",
        "king oyster mushrooms",
        "beefsteak tomato",
        "eggplant",
        "vegetable stock",
        "mushroom oyster sauce",
        "kombu dashi",
        "coconut bacon",
        "tempeh bacon",
        "jackfruit",
        "chamomile tea",
        "hamburger bun",
        "paneer",
        "tofu",
        "agar agar",
        "microbial rennet",
        "vegan fish sauce",
        "butternut squash",
        "chickpeas",
    ],
)
def test_vegetarian_not_flagged(ingredient: str) -> None:
    assert find_violations(ingredient) == [], ingredient


def test_validate_ingredients_collects_unique_violations() -> None:
    result = validate_ingredients(["chicken", "Chicken stock", "rice", "tuna"])
    assert not result.is_vegetarian
    assert result.violations == ("chicken", "chicken stock", "tuna")
    assert result.as_dict()["violations"] == ["chicken", "chicken stock", "tuna"]


def test_validate_text_clean() -> None:
    result = validate_text("Palak paneer with roti and a side of dal")
    assert result.is_vegetarian and result.violations == ()


def test_word_boundaries() -> None:
    # "ham" must not match inside "shampoo"/"chamomile"; "cod" not inside "cocoa".
    assert find_violations("shampoo chamomile cocoa powder") == []
    assert find_violations("ham sandwich") == ["ham"]
