from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.organization_settings import OrganizationSettings
from src.models.user_settings import UserSettings


MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "2026_09_02_org_settings_analytics.py"
)


def test_default_model_columns_accept_canonical_resource_ids() -> None:
    """A gateway model ID accepted by Authorization v2 must fit in either
    persisted default field, and a new organization must not silently acquire
    a hard-coded model that may not exist or be granted."""

    assert OrganizationSettings.__table__.c.default_model.type.length == 255
    assert UserSettings.__table__.c.default_model.type.length == 255
    assert OrganizationSettings().default_model is None


def test_downgrade_rejects_model_ids_that_do_not_fit_legacy_columns() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert "char_length(default_model) > 100" in source
    assert "Cannot downgrade organization model settings" in source


async def test_usage_analytics_schema_has_indexable_status_timelines(
    db: AsyncSession,
) -> None:
    """The report reads accepted and completed events on different
    timestamps. Losing either partial index turns a bounded report into an
    organization-wide ledger scan."""

    lengths = dict(
        (
            await db.execute(
                text(
                    "SELECT table_name, character_maximum_length "
                    "FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND column_name = 'default_model' "
                    "AND table_name IN ('organization_settings', 'user_settings')"
                )
            )
        ).all()
    )
    indexes = dict(
        (
            await db.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = current_schema() "
                    "AND indexname IN ("
                    "'ix_llm_usage_events_org_accepted_at', "
                    "'ix_llm_usage_events_org_completed_at')"
                )
            )
        ).all()
    )

    assert lengths == {
        "organization_settings": 255,
        "user_settings": 255,
    }
    assert "(org_id, accepted_at) WHERE ((status)::text = 'accepted'::text)" in indexes[
        "ix_llm_usage_events_org_accepted_at"
    ]
    assert "(org_id, completed_at) WHERE ((status)::text = 'completed'::text)" in indexes[
        "ix_llm_usage_events_org_completed_at"
    ]
