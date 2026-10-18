from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

MARKER = "knowledge-ui-e2e-0123456789"
ORG_ID = "11111111-1111-1111-1111-111111111111"
MEMBER_ID = "22222222-2222-2222-2222-222222222222"
KNOWLEDGE_BASE_ID = "33333333-3333-3333-3333-333333333333"
DOCUMENT_ID = "44444444-4444-4444-4444-444444444444"
ROOT = Path(__file__).parents[2]


def _load_harness() -> ModuleType:
    path = ROOT / "scripts" / "knowledge_ui_e2e.py"
    spec = importlib.util.spec_from_file_location("knowledge_ui_e2e_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_harness = _load_harness()
Config = _harness.Config
FixtureController = _harness.FixtureController
FixtureError = _harness.FixtureError
FixtureState = _harness.FixtureState
build_parser = _harness.build_parser
load_state = _harness.load_state
primary_exit_code = _harness.primary_exit_code
save_state = _harness.save_state
validate_owned_state = _harness.validate_owned_state


def owned_state(**changes: object) -> FixtureState:
    values: dict[str, object] = {
        "marker": MARKER,
        "organization_id": ORG_ID,
        "member_id": MEMBER_ID,
        "original_capabilities": ["chat.send"],
        "knowledge_base_name": f"{MARKER}-base",
        "source_filename": f"{MARKER}.txt",
        "knowledge_base_id": KNOWLEDGE_BASE_ID,
        "document_id": DOCUMENT_ID,
        "storage_key": f"documents/{ORG_ID}/{KNOWLEDGE_BASE_ID}/{MARKER}.txt",
    }
    values.update(changes)
    return FixtureState(**values)


def controller(tmp_path: Path) -> FixtureController:
    return FixtureController(
        Config(), state_path=tmp_path / "state.json", marker=MARKER
    )


def test_private_state_round_trip_is_strict_and_contains_no_secrets(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = owned_state()

    save_state(path, state)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_state(path) == state
    payload = path.read_text(encoding="utf-8").lower()
    for forbidden in ("token", "password", "secret", "content", "query"):
        assert forbidden not in payload

    document = json.loads(path.read_text(encoding="utf-8"))
    document["unexpected"] = "field"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(FixtureError, match="unknown state fields"):
        load_state(path)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"marker": "knowledge-ui-e2e-short"}, "marker"),
        ({"organization_id": "not-a-uuid"}, "organization_id"),
        ({"knowledge_base_name": "another-base"}, "knowledge_base_name"),
        ({"source_filename": "other.txt"}, "source_filename"),
        ({"storage_key": "documents/foreign/key"}, "storage_key"),
    ],
)
def test_owned_state_rejects_malformed_or_unowned_values(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(FixtureError, match=message):
        validate_owned_state(owned_state(**changes), require_resources=True)


def test_prepare_snapshots_both_audiences_without_granting_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = controller(tmp_path)
    calls: list[tuple[str, str]] = []
    capabilities = ["workspace.knowledge.read", "chat.send"]

    def auth_me(plane: str, username: str, _password: str) -> dict[str, object]:
        calls.append((plane, username))
        return {"user_id": MEMBER_ID, "org_id": ORG_ID}

    monkeypatch.setattr(subject, "_auth_me", auth_me)
    monkeypatch.setattr(subject, "_get_capabilities", lambda: capabilities)
    monkeypatch.setattr(subject, "_business_scalar", lambda _sql: "0")

    def put(_values: list[str]) -> None:
        pytest.fail("prepare must not change capabilities")

    monkeypatch.setattr(subject, "_put_capabilities", put)

    subject.prepare()

    state = load_state(subject.state_path)
    assert calls == [("business", "user"), ("admin", "user")]
    assert state.organization_id == ORG_ID
    assert state.member_id == MEMBER_ID
    assert state.original_capabilities == sorted(capabilities)
    assert state.knowledge_base_name == f"{MARKER}-base"
    assert state.source_filename == f"{MARKER}.txt"


def test_prepare_refuses_existing_journal_and_exact_db_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = controller(tmp_path)
    subject.state_path.write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(FixtureError, match="already exists"):
        subject.prepare()

    subject.state_path.unlink()
    monkeypatch.setattr(
        subject,
        "_auth_me",
        lambda _plane, _username, _password: {
            "user_id": MEMBER_ID,
            "org_id": ORG_ID,
        },
    )
    monkeypatch.setattr(subject, "_get_capabilities", lambda: ["chat.send"])
    monkeypatch.setattr(subject, "_business_scalar", lambda _sql: "1")
    with pytest.raises(FixtureError, match="collision"):
        subject.prepare()


def test_prepare_refuses_an_organization_that_already_has_create_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = controller(tmp_path)
    monkeypatch.setattr(
        subject,
        "_auth_me",
        lambda _plane, _username, _password: {
            "user_id": MEMBER_ID,
            "org_id": ORG_ID,
        },
    )
    monkeypatch.setattr(
        subject,
        "_get_capabilities",
        lambda: ["chat.send", "workspace.knowledge.create"],
    )

    with pytest.raises(FixtureError, match="already has workspace.knowledge.create"):
        subject.prepare()


def test_capture_verifies_relationship_and_resolves_storage_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = controller(tmp_path)
    save_state(subject.state_path, owned_state(
        knowledge_base_id="", document_id="", storage_key=""
    ))
    key = f"documents/{ORG_ID}/{KNOWLEDGE_BASE_ID}/{MARKER}.txt"
    business_values = iter([KNOWLEDGE_BASE_ID, key])
    monkeypatch.setattr(subject, "_business_scalar", lambda _sql: next(business_values))
    monkeypatch.setattr(subject, "_rag_scalar", lambda _sql: DOCUMENT_ID)

    subject.capture(KNOWLEDGE_BASE_ID, DOCUMENT_ID)

    captured = load_state(subject.state_path)
    assert captured.knowledge_base_id == KNOWLEDGE_BASE_ID
    assert captured.document_id == DOCUMENT_ID
    assert captured.storage_key == key


def test_capture_rejects_a_cross_resource_relationship(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = controller(tmp_path)
    save_state(subject.state_path, owned_state(
        knowledge_base_id="", document_id="", storage_key=""
    ))
    monkeypatch.setattr(subject, "_business_scalar", lambda _sql: KNOWLEDGE_BASE_ID)
    monkeypatch.setattr(subject, "_rag_scalar", lambda _sql: "")

    with pytest.raises(FixtureError, match="RAG document"):
        subject.capture(KNOWLEDGE_BASE_ID, DOCUMENT_ID)


def test_capture_cli_never_accepts_a_caller_supplied_storage_key() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "capture",
                "--state",
                "/tmp/state",
                "--marker",
                MARKER,
                "--knowledge-base-id",
                KNOWLEDGE_BASE_ID,
                "--document-id",
                DOCUMENT_ID,
                "--storage-key",
                "attacker-value",
            ]
        )


