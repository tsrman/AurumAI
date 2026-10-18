from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

from sqlalchemy import BigInteger, Date, cast, func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql import ColumnElement
from sqlalchemy.sql.selectable import CTE

from src.models.llm_usage import (
    LLMUsageEvent,
    OrganizationUsageMonthly,
    UsageEventStatus,
)
from src.models.organization import Organization
from src.models.user import User


@dataclass(frozen=True, slots=True)
class UsageTotals:
    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    unaccounted_requests: int

    @classmethod
    def zero(cls) -> "UsageTotals":
        return cls(0, 0, 0, 0, 0)


@dataclass(frozen=True, slots=True)
class DailyUsage:
    date: date
    totals: UsageTotals


@dataclass(frozen=True, slots=True)
class UserUsage:
    user_id: UUID
    email: str
    full_name: str | None
    totals: UsageTotals


@dataclass(frozen=True, slots=True)
class ModelUsage:
    model: str
    totals: UsageTotals


@dataclass(frozen=True, slots=True)
class QuotaConsumption:
    """Where the organization stands against its *monthly* quota.

    Deliberately independent of the report's period: the limits are per
    calendar month, so subtracting an arbitrary range's totals from them
    answers a question nobody asked. Read straight off the same
    `organization_usage_monthly` aggregate the quota gate itself uses, which
    also counts reservations still in flight — `requests_used` will
    legitimately sit above `totals.requests` (accepted + completed only)
    while requests are streaming.
    """

    period_start: date
    requests_used: int
    tokens_used: int


@dataclass(frozen=True, slots=True)
class UsageAnalyticsReport:
    request_limit: int | None
    token_limit: int | None
    quota: QuotaConsumption
    totals: UsageTotals
    daily: tuple[DailyUsage, ...]
    by_user: tuple[UserUsage, ...]
    by_user_total: int
    by_model: tuple[ModelUsage, ...]


# `by_user` used to be "however many members spent something this period",
# which is unbounded in a large organization — one row per user, in a single
# response and a single DOM table. The rows are ordered by tokens spent, so
# the head of that list is the part anyone reads; `by_user_total` reports how
# many there were so a truncated table can say so.
BY_USER_LIMIT = 50


def _sum(column: ColumnElement[int]) -> ColumnElement[int]:
    return func.coalesce(func.sum(column), 0)


