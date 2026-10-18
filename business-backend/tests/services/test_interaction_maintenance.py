"""Runtime protection for durable user-question waits."""

import importlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _wait_protected() -> Callable[..., bool] | None:
    try:
        module = importlib.import_module("src.services.interaction_maintenance")
    except ModuleNotFoundError:
        return None
    candidate = getattr(module, "wait_protected", None)
    return candidate if callable(candidate) else None


def test_pending_wait_is_protected_only_before_original_deadline() -> None:
    wait_protected = _wait_protected()
    assert wait_protected is not None, "interaction wait protection is not implemented"
    now = datetime(2026, 9, 6, 12, tzinfo=UTC)
    expires_at = now + timedelta(hours=1)
    settlement_until = expires_at + timedelta(minutes=5)

    assert wait_protected("pending", expires_at, settlement_until, now, True)
    assert not wait_protected("pending", expires_at, settlement_until, expires_at, True)


def test_answered_wait_has_bounded_settlement_window() -> None:
    wait_protected = _wait_protected()
    assert wait_protected is not None, "interaction wait protection is not implemented"
    now = datetime(2026, 9, 6, 12, tzinfo=UTC)
    expires_at = now - timedelta(seconds=1)
    settlement_until = now + timedelta(minutes=5)

    assert wait_protected("answered", expires_at, settlement_until, now, True)
    assert not wait_protected(
        "answered", expires_at, settlement_until, settlement_until, True
    )


def test_terminal_turn_never_protects_runner() -> None:
    wait_protected = _wait_protected()
    assert wait_protected is not None, "interaction wait protection is not implemented"
    now = datetime(2026, 9, 6, 12, tzinfo=UTC)

    assert not wait_protected(
        "pending",
        now + timedelta(hours=1),
        now + timedelta(hours=1, minutes=5),
        now,
        False,
    )


def test_expired_question_is_not_a_wait_lease() -> None:
    wait_protected = _wait_protected()
    assert wait_protected is not None, "interaction wait protection is not implemented"
    now = datetime(2026, 9, 6, 12, tzinfo=UTC)

    assert not wait_protected(
        "expired", now, now + timedelta(minutes=5), now, True
    )


@pytest.mark.asyncio
async def test_maintenance_refreshes_each_exact_runtime_once(monkeypatch) -> None:
    module = importlib.import_module("src.services.interaction_maintenance")
    now = datetime(2026, 9, 6, 12, tzinfo=UTC)
    waits = [
        SimpleNamespace(
            org_id="org-1",
            conversation_id="conv-1",
            owner_id="user-1",
            status="pending",
            expires_at=now + timedelta(hours=1),
            settlement_until=now + timedelta(hours=1, minutes=5),
            reconciliation=False,
        ),
        SimpleNamespace(
            org_id="org-1",
            conversation_id="conv-1",
            owner_id="user-1",
            status="answered",
            expires_at=now,
            settlement_until=now + timedelta(minutes=5),
            reconciliation=False,
        ),
    ]
    load = AsyncMock(return_value=waits)
    monkeypatch.setattr(module, "load_protected_waits", load, raising=False)
    runtime = SimpleNamespace(refresh_owned_waiting_runtime=AsyncMock(return_value=True))
    maintain = getattr(module, "maintain_interaction_waits", None)
    assert maintain is not None, "interaction wait maintenance is not implemented"

    maintained = await maintain(runtime, object())

    assert maintained == 1
    runtime.refresh_owned_waiting_runtime.assert_awaited_once_with(
        "conv-1", "user-1", "org-1"
    )


@pytest.mark.asyncio
async def test_durable_guard_scopes_same_conversation_to_organization(monkeypatch) -> None:
    module = importlib.import_module("src.services.interaction_maintenance")
    load = AsyncMock(
        return_value=[
            SimpleNamespace(
                org_id="org-2",
                conversation_id="conv-1",
                owner_id="user-2",
            )
        ]
    )
    monkeypatch.setattr(module, "load_protected_waits", load, raising=False)
    guard = getattr(module, "durable_wait_protected", None)
    assert guard is not None, "durable wait guard is not implemented"

    assert not await guard("org-1", "conv-1", object())
    assert await guard(None, "conv-1", object())


@pytest.mark.asyncio
async def test_maintenance_continues_after_one_runtime_refresh_fails(monkeypatch) -> None:
    module = importlib.import_module("src.services.interaction_maintenance")
    waits = [
        SimpleNamespace(org_id="org-1", conversation_id="conv-1", owner_id="user-1"),
        SimpleNamespace(org_id="org-2", conversation_id="conv-2", owner_id="user-2"),
    ]
    monkeypatch.setattr(module, "load_protected_waits", AsyncMock(return_value=waits))
    refresh = AsyncMock(side_effect=[RuntimeError("runtime lookup failed"), True])
    runtime = SimpleNamespace(refresh_owned_waiting_runtime=refresh)

    maintained = await module.maintain_interaction_waits(runtime, object())

    assert maintained == 1
    assert refresh.await_count == 2