def test_cleanup_uses_public_apis_then_exact_fallback_and_restores_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = controller(tmp_path)
    save_state(subject.state_path, owned_state())
    events: list[str] = []
    monkeypatch.setattr(subject, "_resolve_owned_resources", lambda state: state)
    monkeypatch.setattr(
        subject, "_delete_document_via_business_api", lambda _state: events.append("document-api")
    )
    monkeypatch.setattr(
        subject, "_delete_knowledge_base_via_business_api", lambda _state: events.append("base-api")
    )
    monkeypatch.setattr(
        subject, "_cleanup_exact_fallbacks", lambda _state: events.append("fallback")
    )
    monkeypatch.setattr(
        subject, "_restore_original_capabilities", lambda _state: events.append("capabilities")
    )
    monkeypatch.setattr(subject, "verify_clean", lambda _state=None: events.append("verify"))

    subject.cleanup()

    assert events == [
        "document-api",
        "base-api",
        "fallback",
        "capabilities",
        "verify",
    ]


def test_cleanup_continues_to_fallback_when_public_apis_are_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = controller(tmp_path)
    save_state(subject.state_path, owned_state())
    events: list[str] = []
    monkeypatch.setattr(subject, "_resolve_owned_resources", lambda state: state)
    monkeypatch.setattr(
        subject,
        "_delete_document_via_business_api",
        lambda _state: (_ for _ in ()).throw(FixtureError("business DELETE failed")),
    )
    monkeypatch.setattr(
        subject,
        "_delete_knowledge_base_via_business_api",
        lambda _state: (_ for _ in ()).throw(FixtureError("business DELETE failed")),
    )
    monkeypatch.setattr(
        subject, "_cleanup_exact_fallbacks", lambda _state: events.append("fallback")
    )
    monkeypatch.setattr(
        subject, "_restore_original_capabilities", lambda _state: events.append("capabilities")
    )
    monkeypatch.setattr(subject, "verify_clean", lambda _state=None: events.append("verify"))

    subject.cleanup()

    assert events == ["fallback", "capabilities", "verify"]


