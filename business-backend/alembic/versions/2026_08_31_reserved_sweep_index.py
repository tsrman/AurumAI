"""index the stale-reservation sweep

QuotaService reclaims reservations that were counted but never resolved, and
it runs on every preflight() and reserve() — the sweep has to be on both,
because preflight() gates /v1/chat/stream and raises before reserve() is ever
reached, so a leaked counter would otherwise lock an org out for the rest of
the month with nothing able to clear it.

That puts the sweep's predicate on the hot path of every chat request. Only
ix_llm_usage_events_org_id covers it, so a busy organization would pay an
index scan over all of its events for the month on each request. A partial
index over just the `reserved` rows keeps it proportional to what the sweep
can actually delete: at any instant that is the handful of requests sitting
between reserve() and the provider's response headers, not a month of history.

Revision ID: 2026_08_31_reserved_sweep
Revises: 2026_08_31_llm_usage
Create Date: 2026-08-31 18:30:00.000000

"""

from typing import Sequence, Union

from alembic import op

revision: str = "2026_08_31_reserved_sweep"
down_revision: Union[str, None] = "2026_08_31_llm_usage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "ix_llm_usage_events_reserved_sweep"


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "llm_usage_events",
        ["org_id", "period_start", "created_at"],
        postgresql_where="status = 'reserved'",
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="llm_usage_events")
