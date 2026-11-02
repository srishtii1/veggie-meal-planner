"""Request / response schemas for the REST API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000, examples=["Plan 5 vegan dinners for 2"])
    user_id: str = Field(default="demo-user", min_length=1, max_length=128)
    session_id: str | None = Field(default=None, description="Omit to start a new session")


class ToolCallSummary(BaseModel):
    agent: str
    tool: str
    status: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    user_id: str
    reply: str
    tool_calls: list[ToolCallSummary] = Field(default_factory=list)
    current_plan_id: str | None = None
    trace_id: str | None = None


class SessionResponse(BaseModel):
    session_id: str
    user_id: str
    state: dict[str, Any]
    event_count: int


class HealthResponse(BaseModel):
    status: str
    version: str
    model: str
    recipes: int