class UsageAnalyticsService:
    @staticmethod
    def _normalized_events(
        org_id: UUID,
        date_from: date,
        date_to: date,
        *,
        user_id: UUID | None,
        model: str | None,
    ) -> CTE:
        start = datetime.combine(date_from, time.min, tzinfo=timezone.utc)
        end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc)

        def filters(
            event_at: InstrumentedAttribute[datetime | None],
        ) -> list[ColumnElement[bool]]:
            predicates = [
                LLMUsageEvent.org_id == org_id,
                event_at >= start,
                event_at < end,
            ]
            if user_id is not None:
                predicates.append(LLMUsageEvent.user_id == user_id)
            if model is not None:
                predicates.append(LLMUsageEvent.model == model)
            return predicates

        zero = literal(0, type_=BigInteger)
        accepted = select(
            LLMUsageEvent.user_id.label("user_id"),
            LLMUsageEvent.model.label("model"),
            LLMUsageEvent.accepted_at.label("event_at"),
            zero.label("prompt_tokens"),
            zero.label("completion_tokens"),
            zero.label("total_tokens"),
            literal(1, type_=BigInteger).label("unaccounted_requests"),
        ).where(
            LLMUsageEvent.status == UsageEventStatus.ACCEPTED.value,
            LLMUsageEvent.accepted_at.is_not(None),
            *filters(LLMUsageEvent.accepted_at),
        )
        completed = select(
            LLMUsageEvent.user_id.label("user_id"),
            LLMUsageEvent.model.label("model"),
            LLMUsageEvent.completed_at.label("event_at"),
            LLMUsageEvent.prompt_tokens.label("prompt_tokens"),
            LLMUsageEvent.completion_tokens.label("completion_tokens"),
            LLMUsageEvent.total_tokens.label("total_tokens"),
            zero.label("unaccounted_requests"),
        ).where(
            LLMUsageEvent.status == UsageEventStatus.COMPLETED.value,
            LLMUsageEvent.completed_at.is_not(None),
            *filters(LLMUsageEvent.completed_at),
        )
        return union_all(accepted, completed).cte("normalized_usage_events")

    @staticmethod
    def _aggregate_columns(events: CTE) -> tuple[ColumnElement[int], ...]:
        return (
            func.count().label("requests"),
            _sum(events.c.prompt_tokens).label("prompt_tokens"),
            _sum(events.c.completion_tokens).label("completion_tokens"),
            _sum(events.c.total_tokens).label("total_tokens"),
            _sum(events.c.unaccounted_requests).label("unaccounted_requests"),
        )

    @staticmethod
    def _totals(row: object) -> UsageTotals:
        values = row  # SQLAlchemy Row exposes the selected attributes at runtime.
        return UsageTotals(
            requests=int(getattr(values, "requests")),
            prompt_tokens=int(getattr(values, "prompt_tokens")),
            completion_tokens=int(getattr(values, "completion_tokens")),
            total_tokens=int(getattr(values, "total_tokens")),
            unaccounted_requests=int(getattr(values, "unaccounted_requests")),
        )

    @staticmethod
    def _quota_period_start(now: datetime | None) -> date:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return date(current.year, current.month, 1)

    async def report(
        self,
        session: AsyncSession,
        org_id: UUID,
        date_from: date,
        date_to: date,
        *,
        user_id: UUID | None = None,
        model: str | None = None,
        now: datetime | None = None,
    ) -> UsageAnalyticsReport:
        events = self._normalized_events(
            org_id,
            date_from,
            date_to,
            user_id=user_id,
            model=model,
        )
        aggregate_columns = self._aggregate_columns(events)
        totals_row = (await session.execute(select(*aggregate_columns))).one()

        utc_day = cast(func.timezone("UTC", events.c.event_at), Date).label("date")
        daily_rows = (
            await session.execute(
                select(utc_day, *aggregate_columns)
                .group_by(utc_day)
                .order_by(utc_day)
            )
        ).all()
        daily_by_date = {row.date: self._totals(row) for row in daily_rows}

        user_rows = (
            await session.execute(
                select(
                    User.id.label("user_id"),
                    User.email,
                    User.full_name,
                    *aggregate_columns,
                )
                .join(events, events.c.user_id == User.id)
                .where(User.org_id == org_id)
                .group_by(User.id, User.email, User.full_name)
                .order_by(
                    _sum(events.c.total_tokens).desc(),
                    func.count().desc(),
                    User.id.asc(),
                )
                .limit(BY_USER_LIMIT)
            )
        ).all()
        # Counted over the same join and predicate the rows come from, so the
        # total can never promise a user the report cannot produce. DISTINCT
        # is what makes this users rather than events.
        by_user_total = int(
            await session.scalar(
                select(func.count(func.distinct(User.id)))
                .select_from(User)
                .join(events, events.c.user_id == User.id)
                .where(User.org_id == org_id)
            )
            or 0
        )
        model_rows = (
            await session.execute(
                select(events.c.model, *aggregate_columns)
                .group_by(events.c.model)
                .order_by(
                    _sum(events.c.total_tokens).desc(),
                    func.count().desc(),
                    events.c.model.asc(),
                )
            )
        ).all()
        quota_row = (
            await session.execute(
                select(Organization.quota_requests, Organization.quota_tokens).where(
                    Organization.id == org_id
                )
            )
        ).one()
        # The quota is monthly, so its consumption is read for the current
        # month regardless of the report's period. The aggregate row only
        # appears on the period's first reservation — absent means zero.
        quota_period_start = self._quota_period_start(now)
        consumption_row = (
            await session.execute(
                select(
                    OrganizationUsageMonthly.requests_used,
                    OrganizationUsageMonthly.total_tokens,
                ).where(
                    OrganizationUsageMonthly.org_id == org_id,
                    OrganizationUsageMonthly.period_start == quota_period_start,
                )
            )
        ).one_or_none()

        day_count = (date_to - date_from).days + 1
        return UsageAnalyticsReport(
            request_limit=(
                int(quota_row.quota_requests)
                if quota_row.quota_requests is not None
                else None
            ),
            token_limit=(
                int(quota_row.quota_tokens)
                if quota_row.quota_tokens is not None
                else None
            ),
            quota=QuotaConsumption(
                period_start=quota_period_start,
                requests_used=(
                    int(consumption_row.requests_used) if consumption_row else 0
                ),
                tokens_used=(
                    int(consumption_row.total_tokens) if consumption_row else 0
                ),
            ),
            totals=self._totals(totals_row),
            daily=tuple(
                DailyUsage(
                    date=current,
                    totals=daily_by_date.get(current, UsageTotals.zero()),
                )
                for current in (
                    date_from + timedelta(days=offset) for offset in range(day_count)
                )
            ),
            by_user=tuple(
                UserUsage(
                    user_id=row.user_id,
                    email=row.email,
                    full_name=row.full_name,
                    totals=self._totals(row),
                )
                for row in user_rows
            ),
            by_user_total=by_user_total,
            by_model=tuple(
                ModelUsage(model=row.model, totals=self._totals(row))
                for row in model_rows
            ),
        )


usage_analytics_service = UsageAnalyticsService()
