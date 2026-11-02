"""ADK agent package. ``adk web src/veggie_planner`` discovers ``agents`` via ``root_agent``."""

from veggie_planner.agents import agent
from veggie_planner.agents.agent import build_root_agent, root_agent

__all__ = ["agent", "build_root_agent", "root_agent"]
