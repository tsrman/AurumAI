"""Never adopt changed payloads as the established archive acceptance fixture."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4
import json
from unittest.mock import AsyncMock, MagicMock

import pytest


def load_fixture():
    path = Path(__file__).parents[1] / "prepare-data-lifecycle-fixture.py"
    spec = importlib.util.spec_from_file_location("lifecycle_history_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample():
    state = {
        "conversation_id": str(uuid4()), "org_id": str(uuid4()), "user_id": str(uuid4()),
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
        "turns": [{"id": str(uuid4()), "user_message": str(uuid4()),
                   "assistant_message": str(uuid4())} for _ in range(26)],
    }
    module = load_fixture()
    rows = list(module.expected_messages(state))
    return module, state, rows


def test_exact_52_rows_include_final_turn_and_immutable_identity():
    module, state, rows = sample()
    module.verify_messages(state, rows)
    assert len(rows) == 52
    assert rows[-1]["content"] == "Archive fixture answer 26"
    assert str(rows[-1]["id"]) == state["turns"][-1]["assistant_message"]


@pytest.mark.parametrize("field,value", [
    ("content", "changed"), ("id", uuid4()), ("turn_id", uuid4()),
    ("org_id", uuid4()), ("conversation_id", uuid4()), ("role", "user"),
])
def test_same_count_changed_payload_is_not_adopted(field, value):
    module, state, rows = sample()
    rows[-1][field] = value
    with pytest.raises(ValueError, match="established_fixture_content_mismatch"):
        module.verify_messages(state, rows)


def test_missing_last_page_and_duplicated_rows_are_rejected():
    module, state, rows = sample()
    for changed in (rows[:50], rows[:-1] + [rows[0]]):
        with pytest.raises(ValueError, match="established_fixture_content_mismatch"):
            module.verify_messages(state, changed)


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["prepared", "run_id", "runner_id", "attachment"])
async def test_missing_established_identity_never_inserts(marker, tmp_path, monkeypatch):
    module, state, _ = sample()
    state.update(postgres_container_id="owned-postgres", **{marker: True})
    journal = tmp_path / "state.json"
    journal.write_text(json.dumps(state))
    monkeypatch.setattr(module, "STATE", journal)
    monkeypatch.setattr(module, "owned_postgres", lambda: "owned-postgres")
    connection = AsyncMock()
    connection.fetch.return_value = [{"id": UUID(state["user_id"]),
                                     "org_id": UUID(state["org_id"])}]
    connection.fetchrow.return_value = None
    connection.transaction = MagicMock(return_value=AsyncMock())
    monkeypatch.setattr(module.asyncpg, "connect", AsyncMock(return_value=connection))
    with pytest.raises(ValueError, match="established_fixture_identity_missing"):
        await module.prepare()
    connection.execute.assert_not_awaited()
    assert json.loads(journal.read_text()) == state
