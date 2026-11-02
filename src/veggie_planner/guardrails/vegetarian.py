"""Hard vegetarian guardrail.

This module is the single source of truth for "is this ingredient vegetarian?".
It is deliberately framework-free so it can be unit-tested in isolation and
reused by tools, callbacks, and the critic agent. The LLM never gets to decide
this; it can only call the validator.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

# Terms that make an ingredient non-vegetarian. Matched case-insensitively on
# word boundaries so "ham" does not match "hamburger bun" tokens like "chamomile".
_DENY_TERMS: tuple[str, ...] = (
    # red meat / poultry
    "beef",
    "steak",
    "veal",
    "pork",
    "bacon",
    "ham",
    "prosciutto",
    "pancetta",
    "lard",
    "lamb",
    "mutton",
    "goat meat",
    "venison",
    "chicken",
    "turkey",
    "duck",
    "goose",
    "quail",
    "sausage",
    "salami",
    "pepperoni",
    "chorizo",
    "mince",
    "meatball",
    "meat",
    "bone broth",
    "chicken stock",
    "beef stock",
    "chicken broth",
    "beef broth",
    "suet",
    "tallow",
    "gelatin",
    "gelatine",
    "rennet",
    "isinglass",
    "carmine",
    "cochineal",
    "shellac",
    "worcestershire sauce",
    # seafood
    "fish",
    "anchovy",
    "anchovies",
    "salmon",
    "tuna",
    "cod",
    "haddock",
    "sardine",
    "mackerel",
    "trout",
    "tilapia",
    "shrimp",
    "prawn",
    "crab",
    "lobster",
    "clam",
    "mussel",
    "oyster",
    "squid",
    "calamari",
    "octopus",
    "scallop",
    "eel",
    "roe",
    "caviar",
    "fish sauce",
    "oyster sauce",
    "shrimp paste",
    "dashi",
    "bonito",
    "katsuobushi",
)

# Phrases that contain a deny term but are vegetarian. Checked first.
_ALLOW_PHRASES: tuple[str, ...] = (
    "oyster mushroom",
    "king oyster",
    "beefsteak tomato",
    "chicken of the woods",
    "vegan fish sauce",
    "vegetarian oyster sauce",
    "mushroom oyster sauce",
    "vegetarian fish sauce",
    "kombu dashi",
    "shiitake dashi",
    "vegan dashi",
    "mock duck",
    "mock chicken",
    "vegan chicken",
    "vegetarian chicken",
    "plant-based mince",
    "soy mince",
    "veggie mince",
    "vegetable stock",
    "vegetable broth",
    "agar",
    "crab apple",
    "vegetarian rennet",
    "microbial rennet",
    "vegan gelatin",
    "coconut bacon",
    "tempeh bacon",
    "tofu fish",
    "vegan worcestershire sauce",
    "vegetarian worcestershire sauce",
    "vegetable suet",
    "jackfruit",
    "eggplant",
    "hamburger bun",
    "ham radio",
)

_DENY_RE = re.compile(
    r"\b("
    + "|".join(re.escape(t) for t in sorted(_DENY_TERMS, key=len, reverse=True))
    + r")(?:es|s)?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of a vegetarian check."""

    is_vegetarian: bool
    violations: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {"is_vegetarian": self.is_vegetarian, "violations": list(self.violations)}


def _strip_allowed(text: str) -> str:
    lowered = text.lower()
    for phrase in _ALLOW_PHRASES:
        lowered = lowered.replace(phrase, " ")
    return lowered


def find_violations(text: str) -> list[str]:
    """Return the non-vegetarian terms found in ``text`` (deduplicated, in order)."""
    cleaned = _strip_allowed(text)
    seen: dict[str, None] = {}
    for match in _DENY_RE.finditer(cleaned):
        seen.setdefault(match.group(1).lower(), None)
    return list(seen)


def validate_ingredients(ingredients: Iterable[str]) -> ValidationResult:
    """Validate a list of ingredient names / free-text lines."""
    violations: dict[str, None] = {}
    for item in ingredients:
        for v in find_violations(item):
            violations.setdefault(v, None)
    return ValidationResult(is_vegetarian=not violations, violations=tuple(violations))


def validate_text(text: str) -> ValidationResult:
    """Validate a block of free text (recipe title, description, user request)."""
    violations = find_violations(text)
    return ValidationResult(is_vegetarian=not violations, violations=tuple(violations))
