"""Human-in-the-loop confirmation policies.

Uses ADK's native tool confirmation (``FunctionTool(require_confirmation=...)``): when a
policy returns ``True`` the tool does **not** run; ADK emits an
``adk_request_confirmation`` long-running call, the turn ends, and the action executes only
after the client replies with ``{"confirmed": true}`` (REST: ``POST /chat/confirm``).

What needs a human:

* ``swap_meal`` from the coordinator -- it edits a plan the user has already seen.
* ``rate_meal`` with rating 1 -- permanently bans a recipe.
* ``update_user_profile`` when it *loosens* safety constraints -- removes an allergen or
  changes the diet variant. Adding allergens / tightening is applied immediately.

The planner's internal ``swap_meal`` calls inside the draft->critique loop are deliberately
*not* gated: that plan has not been shown to the user yet, and ``AgentTool`` runs cannot
pause/resume.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from google.adk.tools.function_tool import FunctionTool

from veggie_planner.tools import rate_meal, swap_meal, update_user_profile
from veggie_planner.tools.profile import load_profile


def confirm_swap(**_: Any) -> bool:
    """Every user-visible plan edit is confirmed."""
    return True


def confirm_rating(rating: int | None = None, **_: Any) -> bool:
    """Only a 1-star rating (permanent ban) needs confirmation."""
    return rating is not None and rating <= 1


def confirm_profile_change(
    tool_context: Any = None,
    diet: str | None = None,
    allergens: list[str] | None = None,
    **_: Any,
) -> bool:
    """Confirm when the change would loosen allergen or diet constraints."""
    current = load_profile(getattr(tool_context, "state", None))
    # Changing an established diet needs approval; first-time setup is not a loosening.
    established = current.household_size is not None
    if established and diet is not None and diet.lower() != current.diet.value:
        return True
    if allergens is not None:
        removed = {a.value for a in current.allergens} - {a.lower() for a in allergens}
        if removed:
            return True
    return False


def gated(func: Callable[..., Any], policy: Callable[..., bool], enabled: bool) -> Any:
    return FunctionTool(func, require_confirmation=policy) if enabled else func


def coordinator_write_tools(enabled: bool) -> list[Any]:
    """The coordinator's side-effecting tools, wrapped with confirmation when enabled."""
    return [
        gated(update_user_profile, confirm_profile_change, enabled),
        gated(rate_meal, confirm_rating, enabled),
        gated(swap_meal, confirm_swap, enabled),
    ]
