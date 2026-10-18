"""enable RLS on organizations and organization_settings

Revision ID: 2026_07_19_org_rls
Revises: 2026_07_19_uq_groups_org_name
Create Date: 2026-07-19 00:10:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '2026_07_19_org_rls'
down_revision: Union[str, None] = '2026_07_19_uq_groups_org_name'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# b4f2b8c4b3f1_add_org_rls_policies.py added org-isolation policies to
# users/conversations/messages/groups/user_settings/group_permissions/
# user_groups/skills, and 2026_06_21_app_role_force_rls.py FORCE'd RLS on
# the same list under the aurum_ai_app runtime role — organizations and
# organization_settings were left out of both, so the aurum_ai_app role
# (full DML, no FORCE RLS) could read/write every org's row if application
# code ever had a bug that skipped setting app.current_org_id.


def upgrade() -> None:
    op.execute("ALTER TABLE organizations ENABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS organizations_org_isolation ON organizations")
    op.execute(
        """
        CREATE POLICY organizations_org_isolation ON organizations
        USING (id::text = current_setting('app.current_org_id', true))
        WITH CHECK (id::text = current_setting('app.current_org_id', true))
        """
    )
    op.execute("ALTER TABLE organizations FORCE ROW LEVEL SECURITY")

    op.execute("ALTER TABLE organization_settings ENABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS organization_settings_org_isolation ON organization_settings")
    op.execute(
        """
        CREATE POLICY organization_settings_org_isolation ON organization_settings
        USING (org_id::text = current_setting('app.current_org_id', true))
        WITH CHECK (org_id::text = current_setting('app.current_org_id', true))
        """
    )
    op.execute("ALTER TABLE organization_settings FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("ALTER TABLE organization_settings NO FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS organization_settings_org_isolation ON organization_settings")
    op.execute("ALTER TABLE organization_settings DISABLE ROW LEVEL SECURITY")

    op.execute("ALTER TABLE organizations NO FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS organizations_org_isolation ON organizations")
    op.execute("ALTER TABLE organizations DISABLE ROW LEVEL SECURITY")
