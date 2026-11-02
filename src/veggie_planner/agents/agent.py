"""Agent tree.

coordinator (LlmAgent, chat)
├── tools: profile / recipe / swap / rating / memory tools
├── AgentTool(planning_pipeline)      SequentialAgent
│     ├── planning_loop               LoopAgent(max_iterations=N)
│     │     ├── planner               LlmAgent  draft / fix
│     │     └── critic                LlmAgent  evaluate -> exit_loop when clean
│     └── presenter                   deterministic markdown rendering
└── AgentTool(grocery_agent)          LlmAgent

``Workflow`` (the newer ADK graph API) cannot be used as an ``LlmAgent`` sub-agent or
``AgentTool`` yet, so the loop uses ``LoopAgent``; see ARCHITECTURE.md.
"""

from __future__ import annotations

import warnings

from google.adk.agents import LlmAgent, LoopAgent, SequentialAgent
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.exit_loop_tool import exit_loop
from google.adk.tools.load_memory_tool import load_memory

from veggie_planner.agents import prompts
from veggie_planner.agents.callbacks import block_non_vegetarian_tool_args, scrub_prompt_injection
from veggie_planner.agents.presenter import PlanPresenterAgent
from veggie_planner.config import get_settings
from veggie_planner.tools import (
    build_grocery_list,
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

warnings.filterwarnings("ignore", category=DeprecationWarning, module="google.adk")


def build_root_agent(model: str | None = None, planner_model: str | None = None) -> LlmAgent:
    """Construct the agent tree. ``model`` may be a model id or a ``BaseLlm`` (tests)."""
    settings = get_settings()
    model = model or settings.model
    planner_model = planner_model or model or settings.effective_planner_model

    planner = LlmAgent(
        name="planner",
        model=planner_model,
        description="Drafts a meal plan or applies the critic's fixes.",
        instruction=prompts.PLANNER_INSTRUCTION,
        tools=[draft_meal_plan, swap_meal, search_recipes, get_meal_plan, evaluate_meal_plan],
        output_key="planner_summary",
        before_tool_callback=block_non_vegetarian_tool_args,
        before_model_callback=scrub_prompt_injection,
    )
    critic = LlmAgent(
        name="critic",
        model=model,
        description="Checks the plan for compliance, nutrition, variety and budget.",
        instruction=prompts.CRITIC_INSTRUCTION,
        tools=[evaluate_meal_plan, exit_loop],
        output_key="critic_feedback",
        before_model_callback=scrub_prompt_injection,
    )
    planning_loop = LoopAgent(
        name="planning_loop",
        sub_agents=[planner, critic],
        max_iterations=settings.max_loop_iterations,
    )
    planning_pipeline = SequentialAgent(
        name="planning_pipeline",
        description=(
            "Builds a complete vegetarian meal plan for the user: drafts it, critiques it "
            "for diet/allergen compliance, protein, variety, prep time and budget, fixes "
            "issues, and returns a markdown summary with the plan_id. Input: a short "
            "request describing days, meal slots, servings, start day and special wishes."
        ),
        sub_agents=[planning_loop, PlanPresenterAgent(name="presenter")],
    )
    grocery_agent = LlmAgent(
        name="grocery_agent",
        model=model,
        description=(
            "Produces an aisle-grouped grocery list (scaled to servings, pantry items "
            "removed) and optional cost estimate for the current or a named meal plan."
        ),
        instruction=prompts.GROCERY_INSTRUCTION,
        tools=[build_grocery_list, get_meal_plan, estimate_cost],
        before_tool_callback=block_non_vegetarian_tool_args,
        before_model_callback=scrub_prompt_injection,
    )
    return LlmAgent(
        name="meal_planner_coordinator",
        model=model,
        description="Vegetarian meal-planning assistant.",
        instruction=prompts.COORDINATOR_INSTRUCTION,
        tools=[
            get_user_profile,
            update_user_profile,
            list_diet_options,
            rate_meal,
            search_recipes,
            get_recipe,
            validate_vegetarian,
            get_meal_plan,
            swap_meal,
            estimate_cost,
            load_memory,
            AgentTool(agent=planning_pipeline),
            AgentTool(agent=grocery_agent),
        ],
        before_tool_callback=block_non_vegetarian_tool_args,
        before_model_callback=scrub_prompt_injection,
    )


root_agent = build_root_agent()
