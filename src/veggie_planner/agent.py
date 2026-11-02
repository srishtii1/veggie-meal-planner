"""ADK CLI entrypoint: ``adk web src`` / ``adk run src/veggie_planner`` discover ``root_agent`` here."""

from veggie_planner.agents.agent import root_agent

__all__ = ["root_agent"]
