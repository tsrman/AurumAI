from typing import Any, cast

import httpx

from src.config import settings


class SkillExecutorError(Exception):
    """Any failure talking to skill-executor — callers should treat this as
    a non-retryable skill-execution failure, not silently retry (a stuck
    sandbox session isn't fixed by calling open_session again with the same
    args)."""


async def open_session(skill: dict[str, Any], conversation_id: str | None = None) -> dict[str, Any]:
    bundle_storage_key = skill.get("bundle_storage_key")
    bundle_checksum = skill.get("bundle_checksum")
    if not bundle_storage_key or not bundle_checksum:
        raise SkillExecutorError(
            f"Code skill {skill.get('id')} has no uploaded bundle "
            "(bundle_storage_key/bundle_checksum missing)"
        )
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            response = await client.post(
                f"{settings.skill_executor_url}/sessions",
                json={
                    "skill_id": skill.get("id") or "",
                    "bundle_storage_key": bundle_storage_key,
                    "bundle_checksum": bundle_checksum,
                    "conversation_id": conversation_id,
                    "network_policy": skill.get("network_policy") or "none",
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SkillExecutorError(f"Could not open skill-executor session: {exc}") from exc
        return cast(dict[str, Any], response.json())


async def exec_command(session_id: str, command: str, timeout: float | None = None) -> dict[str, Any]:
    http_timeout = (timeout or 60.0) + 10.0
    async with httpx.AsyncClient(timeout=http_timeout) as client:
        try:
            response = await client.post(
                f"{settings.skill_executor_url}/sessions/{session_id}/exec",
                json={"command": command, "timeout_seconds": timeout},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SkillExecutorError(f"skill-executor exec failed: {exc}") from exc
        return cast(dict[str, Any], response.json())


async def close_session(session_id: str) -> None:
    """Best-effort — skill-executor's own TTL reaper is the backstop if this
    never runs or fails (crash, network partition), so swallow errors here
    rather than let cleanup failure mask the real execution result."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            await client.delete(f"{settings.skill_executor_url}/sessions/{session_id}")
        except httpx.HTTPError:
            pass
