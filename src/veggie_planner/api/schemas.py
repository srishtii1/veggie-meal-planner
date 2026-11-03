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


class PendingConfirmation(BaseModel):
    """An action the agent wants to take that is waiting for the user's approval."""

    confirmation_id: str = Field(description="Pass back to POST /chat/confirm")
    tool: str
    args: dict[str, Any] = Field(default_factory=dict, description="PII-redacted arguments")
    hint: str = ""
    summary: str = Field(description="Human-readable description of the pending action")


class ConfirmRequest(BaseModel):
    user_id: str = Field(default="demo-user", min_length=1, max_length=128)
    session_id: str
    confirmation_id: str
    approved: bool
    reason: str | None = Field(default=None, max_length=500)


class ChatResponse(BaseModel):
    session_id: str
    user_id: str
    reply: str
    tool_calls: list[ToolCallSummary] = Field(default_factory=list)
    pending_confirmations: list[PendingConfirmation] = Field(default_factory=list)
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
