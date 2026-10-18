from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src import skill_executor_client
from src.skill_executor_client import SkillExecutorError


def fake_async_client(response: MagicMock | None = None, side_effect: Exception | None = None):
    instance = AsyncMock()
    if side_effect is not None:
        instance.post = AsyncMock(side_effect=side_effect)
        instance.delete = AsyncMock(side_effect=side_effect)
    else:
        instance.post = AsyncMock(return_value=response)
        instance.delete = AsyncMock(return_value=response)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=instance)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm), instance


def make_response(status_code: int = 200, json_data: dict | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data or {}
    if status_code >= 400:
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=response
        )
    else:
        response.raise_for_status = MagicMock()
    return response


@pytest.mark.asyncio
async def test_open_session_posts_bundle_info_and_returns_response():
    skill = {
        "id": "skill-1",
        "bundle_storage_key": "skills/org/abc.zip",
        "bundle_checksum": "abc123",
        "network_policy": "none",
    }
    response = make_response(200, {"session_id": "sess-1", "skill_md": "docs"})
    factory, instance = fake_async_client(response)

    with patch("src.skill_executor_client.httpx.AsyncClient", factory):
        result = await skill_executor_client.open_session(skill, conversation_id="conv-1")

    assert result == {"session_id": "sess-1", "skill_md": "docs"}
    instance.post.assert_awaited_once()
    _, kwargs = instance.post.call_args
    assert kwargs["json"] == {
        "skill_id": "skill-1",
        "bundle_storage_key": "skills/org/abc.zip",
        "bundle_checksum": "abc123",
        "conversation_id": "conv-1",
        "network_policy": "none",
    }


@pytest.mark.asyncio
async def test_open_session_raises_without_bundle():
    with pytest.raises(SkillExecutorError, match="no uploaded bundle"):
        await skill_executor_client.open_session({"id": "skill-1"})


@pytest.mark.asyncio
async def test_open_session_wraps_http_errors():
    factory, _ = fake_async_client(side_effect=httpx.ConnectError("refused"))
    skill = {"id": "skill-1", "bundle_storage_key": "x", "bundle_checksum": "y"}

    with patch("src.skill_executor_client.httpx.AsyncClient", factory):
        with pytest.raises(SkillExecutorError, match="Could not open skill-executor session"):
            await skill_executor_client.open_session(skill)


@pytest.mark.asyncio
async def test_exec_command_returns_json():
    response = make_response(200, {"stdout": "hi", "stderr": "", "exit_code": 0, "timed_out": False})
    factory, instance = fake_async_client(response)

    with patch("src.skill_executor_client.httpx.AsyncClient", factory):
        result = await skill_executor_client.exec_command("sess-1", "echo hi")

    assert result["stdout"] == "hi"
    instance.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_exec_command_wraps_http_errors():
    factory, _ = fake_async_client(side_effect=httpx.ConnectError("refused"))

    with patch("src.skill_executor_client.httpx.AsyncClient", factory):
        with pytest.raises(SkillExecutorError, match="skill-executor exec failed"):
            await skill_executor_client.exec_command("sess-1", "echo hi")


@pytest.mark.asyncio
async def test_close_session_swallows_http_errors():
    factory, instance = fake_async_client(side_effect=httpx.ConnectError("refused"))

    with patch("src.skill_executor_client.httpx.AsyncClient", factory):
        await skill_executor_client.close_session("sess-1")  # must not raise

    instance.delete.assert_awaited_once()
