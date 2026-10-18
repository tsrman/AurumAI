from typing import Optional, List, Any
from pydantic import BaseModel, Field


class ConversationCreate(BaseModel):
    title: Optional[str] = "New conversation"
    model: str
    system_prompt: Optional[str] = None
    metadata: Optional[dict[str, Any]] = Field(default_factory=dict)


class ConversationUpdate(BaseModel):
    title: Optional[str] = None
    system_prompt: Optional[str] = None
    status: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None
    # None (explicitly sent, distinguished from "field omitted" via
    # exclude_unset=True in the router) clears the override and falls back to
    # the organization default.
    temperature: Optional[float] = Field(default=None, ge=0, le=2)


class ConversationResponse(BaseModel):
    id: str
    title: Optional[str]
    model: str
    status: str
    lastMessageAt: Optional[str]
    createdAt: str
    messageCount: int = 0
    tokenCount: int = 0
    costUsd: float = 0.0
    # Always the resolved value (conversation override, else organization
    # default, else the platform default) — never null, so the runner and UI
    # never need to know about the cascade.
    temperature: float = 0.7


class MessageResponse(BaseModel):
    id: str
    role: str
    content: Optional[str]
    model: Optional[str]
    tokensIn: int = 0
    tokensOut: int = 0
    costUsd: float = 0.0
    latencyMs: Optional[int]
    toolCalls: Optional[List[Any]]
    # Keyed by tool_calls[].call_id — see conversation-runner's
    # ConversationWorkflow, which builds this alongside tool_calls before
    # calling persist_assistant_message.
    toolResults: Optional[dict[str, Any]] = None
    createdAt: str
    faithfulnessScore: Optional[float] = None
    faithfulnessReason: Optional[str] = None
    turnId: Optional[str] = None
    sourceIds: List[str] = Field(default_factory=list)
    unsupportedSourceIds: List[str] = Field(default_factory=list)
