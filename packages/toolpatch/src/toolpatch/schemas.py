"""OpenAI-compatible Pydantic schemas shared by the middleware pipeline."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Role = Literal["system", "user", "assistant", "tool"]


class ToolFunction(BaseModel):
    """A function definition inside a `tools` entry."""

    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


class Tool(BaseModel):
    type: Literal["function"] = "function"
    function: ToolFunction


class ToolCallFn(BaseModel):
    name: str
    arguments: str  # JSON-encoded string, per OpenAI spec


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: ToolCallFn


class Message(BaseModel):
    role: Role
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


class ChatRequest(BaseModel):
    """Partial OpenAI chat-completion request; unknown fields pass through."""

    model: str
    messages: list[Message]
    tools: list[Tool] | None = None
    stream: bool = False

    model_config = ConfigDict(extra="allow")
