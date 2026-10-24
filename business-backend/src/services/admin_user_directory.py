from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.group import Group
from src.models.user import User
from src.models.user_group import UserGroup


@dataclass(frozen=True, slots=True)
class AdminUserRecord:
    id: UUID
    email: str
    full_name: str | None
    role: str
    status: str
    last_login_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AdminUserPage:
    items: tuple[AdminUserRecord, ...]
    total: int


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class AdminUserDirectory:
    async def list(
        self,
        session: AsyncSession,
        org_id: UUID,
        *,
        limit: int,
        offset: int,
        search: str | None,
        status: str | None,
        group_id: UUID | None = None,
    ) -> AdminUserPage:
        predicates = [User.org_id == org_id]
        normalized_search = (search or "").strip()
        if normalized_search:
            pattern = f"%{_escape_like(normalized_search)}%"
            predicates.append(
                or_(
                    User.email.ilike(pattern, escape="\\"),
                    User.full_name.ilike(pattern, escape="\\"),
                )
            )
        if status is not None:
            predicates.append(User.status == status)
        if group_id is not None:
            # The group's org is checked here as well: membership rows carry
            # no org of their own, so a foreign group id must match nobody.
            predicates.append(
                User.id.in_(
                    select(UserGroup.user_id)
                    .join(Group, Group.id == UserGroup.group_id)
                    .where(UserGroup.group_id == group_id, Group.org_id == org_id)
                )
            )

        total = int(
            await session.scalar(
                select(func.count()).select_from(User).where(*predicates)
            )
            or 0
        )
        users = list(
            (
                await session.scalars(
                    select(User)
                    .where(*predicates)
                    # Ordered for findability, not recency: the listing backs
                    # people pickers and the admin user table, and a
                    # recency column ties for every user who has not been
                    # seen yet — which used to collapse the whole listing
                    # into `id ASC`, i.e. random UUID order.
                    .order_by(
                        func.lower(func.coalesce(User.full_name, User.email)).asc(),
                        User.id.asc(),
                    )
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        )
        return AdminUserPage(
            items=tuple(
                AdminUserRecord(
                    id=user.id,
                    email=user.email,
                    full_name=user.full_name,
                    role=user.role or "member",
                    status=user.status or "active",
                    last_login_at=user.last_login_at,
                    created_at=user.created_at,
                )
                for user in users
            ),
            total=total,
        )


admin_user_directory = AdminUserDirectory()
