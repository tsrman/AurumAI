"""add unique constraint on groups(org_id, name)

Revision ID: 2026_07_19_uq_groups_org_name
Revises: 2026_07_18_drop_documents_tables
Create Date: 2026-07-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '2026_07_19_uq_groups_org_name'
down_revision: Union[str, None] = '2026_07_18_drop_documents_tables'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # sync_keycloak_user does select-then-insert on (org_id, name) with no
    # locking — two concurrent syncs for a brand-new group both pass the
    # SELECT, then both INSERT, producing duplicate Group rows with
    # independent permission sets. This constraint turns that race into a
    # catchable IntegrityError instead of silent data corruption.
    op.create_unique_constraint(
        'uq_groups_org_id_name', 'groups', ['org_id', 'name']
    )


def downgrade() -> None:
    op.drop_constraint('uq_groups_org_id_name', 'groups', type_='unique')
