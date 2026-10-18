from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class SkillCreate(BaseModel):
    # Accepts both snake_case (existing convention for this schema, still
    # used by SkillFormDialog.tsx) and camelCase (matching SkillResponse and
    # the rest of the API's request bodies) for each field.
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    name: str
    description: Optional[str] = None
    type: str = Field(..., pattern="^(agent|tool|mcp)$")
    workflow_type: Optional[str] = None
    activity_name: Optional[str] = None
    mcp_transport: Optional[str] = None
    mcp_endpoint: Optional[str] = None
    mcp_auth_token: Optional[str] = None
    input_schema: Optional[dict[str, Any]] = Field(default_factory=dict)
    allowed_child_skills: Optional[list[Any]] = None
    # "egress" grants the sandbox/skill outbound network access — admin-only
    # endpoints accept this, never the LLM or a bundle (see skill-executor).
    # Non-optional with a default: the DB column is NOT NULL.
    network_policy: str = Field("none", pattern="^(none|egress)$")


class SkillUpdate(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    name: Optional[str] = None
    description: Optional[str] = None
    type: Optional[str] = Field(None, pattern="^(agent|tool|mcp)$")
    workflow_type: Optional[str] = None
    activity_name: Optional[str] = None
    mcp_transport: Optional[str] = None
    mcp_endpoint: Optional[str] = None
    mcp_auth_token: Optional[str] = None
    input_schema: Optional[dict[str, Any]] = None
    allowed_child_skills: Optional[list[Any]] = None
    network_policy: Optional[str] = Field(None, pattern="^(none|egress)$")


class CodeSkillUpdate(BaseModel):
    """PATCH payload for type="code" skills — bundle fields are immutable
    here on purpose; replacing the bundle goes through the dedicated
    PUT /v1/skills/code/{id}/bundle endpoint instead."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    name: Optional[str] = None
    description: Optional[str] = None
    network_policy: Optional[str] = Field(None, pattern="^(none|egress)$")


class McpDiscoverRequest(BaseModel):
    mcpEndpoint: str
    mcpTransport: str = "sse"
    mcpAuthToken: Optional[str] = None
    # mcpAuthToken is write-only to browsers (see
    # SkillResponse.hasMcpAuthToken) — editing an existing MCP skill with a
    # saved token has no way to put that token back in this request. When
    # set (and mcpAuthToken is empty), the endpoint uses the skill's own
    # stored token server-side instead.
    skillId: Optional[str] = None


class McpToolInfo(BaseModel):
    name: str
    description: Optional[str] = None
    inputSchema: dict[str, Any]


class SkillResponse(BaseModel):
    id: str
    orgId: Optional[str]
    name: str
    description: Optional[str] = None
    type: str
    workflowType: Optional[str]
    activityName: Optional[str]
    mcpTransport: Optional[str]
    mcpEndpoint: Optional[str]
    mcpAuthToken: Optional[str] = None
    # mcpAuthToken is always None to browser clients
    # (see _skill_response's include_secrets) — this is the only way a
    # browser can tell "no token configured" apart from "token configured
    # but hidden," e.g. to decide which placeholder text to show.
    hasMcpAuthToken: bool = False
    inputSchema: dict[str, Any]
    allowedChildSkills: Optional[list[Any]]
    bundleSkillName: Optional[str] = None
    bundleSizeBytes: Optional[int] = None
    bundleChecksum: Optional[str] = None
    # Internal MinIO object key — not useful to browser clients, but
    # tool-worker needs it to tell skill-executor where to fetch the bundle
    # from (see tool-worker/src/skill_executor_client.py).
    bundleStorageKey: Optional[str] = None
    networkPolicy: str = "none"
    createdAt: str
    updatedAt: str
