import pytest
from pydantic import ValidationError

from src.core.config import Settings


def test_identity_sync_is_opt_in_with_bounded_wait():
    settings = Settings(DEBUG=True, _env_file=None)
    assert settings.IDENTITY_SYNC_ENABLED is False
    assert settings.IDENTITY_SYNC_WAIT_SECONDS == 10


@pytest.mark.parametrize('wait', [0, -1, 61])
def test_identity_wait_rejects_unbounded_or_nonpositive_deadline(wait):
    with pytest.raises(ValidationError):
        Settings(DEBUG=True, _env_file=None, IDENTITY_SYNC_WAIT_SECONDS=wait)


def test_identity_sync_operator_can_enable_and_adjust_wait(monkeypatch):
    monkeypatch.setenv('APP_IDENTITY_SYNC_ENABLED', 'true')
    monkeypatch.setenv('APP_IDENTITY_SYNC_WAIT_SECONDS', '3')
    settings = Settings(DEBUG=True, _env_file=None)
    assert settings.IDENTITY_SYNC_ENABLED is True
    assert settings.IDENTITY_SYNC_WAIT_SECONDS == 3
