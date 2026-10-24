"""Durable web-operation leases, normalized results and immutable provenance."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.interaction import InteractionTurn, ToolOperation
from src.models.web_source import WebSource
from src.repositories.tool_operation_leases import (
    MAX_PLATFORM_ATTEMPTS,
    OperationClaim,
    ToolOperationLeaseError,
    ToolOperationLeases,
)

__all__ = [
    "MAX_PLATFORM_ATTEMPTS",
    "OperationClaim",
    "WEB_OPERATION_DEADLINE_SECONDS",
    "ListedWebSource",
    "WebSourceRepository",
    "WebSourceRepositoryError",
]

WEB_OPERATION_DEADLINE_SECONDS = 30.0


class WebSourceRepositoryError(ToolOperationLeaseError):
    pass


@dataclass(frozen=True, slots=True)
class ListedWebSource:
    source: WebSource
    content: str | None


class WebSourceRepository(ToolOperationLeases):
    deadline_seconds = WEB_OPERATION_DEADLINE_SECONDS
    timeout_code = "web_timeout"
    unavailable_code = "web_unavailable"
    error = WebSourceRepositoryError

    async def complete_operation(
        self,
        session: AsyncSession,
        operation_id: UUID,
        lease_id: UUID,
        raw_result: dict[str, Any],
    ) -> dict[str, Any]:
        turn, operation = await self._locked_operation(session, operation_id)
        if turn.status != "active":
            raise WebSourceRepositoryError("interaction_conflict")
        if operation.status == "completed" and operation.result is not None:
            return operation.result
        if operation.status != "running" or operation.lease_id != lease_id:
            raise WebSourceRepositoryError("operation_in_progress")
        retrieved_at = await self._clock(session)
        if self._remaining_seconds(operation, retrieved_at) <= 0:
            self._mark_failed(operation, "web_timeout")
            await session.flush()
            raise WebSourceRepositoryError("web_timeout")
        if operation.kind == "web_search":
            result = self._save_search_sources(session, operation, raw_result, retrieved_at)
        elif operation.kind == "web_read":
            result = self._save_read_source(session, operation, raw_result, retrieved_at)
        else:
            raise WebSourceRepositoryError("interaction_conflict")
        operation.status = "completed"
        operation.result = result
        operation.error_code = None
        operation.lease_id = None
        operation.lease_until = None
        await session.flush()
        return result

    def _save_search_sources(
        self,
        session: AsyncSession,
        operation: ToolOperation,
        raw_result: dict[str, Any],
        retrieved_at: datetime,
    ) -> dict[str, Any]:
        if set(raw_result) != {"results"}:
            raise WebSourceRepositoryError("web_unavailable")
        raw_hits = raw_result.get("results")
        if not isinstance(raw_hits, list) or len(raw_hits) > 5:
            raise WebSourceRepositoryError("web_unavailable")
        hits: list[dict[str, Any]] = []
        for position, raw in enumerate(raw_hits):
            if not isinstance(raw, dict) or set(raw) != {
                "title",
                "url",
                "snippet",
                "published_at",
            }:
                raise WebSourceRepositoryError("web_unavailable")
            title = self._bounded_string(raw.get("title"), 2000, required=True)
            url = self._bounded_string(raw.get("url"), 8192, required=True)
            snippet = self._bounded_string(
                raw.get("snippet"), 4000, required=True, allow_empty=True
            )
            published_at = self._datetime(raw.get("published_at"))
            source_id = uuid4()
            session.add(
                WebSource(
                    id=source_id,
                    org_id=self.org_id,
                    turn_id=operation.turn_id,
                    execution_id=operation.execution_id,
                    operation_id=operation.id,
                    position=position,
                    kind="search",
                    final_url=url,
                    title=title,
                    content=snippet,
                    published_at=published_at,
                    retrieved_at=retrieved_at,
                )
            )
            hits.append(
                {
                    "sourceId": str(source_id),
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                    "publishedAt": published_at.isoformat() if published_at else None,
                    "retrievedAt": retrieved_at.isoformat(),
                }
            )
        return {"results": hits}

    def _save_read_source(
        self,
        session: AsyncSession,
        operation: ToolOperation,
        raw: dict[str, Any],
        retrieved_at: datetime,
    ) -> dict[str, Any]:
        if set(raw) != {
            "requested_url",
            "final_url",
            "content_type",
            "text",
            "title",
            "truncated",
            "page_count",
        }:
            raise WebSourceRepositoryError("web_unavailable")
        requested_url = self._bounded_string(raw.get("requested_url"), 8192, required=True)
        final_url = self._bounded_string(raw.get("final_url"), 8192, required=True)
        content_type = self._bounded_string(raw.get("content_type"), 255, required=True)
        text = self._bounded_string(raw.get("text"), 50000, required=True)
        title = self._bounded_string(raw.get("title"), 2000, required=False)
        truncated = raw.get("truncated")
        page_count = raw.get("page_count")
        if not isinstance(truncated, bool) or (
            page_count is not None
            and (
                not isinstance(page_count, int)
                or isinstance(page_count, bool)
                or page_count < 0
                or page_count > 1_000_000
            )
        ):
            raise WebSourceRepositoryError("web_unavailable")
        source_id = uuid4()
        session.add(
            WebSource(
                id=source_id,
                org_id=self.org_id,
                turn_id=operation.turn_id,
                execution_id=operation.execution_id,
                operation_id=operation.id,
                position=0,
                kind="read",
                requested_url=requested_url,
                final_url=final_url,
                title=title,
                content_type=content_type,
                content=text,
                retrieved_at=retrieved_at,
                truncated=truncated,
                page_count=page_count,
            )
        )
        return {
            "sourceId": str(source_id),
            "requestedUrl": requested_url,
            "finalUrl": final_url,
            "contentType": content_type,
            "text": text,
            "title": title,
            "retrievedAt": retrieved_at.isoformat(),
            "truncated": truncated,
            "pageCount": page_count,
        }

    @staticmethod
    def _bounded_string(
        value: object,
        maximum: int,
        *,
        required: bool,
        allow_empty: bool = False,
    ) -> str | None:
        if value is None and not required:
            return None
        if (
            not isinstance(value, str)
            or (required and not allow_empty and not value)
            or len(value) > maximum
        ):
            raise WebSourceRepositoryError("web_unavailable")
        return value

    @staticmethod
    def _datetime(value: object) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise WebSourceRepositoryError("web_unavailable") from exc
        else:
            raise WebSourceRepositoryError("web_unavailable")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    async def list_sources(
        self, session: AsyncSession, turn_id: UUID
    ) -> list[ListedWebSource]:
        from sqlalchemy import case, func, or_

        visible_content = case(
            (
                or_(
                    ToolOperation.result_expires_at.is_(None),
                    ToolOperation.result_expires_at > func.clock_timestamp(),
                ),
                WebSource.content,
            ),
            else_=None,
        ).label("visible_content")
        rows = (
            await session.execute(
                select(WebSource, visible_content)
                .join(
                    ToolOperation,
                    (ToolOperation.org_id == WebSource.org_id)
                    & (ToolOperation.id == WebSource.operation_id),
                )
                .where(WebSource.org_id == self.org_id, WebSource.turn_id == turn_id)
                .order_by(WebSource.created_at, WebSource.position, WebSource.id)
            )
        ).all()
        return [ListedWebSource(source=row, content=content) for row, content in rows]

    async def owned_source_ids(
        self,
        session: AsyncSession,
        conversation_id: UUID,
        owner_id: UUID,
        turn_id: UUID,
        source_ids: tuple[str, ...],
    ) -> set[str]:
        owned_turn = await session.scalar(
            select(InteractionTurn.id).where(
                InteractionTurn.org_id == self.org_id,
                InteractionTurn.id == turn_id,
                InteractionTurn.conversation_id == conversation_id,
                InteractionTurn.owner_id == owner_id,
            )
        )
        if owned_turn is None:
            raise WebSourceRepositoryError("interaction_not_found")
        if not source_ids:
            return set()
        parsed = tuple(UUID(source_id) for source_id in source_ids)
        rows = await session.scalars(
            select(WebSource.id).where(
                WebSource.org_id == self.org_id,
                WebSource.turn_id == turn_id,
                WebSource.id.in_(parsed),
            )
        )
        return {str(source_id) for source_id in rows}

    async def scrub_expired_results(self, session: AsyncSession, *, batch_size: int = 100) -> int:
        if not 1 <= batch_size <= 1000:
            raise ValueError("batch_size out of range")
        now = await self._clock(session)
        candidates = list(
            (
                await session.execute(
                    select(ToolOperation.turn_id, ToolOperation.id)
                    .where(
                        ToolOperation.org_id == self.org_id,
                        ToolOperation.result.is_not(None),
                        ToolOperation.result_expires_at.is_not(None),
                        ToolOperation.result_expires_at <= now,
                    )
                    .order_by(ToolOperation.turn_id, ToolOperation.id)
                    .limit(batch_size)
                )
            ).all()
        )
        scrubbed = 0
        for _turn_id, operation_id in candidates:
            _, operation = await self._locked_operation(session, operation_id)
            if (
                operation.result is None
                or operation.result_expires_at is None
                or operation.result_expires_at > now
            ):
                continue
            operation.result = None
            await session.execute(
                update(WebSource)
                .where(
                    WebSource.org_id == self.org_id,
                    WebSource.operation_id == operation.id,
                )
                .values(content=None)
            )
            scrubbed += 1
        await session.flush()
        return scrubbed
