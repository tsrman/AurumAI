import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.llm_usage import (
    LLMUsageEvent,
    OrganizationUsageMonthly,
    UsageEventStatus,
)
from src.models.organization import Organization
from src.models.user import User
from src.services.usage_analytics import UsageAnalyticsService, UsageTotals


MODEL_A = "provider/model-a"
MODEL_B = "provider/model-b"


def _event(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    model: str,
    status: UsageEventStatus,
    accepted_at: datetime | None = None,
    completed_at: datetime | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    kind: str = "chat",
) -> LLMUsageEvent:
    return LLMUsageEvent(
        org_id=org_id,
        user_id=user_id,
        request_id=uuid.uuid4(),
        period_start=date(2026, 9, 1),
        model=model,
        status=status.value,
        accepted_at=accepted_at,
        completed_at=completed_at,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        kind=kind,
    )


@pytest.mark.asyncio
async def test_report_classifies_statuses_fills_dates_and_excludes_foreign_org(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    test_user.email = "first@example.com"
    test_user.full_name = "First User"
    second_user = User(
        keycloak_id=uuid.uuid4(),
        email="second@example.com",
        full_name="Second User",
        org_id=test_org.id,
        role="member",
        status="active",
    )
    foreign_org = Organization(
        name="Foreign analytics org",
        slug=f"foreign-analytics-{uuid.uuid4().hex}",
    )
    db.add_all([second_user, foreign_org])
    await db.flush()
    foreign_user = User(
        keycloak_id=uuid.uuid4(),
        email="foreign-analytics@example.com",
        full_name="Foreign Analytics",
        org_id=foreign_org.id,
        role="member",
        status="active",
    )
    db.add(foreign_user)
    await db.flush()
    db.add_all(
        [
            _event(
                test_org.id,
                test_user.id,
                model=MODEL_A,
                status=UsageEventStatus.ACCEPTED,
                accepted_at=datetime(2026, 9, 1, 10, tzinfo=timezone.utc),
            ),
            _event(
                test_org.id,
                test_user.id,
                model=MODEL_A,
                status=UsageEventStatus.COMPLETED,
                accepted_at=datetime(2026, 8, 31, 23, tzinfo=timezone.utc),
                completed_at=datetime(2026, 9, 1, 23, tzinfo=timezone.utc),
                prompt_tokens=5,
                completion_tokens=3,
                total_tokens=8,
            ),
            _event(
                test_org.id,
                second_user.id,
                model=MODEL_B,
                status=UsageEventStatus.COMPLETED,
                accepted_at=datetime(2026, 9, 1, 11, tzinfo=timezone.utc),
                completed_at=datetime(2026, 9, 3, 1, tzinfo=timezone.utc),
                prompt_tokens=7,
                completion_tokens=5,
                total_tokens=12,
            ),
            _event(
                test_org.id,
                test_user.id,
                model=MODEL_A,
                status=UsageEventStatus.RESERVED,
            ),
            _event(
                foreign_org.id,
                foreign_user.id,
                model="foreign/model",
                status=UsageEventStatus.COMPLETED,
                accepted_at=datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
                completed_at=datetime(2026, 9, 1, 9, tzinfo=timezone.utc),
                prompt_tokens=500,
                completion_tokens=500,
                total_tokens=1000,
            ),
        ]
    )
    await db.commit()

    report = await UsageAnalyticsService().report(
        db,
        test_org.id,
        date(2026, 9, 1),
        date(2026, 9, 3),
    )

    assert report.totals == UsageTotals(
        requests=3,
        prompt_tokens=12,
        completion_tokens=8,
        total_tokens=20,
        unaccounted_requests=1,
    )
    assert [(bucket.date, bucket.totals) for bucket in report.daily] == [
        (
            date(2026, 9, 1),
            UsageTotals(2, 5, 3, 8, 1),
        ),
        (date(2026, 9, 2), UsageTotals.zero()),
        (
            date(2026, 9, 3),
            UsageTotals(1, 7, 5, 12, 0),
        ),
    ]
    assert [
        (row.email, row.totals.requests, row.totals.total_tokens)
        for row in report.by_user
    ] == [
        ("second@example.com", 1, 12),
        ("first@example.com", 2, 8),
    ]
    assert [
        (row.model, row.totals.requests, row.totals.total_tokens)
        for row in report.by_model
    ] == [
        (MODEL_B, 1, 12),
        (MODEL_A, 2, 8),
    ]
    assert report.request_limit == 10_000
    assert report.token_limit == 1_000_000_000


@pytest.mark.asyncio
async def test_report_applies_user_and_model_filters_before_aggregation(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    other_user = User(
        keycloak_id=uuid.uuid4(),
        email="filtered-other@example.com",
        org_id=test_org.id,
        role="member",
        status="active",
    )
    db.add(other_user)
    await db.flush()
    timestamp = datetime(2026, 9, 2, 10, tzinfo=timezone.utc)
    db.add_all(
        [
            _event(
                test_org.id,
                test_user.id,
                model=MODEL_A,
                status=UsageEventStatus.COMPLETED,
                accepted_at=timestamp,
                completed_at=timestamp,
                prompt_tokens=2,
                completion_tokens=3,
                total_tokens=5,
            ),
            _event(
                test_org.id,
                other_user.id,
                model=MODEL_A,
                status=UsageEventStatus.COMPLETED,
                accepted_at=timestamp,
                completed_at=timestamp,
                prompt_tokens=10,
                completion_tokens=10,
                total_tokens=20,
            ),
            _event(
                test_org.id,
                test_user.id,
                model=MODEL_B,
                status=UsageEventStatus.ACCEPTED,
                accepted_at=timestamp,
            ),
        ]
    )
    await db.commit()
    service = UsageAnalyticsService()

    user_report = await service.report(
        db,
        test_org.id,
        date(2026, 9, 1),
        date(2026, 9, 3),
        user_id=test_user.id,
    )
    model_report = await service.report(
        db,
        test_org.id,
        date(2026, 9, 1),
        date(2026, 9, 3),
        model=MODEL_A,
    )

    assert user_report.totals == UsageTotals(2, 2, 3, 5, 1)
    assert {row.email for row in user_report.by_user} == {test_user.email}
    assert model_report.totals == UsageTotals(2, 12, 13, 25, 0)
    assert {row.model for row in model_report.by_model} == {MODEL_A}


@pytest.mark.asyncio
async def test_empty_report_returns_zero_totals_and_every_requested_day(
    db: AsyncSession,
    test_org: Organization,
) -> None:
    report = await UsageAnalyticsService().report(
        db,
        test_org.id,
        date(2026, 9, 10),
        date(2026, 9, 12),
    )

    assert report.totals == UsageTotals.zero()
    assert [(row.date, row.totals) for row in report.daily] == [
        (date(2026, 9, 10), UsageTotals.zero()),
        (date(2026, 9, 11), UsageTotals.zero()),
        (date(2026, 9, 12), UsageTotals.zero()),
    ]
    assert report.by_user == ()
    assert report.by_model == ()


@pytest.mark.asyncio
async def test_report_preserves_unlimited_null_quotas(
    db: AsyncSession,
    test_org: Organization,
) -> None:
    test_org.quota_requests = None
    test_org.quota_tokens = None
    await db.commit()

    report = await UsageAnalyticsService().report(
        db,
        test_org.id,
        date(2026, 9, 1),
        date(2026, 9, 1),
    )

    assert report.request_limit is None
    assert report.token_limit is None


@pytest.mark.asyncio
async def test_quota_consumption_is_the_current_month_not_the_report_period(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    """The quota is monthly; the report's period is arbitrary.

    Subtracting a three-day period's totals from a monthly limit used to be
    presented as "remaining", which read as an almost untouched quota in the
    middle of a month that was nearly spent.
    """
    now = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
    db.add(
        OrganizationUsageMonthly(
            org_id=test_org.id,
            period_start=date(2026, 9, 1),
            requests_used=900,
            prompt_tokens=400,
            completion_tokens=100,
            total_tokens=500,
        )
    )
    db.add(
        _event(
            test_org.id,
            test_user.id,
            model=MODEL_A,
            status=UsageEventStatus.COMPLETED,
            completed_at=datetime(2026, 9, 16, 10, tzinfo=timezone.utc),
            prompt_tokens=3,
            completion_tokens=4,
            total_tokens=7,
        )
    )
    await db.commit()

    report = await UsageAnalyticsService().report(
        db,
        test_org.id,
        date(2026, 9, 16),
        date(2026, 9, 16),
        now=now,
    )

    assert report.totals.requests == 1
    assert report.totals.total_tokens == 7
    assert report.quota.period_start == date(2026, 9, 1)
    # Straight off the aggregate the quota gate itself reads, so it counts
    # reservations in flight and is unmoved by the one-day report period.
    assert report.quota.requests_used == 900
    assert report.quota.tokens_used == 500


@pytest.mark.asyncio
async def test_quota_consumption_is_zero_before_the_month_has_an_aggregate(
    db: AsyncSession,
    test_org: Organization,
) -> None:
    report = await UsageAnalyticsService().report(
        db,
        test_org.id,
        date(2026, 9, 1),
        date(2026, 9, 3),
        now=datetime(2026, 10, 4, 0, 30, tzinfo=timezone.utc),
    )

    assert report.quota.period_start == date(2026, 10, 1)
    assert report.quota.requests_used == 0
    assert report.quota.tokens_used == 0


@pytest.mark.asyncio
async def test_by_user_is_capped_and_reports_how_many_users_had_usage(
    monkeypatch: pytest.MonkeyPatch,
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    """One row per spending user is unbounded in a large organization.

    The rows are ordered by tokens spent, so the cap keeps the head — the
    part anyone reads — and `by_user_total` says how much was left off.
    """
    monkeypatch.setattr("src.services.usage_analytics.BY_USER_LIMIT", 2)
    test_user.email = "small@example.com"
    test_user.full_name = "Small Spender"
    others = [
        User(
            keycloak_id=uuid.uuid4(),
            email=f"user-{index}@example.com",
            full_name=f"User {index}",
            org_id=test_org.id,
            role="member",
            status="active",
        )
        for index in range(3)
    ]
    db.add_all(others)
    await db.flush()
    completed_at = datetime(2026, 9, 2, 10, tzinfo=timezone.utc)
    # The smallest spender also has two events, so a count over the join
    # without DISTINCT would report users that do not exist.
    for _ in range(2):
        db.add(
            _event(
                test_org.id,
                test_user.id,
                model=MODEL_A,
                status=UsageEventStatus.COMPLETED,
                completed_at=completed_at,
                total_tokens=1,
            )
        )
    for index, user in enumerate(others):
        db.add(
            _event(
                test_org.id,
                user.id,
                model=MODEL_A,
                status=UsageEventStatus.COMPLETED,
                completed_at=completed_at,
                total_tokens=100 * (index + 1),
            )
        )
    await db.commit()

    report = await UsageAnalyticsService().report(
        db, test_org.id, date(2026, 9, 1), date(2026, 9, 3)
    )

    assert report.by_user_total == 4
    assert [row.email for row in report.by_user] == [
        "user-2@example.com",
        "user-1@example.com",
    ]
    # Totals are unaffected by the cap — they are aggregated in SQL.
    assert report.totals.requests == 5


@pytest.mark.asyncio
async def test_by_user_total_follows_the_user_filter(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    other = User(
        keycloak_id=uuid.uuid4(),
        email="other@example.com",
        full_name="Other",
        org_id=test_org.id,
        role="member",
        status="active",
    )
    db.add(other)
    await db.flush()
    for user_id in (test_user.id, other.id):
        db.add(
            _event(
                test_org.id,
                user_id,
                model=MODEL_A,
                status=UsageEventStatus.COMPLETED,
                completed_at=datetime(2026, 9, 2, 10, tzinfo=timezone.utc),
                total_tokens=5,
            )
        )
    await db.commit()

    report = await UsageAnalyticsService().report(
        db,
        test_org.id,
        date(2026, 9, 1),
        date(2026, 9, 3),
        user_id=other.id,
    )

    assert report.by_user_total == 1
    assert [row.email for row in report.by_user] == ["other@example.com"]


@pytest.mark.asyncio
async def test_image_calls_count_as_requests_and_as_images(
    db: AsyncSession,
    test_org: Organization,
    test_user: User,
) -> None:
    at = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    db.add_all(
        [
            _event(test_org.id, test_user.id, model=MODEL_A, status=UsageEventStatus.COMPLETED,
                   accepted_at=at, completed_at=at, total_tokens=10),
            _event(test_org.id, test_user.id, model="image-model", kind="image_generate",
                   status=UsageEventStatus.COMPLETED, accepted_at=at, completed_at=at,
                   prompt_tokens=5, completion_tokens=95, total_tokens=100),
            # A provider that reports no usage still produced an image.
            _event(test_org.id, test_user.id, model="image-model", kind="image_edit",
                   status=UsageEventStatus.ACCEPTED, accepted_at=at),
            # A reservation that never reached the provider is no image.
            _event(test_org.id, test_user.id, model="image-model", kind="image_edit",
                   status=UsageEventStatus.RESERVED),
        ]
    )
    await db.flush()

    report = await UsageAnalyticsService().report(
        db, test_org.id, date(2026, 9, 10), date(2026, 9, 10)
    )

    assert report.totals == UsageTotals(3, 5, 95, 110, 1, 2)
    by_model = {row.model: row.totals for row in report.by_model}
    assert (by_model["image-model"].requests, by_model["image-model"].images) == (2, 2)
    assert by_model[MODEL_A].images == 0
    assert report.by_user[0].totals.images == 2
    assert report.daily[0].totals.images == 2
