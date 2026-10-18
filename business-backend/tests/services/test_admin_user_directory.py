import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.organization import Organization
from src.models.user import User
from src.services.admin_user_directory import AdminUserDirectory


def _user(
    org_id: uuid.UUID,
    email: str,
    *,
    full_name: str,
    status: str = "active",
    last_login_at: datetime | None = None,
) -> User:
    return User(
        keycloak_id=uuid.uuid4(),
        email=email,
        full_name=full_name,
        org_id=org_id,
        role="member",
        status=status,
        last_login_at=last_login_at,
    )


@pytest.mark.asyncio
async def test_directory_filters_org_and_orders_by_display_name(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    """Ordering must not depend on a column that is NULL for most users.

    It used to lead on `last_login_at`, which nothing wrote — every row tied
    on NULL and the listing degenerated into `id ASC`, i.e. random UUID
    order, so paging it never converged on anything a human was looking for.
    """
    test_user.email = "zulu@example.com"
    test_user.full_name = None
    banana = _user(
        test_org.id,
        "banana@example.com",
        full_name="banana Person",
        last_login_at=datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
    )
    apple = _user(
        test_org.id,
        "apple@example.com",
        full_name="Apple Person",
        last_login_at=datetime(2026, 9, 2, 8, tzinfo=timezone.utc),
    )
    foreign_org = Organization(
        name="Foreign directory org",
        slug=f"foreign-directory-{uuid.uuid4().hex}",
    )
    db.add_all([banana, apple, foreign_org])
    await db.flush()
    db.add(
        _user(
            foreign_org.id,
            "aaa-foreign@example.com",
            full_name="AAA Foreign User",
            last_login_at=datetime(2026, 9, 3, 8, tzinfo=timezone.utc),
        )
    )
    await db.commit()

    first_page = await AdminUserDirectory().list(
        db,
        test_org.id,
        limit=2,
        offset=0,
        search=None,
        status=None,
    )
    second_page = await AdminUserDirectory().list(
        db,
        test_org.id,
        limit=2,
        offset=2,
        search=None,
        status=None,
    )

    assert first_page.total == 3
    # Case-insensitive, and a user with no full_name sorts on their email.
    assert [row.email for row in first_page.items] == [
        "apple@example.com",
        "banana@example.com",
    ]
    assert [row.email for row in second_page.items] == ["zulu@example.com"]
    assert "aaa-foreign@example.com" not in {
        row.email for row in first_page.items + second_page.items
    }


@pytest.mark.asyncio
async def test_directory_search_is_case_insensitive_and_treats_wildcards_literally(
    db: AsyncSession,
    test_org: Organization,
) -> None:
    db.add_all(
        [
            _user(
                test_org.id,
                "alice@example.com",
                full_name="Alice Analyst",
            ),
            _user(
                test_org.id,
                "percent@example.com",
                full_name="Percent % User",
            ),
            _user(
                test_org.id,
                "underscore@example.com",
                full_name="Under_score User",
            ),
        ]
    )
    await db.commit()
    directory = AdminUserDirectory()

    alice = await directory.list(
        db, test_org.id, limit=50, offset=0, search="ALICE", status=None
    )
    percent = await directory.list(
        db, test_org.id, limit=50, offset=0, search="%", status=None
    )
    underscore = await directory.list(
        db, test_org.id, limit=50, offset=0, search="_", status=None
    )

    assert [row.email for row in alice.items] == ["alice@example.com"]
    assert [row.email for row in percent.items] == ["percent@example.com"]
    assert [row.email for row in underscore.items] == ["underscore@example.com"]


@pytest.mark.asyncio
async def test_directory_status_filter_is_exact(
    db: AsyncSession,
    test_org: Organization,
) -> None:
    db.add_all(
        [
            _user(test_org.id, "active@example.com", full_name="Active", status="active"),
            _user(test_org.id, "blocked@example.com", full_name="Blocked", status="blocked"),
        ]
    )
    await db.commit()

    page = await AdminUserDirectory().list(
        db,
        test_org.id,
        limit=50,
        offset=0,
        search=None,
        status="blocked",
    )

    assert page.total == 1
    assert [row.email for row in page.items] == ["blocked@example.com"]
