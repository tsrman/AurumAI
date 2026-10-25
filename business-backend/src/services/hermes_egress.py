"""Internet egress for Hermes Agent sandboxes (docs/HERMES_AGENT.md).

The operator sets the most a sandbox may reach; the organization chooses within
it (default `packages`), and `public` also needs the user's live `web.access`
capability, without which a session gets `packages`. hermes-egress asks for the
effective mode on every new connection and re-checks open tunnels, so all of
these take effect without restarting a sandbox.
"""

from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import AuthorizationSubject
from src.core.capability_catalog import Capability
from src.core.config import settings
from src.models.organization_settings import OrganizationSettings
from src.services.authorization import authorization_service

EgressMode = Literal["none", "packages", "public"]
EGRESS_MODES: tuple[EgressMode, ...] = ("none", "packages", "public")
DEFAULT_EGRESS_MODE: EgressMode = "packages"


class EgressModeAboveMaximum(Exception):
    pass


def require_egress_secret() -> None:
    """Refuse to start the business API with Hermes egress on and no real secret.

    Only the business API serves hermes-egress's checks, so the admin API and
    workers start without the secret.
    """
    if (
        settings.HERMES_ENABLED
        and settings.HERMES_EGRESS_MAX_MODE != "none"
        and len(settings.HERMES_EGRESS_SERVICE_SECRET.get_secret_value()) < 16
    ):
        raise RuntimeError(
            "APP_HERMES_EGRESS_SERVICE_SECRET must contain at least 16 characters "
            "when APP_HERMES_ENABLED is true and Hermes egress is not none"
        )


def _lowest(*modes: EgressMode) -> EgressMode:
    return min(modes, key=EGRESS_MODES.index)


async def organization_egress_mode(session: AsyncSession, org_id: UUID) -> EgressMode:
    stored = await session.scalar(
        select(OrganizationSettings.hermes_egress_mode).where(
            OrganizationSettings.org_id == org_id
        )
    )
    return cast(EgressMode, stored) if stored in EGRESS_MODES else DEFAULT_EGRESS_MODE


async def set_organization_egress_mode(
    session: AsyncSession, org_id: UUID, mode: EgressMode
) -> None:
    if EGRESS_MODES.index(mode) > EGRESS_MODES.index(settings.HERMES_EGRESS_MAX_MODE):
        raise EgressModeAboveMaximum
    await session.execute(
        insert(OrganizationSettings)
        .values(org_id=org_id, hermes_egress_mode=mode)
        .on_conflict_do_update(
            index_elements=[OrganizationSettings.org_id], set_={"hermes_egress_mode": mode}
        )
    )


async def effective_egress_mode(
    session: AsyncSession, subject: AuthorizationSubject
) -> EgressMode:
    mode = _lowest(
        await organization_egress_mode(session, subject.org_id), settings.HERMES_EGRESS_MAX_MODE
    )
    if mode == "public" and not await authorization_service.has_capability(
        session, subject, Capability.WEB_ACCESS
    ):
        return "packages"
    return mode
