"""Retention policy defaults and invalid operator configuration."""

import pytest
from pydantic import ValidationError

from src.core.config import Settings


def test_archive_defaults_require_explicit_rollout() -> None:
    settings = Settings(DEBUG=True, _env_file=None)
    assert settings.ARCHIVE_ENABLED is False
    assert settings.ARCHIVE_RETENTION_DAYS == 180
    assert settings.ARCHIVE_SWEEP_INTERVAL_SECONDS == 86400
    assert settings.ARCHIVE_BATCH_SIZE == 100


@pytest.mark.parametrize(
    "field", ["ARCHIVE_RETENTION_DAYS", "ARCHIVE_SWEEP_INTERVAL_SECONDS", "ARCHIVE_BATCH_SIZE"]
)
@pytest.mark.parametrize("value", [0, -1])
def test_archive_intervals_and_batch_are_positive(field: str, value: int) -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(DEBUG=True, _env_file=None, **{field: value})
    assert caught.value.errors()[0]["type"] == "greater_than"


def test_archive_retention_is_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ARCHIVE_RETENTION_DAYS", "30")
    settings = Settings(DEBUG=True, _env_file=None)
    assert settings.ARCHIVE_RETENTION_DAYS == 30


def test_archive_credentials_are_separate_and_masked() -> None:
    settings = Settings(
        DEBUG=True,
        _env_file=None,
        S3_ACCESS_KEY="business-fixture-access",
        S3_SECRET_KEY="business-fixture-secret",
        ARCHIVE_S3_ACCESS_KEY="archive-fixture-access",
        ARCHIVE_S3_SECRET_KEY="archive-fixture-secret",
        ARCHIVE_RAG_SERVICE_SECRET="archive-rag-fixture-secret",
    )
    assert settings.ARCHIVE_S3_BUCKET == "aurum-ai-archives"
    assert settings.ARCHIVE_S3_SECRET_KEY.get_secret_value() == "archive-fixture-secret"
    assert settings.ARCHIVE_RAG_SERVICE_SECRET.get_secret_value() == "archive-rag-fixture-secret"
    assert "archive-fixture-secret" not in repr(settings)
    assert "archive-rag-fixture-secret" not in repr(settings)
    assert settings.S3_ACCESS_KEY == "business-fixture-access"


def test_api_can_start_without_archive_credentials() -> None:
    settings = Settings(DEBUG=True, _env_file=None)
    assert settings.ARCHIVE_S3_ACCESS_KEY == ""
    assert settings.ARCHIVE_S3_SECRET_KEY.get_secret_value() == ""
    assert settings.ARCHIVE_RAG_SERVICE_SECRET.get_secret_value() == ""


def test_private_rag_endpoint_is_separate_from_normal_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ARCHIVE_RAG_URL", "http://private-rag-maintenance:8111")
    settings = Settings(DEBUG=True, _env_file=None, RAG_API_URL="http://normal-rag:8110")
    assert settings.ARCHIVE_RAG_URL == "http://private-rag-maintenance:8111"
    assert settings.RAG_API_URL == "http://normal-rag:8110"
