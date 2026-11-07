"""The connection pool is sized by settings, not by constants in the source."""

from src.core.config import Settings
from src.core.database import engine


def test_pool_defaults_keep_the_historical_sizes() -> None:
    fields = Settings.model_fields

    assert fields["DB_POOL_SIZE"].default == 20
    assert fields["DB_MAX_OVERFLOW"].default == 40
    assert fields["DB_POOL_TIMEOUT"].default == 30


def test_the_engine_uses_the_configured_pool() -> None:
    from src.core.config import settings

    pool = engine.pool

    assert pool.size() == settings.DB_POOL_SIZE
    assert pool._max_overflow == settings.DB_MAX_OVERFLOW  # type: ignore[attr-defined]
    assert pool._timeout == settings.DB_POOL_TIMEOUT  # type: ignore[attr-defined]
