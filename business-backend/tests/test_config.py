"""Unit tests for Settings._forbid_insecure_defaults_outside_debug —
the security review's fail-fast guard against known dev-only secrets
(docker-compose.yml's `:-` fallbacks) reaching a non-DEBUG deployment."""

import pytest
from pydantic import ValidationError

from src.core.config import Settings


def _make(**overrides):
    base = {
        "DEBUG": False,
        "SECRET_KEY": "x" * 32,
        "DATABASE_URL": "postgresql+asyncpg://app:realpass@postgres:5432/aurum_ai",
    }
    base.update(overrides)
    return Settings(**base)


def test_debug_mode_allows_every_insecure_default():
    settings = _make(DEBUG=True, SECRET_KEY="change-me-in-production")
    assert settings.SECRET_KEY == "change-me-in-production"


def test_rejects_default_secret_key_outside_debug():
    with pytest.raises(ValidationError, match="APP_SECRET_KEY must be set"):
        _make(SECRET_KEY="change-me-in-production")


def test_rejects_default_database_url_outside_debug():
    with pytest.raises(ValidationError, match="APP_DATABASE_URL must be set"):
        _make(DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/aurum_ai")


def test_rejects_default_s3_secret_key_outside_debug_when_s3_is_configured():
    with pytest.raises(ValidationError, match="APP_S3_SECRET_KEY must be set"):
        _make(S3_ENDPOINT="http://minio:9000", S3_SECRET_KEY="miniosecret")


def test_allows_default_s3_secret_key_when_s3_is_not_configured():
    # S3_ENDPOINT unset -> object storage isn't used at all; nothing to guard.
    settings = _make(S3_ENDPOINT=None, S3_SECRET_KEY="miniosecret")
    assert settings.S3_SECRET_KEY == "miniosecret"


def test_allows_real_s3_secret_key_outside_debug():
    settings = _make(S3_ENDPOINT="http://minio:9000", S3_SECRET_KEY="a-real-secret")
    assert settings.S3_SECRET_KEY == "a-real-secret"


@pytest.mark.parametrize(
    "obsolete_setting",
    [
        "AUTHORIZATION" + "_V2_SHADOW",
        "AUTHORIZATION" + "_V2_SKILLS",
        "AUTHORIZATION" + "_V2_MODELS",
        "AUTHORIZATION" + "_V2_KNOWLEDGE",
        "AUTHORIZATION" + "_V2_AGENTS",
        "AUTHORIZATION" + "_V2_LIVE_SESSION",
    ],
)
def test_rejects_obsolete_authorization_transition_settings(
    obsolete_setting: str,
) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _make(**{obsolete_setting: True})


def test_workflow_operator_settings_have_safe_defaults() -> None:
    settings = _make()

    assert settings.WORKFLOW_MAX_NODES == 20
    assert settings.WORKFLOW_MAX_PARALLELISM == 4
    assert settings.WORKFLOW_DIAGNOSTIC_RETENTION_DAYS == 30
    assert settings.WORKFLOW_RETENTION_SWEEP_INTERVAL_SECONDS > 0


def test_interaction_wait_settings_keep_refresh_ahead_of_token_expiry() -> None:
    settings = _make()

    assert settings.INTERACTION_MAINTENANCE_INTERVAL_SECONDS == 30
    assert settings.INTERACTION_SETTLEMENT_GRACE_SECONDS == 300
    assert settings.RUNNER_TOKEN_REFRESH_AFTER_SECONDS == 1200
    assert (
        settings.RUNNER_TOKEN_REFRESH_AFTER_SECONDS
        + settings.INTERACTION_MAINTENANCE_INTERVAL_SECONDS
        < 30 * 60
    )


@pytest.mark.parametrize(
    "field",
    [
        "INTERACTION_MAINTENANCE_INTERVAL_SECONDS",
        "INTERACTION_SETTLEMENT_GRACE_SECONDS",
        "RUNNER_TOKEN_REFRESH_AFTER_SECONDS",
    ],
)
def test_interaction_wait_settings_reject_non_positive_values(field: str) -> None:
    with pytest.raises(ValidationError):
        _make(**{field: 0})


def test_interaction_wait_settings_reject_refresh_at_token_expiry() -> None:
    with pytest.raises(ValidationError, match="session token expires"):
        _make(
            RUNNER_TOKEN_REFRESH_AFTER_SECONDS=1770,
            INTERACTION_MAINTENANCE_INTERVAL_SECONDS=30,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("WORKFLOW_MAX_NODES", 0),
        ("WORKFLOW_MAX_NODES", 21),
        ("WORKFLOW_MAX_PARALLELISM", 0),
        ("WORKFLOW_MAX_PARALLELISM", 5),
        ("WORKFLOW_DIAGNOSTIC_RETENTION_DAYS", 0),
        ("WORKFLOW_RETENTION_SWEEP_INTERVAL_SECONDS", 0),
    ],
)
def test_workflow_operator_settings_reject_unsafe_bounds(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        _make(**{field: value})


def test_automatic_dotenv_is_service_owned_not_current_working_directory(tmp_path, monkeypatch):
    """A root Compose env or another project's env must not configure this API."""
    (tmp_path / ".env").write_text("PUBLIC_HOST=example.test\nAPP_DATABASE_ROLE_PASSWORD=fixture-password\n")
    monkeypatch.chdir(tmp_path)
    settings = _make(DEBUG=True)
    assert settings.SECRET_KEY == "x" * 32


def test_explicit_dotenv_still_rejects_obsolete_settings(tmp_path):
    path = tmp_path / "custom.env"
    path.write_text("APP_OBSOLETE_SETTING=unexpected\n")
    with pytest.raises(ValidationError, match="Extra inputs"):
        _make(DEBUG=True, _env_file=path)
