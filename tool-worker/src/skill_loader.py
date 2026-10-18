from typing import Any

import httpx

from src.config import settings

# business-backend's /v1/skills/{id} returns camelCase (SkillResponse), but
# execute_skill's dispatch logic reads snake_case keys.
_CAMEL_TO_SNAKE = {
    "orgId": "org_id",
    "workflowType": "workflow_type",
    "activityName": "activity_name",
    "mcpTransport": "mcp_transport",
    "mcpEndpoint": "mcp_endpoint",
    "mcpAuthToken": "mcp_auth_token",
    "inputSchema": "input_schema",
    "allowedChildSkills": "allowed_child_skills",
    "bundleSkillName": "bundle_skill_name",
    "bundleSizeBytes": "bundle_size_bytes",
    "bundleChecksum": "bundle_checksum",
    "bundleStorageKey": "bundle_storage_key",
    "networkPolicy": "network_policy",
    "createdAt": "created_at",
    "updatedAt": "updated_at",
}


def _normalize_skill_keys(data: dict[str, Any]) -> dict[str, Any]:
    return {_CAMEL_TO_SNAKE.get(key, key): value for key, value in data.items()}


async def fetch_skill(skill_id: str, session_token: str | None = None) -> dict[str, Any]:
    token = session_token or settings.session_token
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{settings.backend_api_url}/v1/skills/{skill_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if response.status_code == 404:
                raise ValueError(f"Skill {skill_id} not found") from exc
            raise

        data = response.json().get("data")
        if not isinstance(data, dict):
            raise ValueError(
                f"Invalid skill response for {skill_id}: missing or invalid 'data'"
            )

        return _normalize_skill_keys(data)