def test_missing_minio_object_is_an_idempotent_cleanup_success(
    tmp_path: Path,
) -> None:
    subject = controller(tmp_path)
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="Object does not exist"
        )

    subject._run = run  # type: ignore[method-assign]

    subject._delete_minio_object(owned_state().storage_key)

    assert len(calls) == 1
    assert "stat" in calls[0]


def test_cleanup_restores_capabilities_even_when_an_exact_fallback_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = controller(tmp_path)
    state = owned_state()
    save_state(subject.state_path, state)
    events: list[str] = []
    monkeypatch.setattr(subject, "_resolve_owned_resources", lambda current: current)
    monkeypatch.setattr(subject, "_delete_document_via_business_api", lambda _state: None)
    monkeypatch.setattr(subject, "_delete_knowledge_base_via_business_api", lambda _state: None)
    monkeypatch.setattr(
        subject,
        "_cleanup_exact_fallbacks",
        lambda _state: (_ for _ in ()).throw(FixtureError("fallback failed")),
    )
    monkeypatch.setattr(
        subject,
        "_restore_original_capabilities",
        lambda _state: events.append("capabilities"),
    )
    monkeypatch.setattr(subject, "verify_clean", lambda _state=None: events.append("verify"))

    with pytest.raises(FixtureError, match="fallback failed"):
        subject.cleanup()

    assert events == ["capabilities", "verify"]


def test_destructive_sql_is_scoped_by_org_and_exact_owned_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = controller(tmp_path)
    statements: list[str] = []
    monkeypatch.setattr(subject, "_business_execute", statements.append)
    monkeypatch.setattr(subject, "_rag_execute", statements.append)
    monkeypatch.setattr(subject, "_delete_minio_object", lambda _key: None)
    monkeypatch.setattr(subject, "_delete_milvus_chunks", lambda _doc, _kb: None)

    subject._cleanup_exact_fallbacks(owned_state())

    deletes = [statement for statement in statements if "DELETE FROM" in statement]
    assert deletes
    for statement in deletes:
        assert ORG_ID in statement
        assert any(
            owned in statement
            for owned in (KNOWLEDGE_BASE_ID, DOCUMENT_ID, f"{MARKER}-base", f"{MARKER}.txt")
        )
        assert "LIKE" not in statement.upper()


def test_primary_failure_always_wins_over_cleanup_failure() -> None:
    assert primary_exit_code(7, 9) == 7
    assert primary_exit_code(0, 9) == 9
    assert primary_exit_code(0, 0) == 0


def test_subprocess_errors_do_not_include_command_credentials(tmp_path: Path) -> None:
    subject = controller(tmp_path)
    result = subprocess.CompletedProcess([], 1, stdout="secret output", stderr="password")
    subject._run = lambda *_args, **_kwargs: result  # type: ignore[method-assign]

    with pytest.raises(FixtureError, match="database command failed") as failure:
        subject._business_scalar("SELECT 1")
    assert "secret" not in str(failure.value)
    assert "password" not in str(failure.value)


def test_wrapper_runs_the_owned_compose_browser_contract() -> None:
    path = ROOT / "smoke-test-knowledge-ui.sh"
    wrapper = path.read_text(encoding="utf-8")

    assert "--profile rag" in wrapper
    assert "knowledge.real.spec.ts" in wrapper
    assert "--workers=1" in wrapper
    assert "--trace=off" in wrapper
    assert "test-gateway" not in wrapper
    assert "conversation-runner" not in wrapper
    assert 'com.docker.compose.project' in wrapper
    assert 'COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-$existing_project}"' in wrapper
    assert ".State.Health.Status" in wrapper
    assert "docker exec aurum-ai-rag-api curl" not in wrapper
    assert "http://reranker:7997/models" in wrapper
    assert "RERANKER_READY_ATTEMPTS:-900" in wrapper
    assert 'knowledge_ui_e2e.py" cleanup' in wrapper
    assert stat.S_IMODE(path.stat().st_mode) == 0o755


def test_documentation_names_the_supported_exclusive_command() -> None:
    for relative in ("README.md", "AGENTS.md", "docs/ARCHITECTURE.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "./smoke-test-knowledge-ui.sh" in text, relative
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "must not" in agents
