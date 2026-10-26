from typing import Any

import pytest

from src import hermes_progress
from src.hermes_progress import MAX_CALLS, TurnProgress, public_tool


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def publish(org_id: str, conversation_id: str, payload: dict[str, Any]) -> None:
        sent.append(payload)

    monkeypatch.setattr(hermes_progress.publisher, "publish", publish)
    return sent


@pytest.mark.parametrize(
    ("name", "public"),
    [
        ("terminal", "terminal"),
        ("write_file", "files"),
        ("mcp__aurum__knowledge_search", "knowledge"),
        ("mcp__aurum__web_search", "web_search"),
        ("mcp__other__web_search", "other"),
        ("mcp_aurum_web_search", "other"),
        ("mcp__aurum__unknown", "other"),
        ("ls -la /workspace/secret", "other"),
        (None, "other"),
    ],
)
def test_only_configured_tools_keep_their_name(name: object, public: str) -> None:
    assert public_tool(name) == public


@pytest.mark.asyncio
async def test_tools_of_one_name_finish_in_order(published) -> None:
    progress = TurnProgress("org", "conv", "turn", attempt=2)
    for event in (
        {"event": "tool.started", "tool": "terminal"},
        {"event": "tool.started", "tool": "terminal"},
        {"event": "tool.completed", "tool": "terminal"},
        {"event": "tool.completed", "tool": "terminal", "error": True},
        {"event": "tool.completed", "tool": "terminal"},
    ):
        await progress.event(event)

    assert [call["call_id"] for call in progress.calls] == ["hermes-2-0", "hermes-2-1"]
    assert progress.results == {
        "hermes-2-0": {"tool": "hermes.terminal", "status": "completed"},
        "hermes-2-1": {"tool": "hermes.terminal", "status": "failed"},
    }
    assert [payload["type"] for payload in published] == ["tool-calls", "tool-calls", "tool-result", "tool-result"]


@pytest.mark.asyncio
async def test_cards_are_bounded_per_turn(published) -> None:
    progress = TurnProgress("org", "conv", "turn", attempt=1)
    for _ in range(MAX_CALLS + 5):
        await progress.event({"event": "tool.started", "tool": "todo"})

    assert len(progress.calls) == MAX_CALLS == len(published)


@pytest.mark.asyncio
async def test_without_an_organization_nothing_is_published(published) -> None:
    progress = TurnProgress("", "conv", "turn", attempt=1)
    await progress.event({"event": "tool.started", "tool": "todo"})
    await progress.keepalive()

    assert published == [] and len(progress.calls) == 1


@pytest.mark.asyncio
async def test_hermes_call_ids_pair_cards_and_survive_a_replay(published) -> None:
    # Hermes 0.22 sends tool_call_id and replays a run's events to a reattach.
    events = [
        {"event": "tool.started", "tool": "terminal", "tool_call_id": "call_a", "preview": "SECRET args"},
        {"event": "tool.started", "tool": "terminal", "tool_call_id": "call_b"},
        {"event": "tool.completed", "tool": "terminal", "tool_call_id": "call_b", "preview": "SECRET output"},
        {"event": "tool.completed", "tool": "terminal", "tool_call_id": "call_a", "error": True},
    ]
    first, retry = TurnProgress("org", "conv", "turn", attempt=1), TurnProgress("org", "conv", "turn", attempt=2)
    for progress in (first, retry):
        for event in events:
            await progress.event(event)

    assert first.calls == retry.calls
    a, b = (call["call_id"] for call in first.calls)
    assert first.results == {
        a: {"tool": "hermes.terminal", "status": "failed"},
        b: {"tool": "hermes.terminal", "status": "completed"},
    }
    serialized = repr(published)
    assert "SECRET" not in serialized and "call_a" not in serialized


@pytest.mark.parametrize(("name", "public"), [("process_manage", "terminal"), ("todo_list", "plan")])
def test_hermes_022_tool_names_keep_their_kind(name: str, public: str) -> None:
    assert public_tool(name) == public
