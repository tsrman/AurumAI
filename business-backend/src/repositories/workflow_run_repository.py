from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.workflow_run import WorkflowNodeRun, WorkflowRun, WorkflowRunStatus


@dataclass(frozen=True, slots=True)
class WorkflowTransitionRejected(Exception):
    run_id: UUID
    target: WorkflowRunStatus


@dataclass(frozen=True, slots=True)
class WorkflowOutputLimitExceeded(Exception):
    code: str


@dataclass(frozen=True, slots=True)
class WorkflowNodeTransitionRejected(Exception):
    run_id: UUID
    phase_key: str
    target: str


class WorkflowRunRepository:
    _ALLOWED_FROM: dict[WorkflowRunStatus, tuple[WorkflowRunStatus, ...]] = {
        WorkflowRunStatus.PLANNING: (WorkflowRunStatus.PENDING,),
        WorkflowRunStatus.RUNNING: (WorkflowRunStatus.PLANNING,),
        WorkflowRunStatus.SYNTHESIZING: (WorkflowRunStatus.RUNNING,),
        WorkflowRunStatus.COMPLETED: (WorkflowRunStatus.SYNTHESIZING,),
        WorkflowRunStatus.FAILED: (
            WorkflowRunStatus.PENDING,
            WorkflowRunStatus.PLANNING,
            WorkflowRunStatus.RUNNING,
            WorkflowRunStatus.SYNTHESIZING,
        ),
        WorkflowRunStatus.CANCELLED: (
            WorkflowRunStatus.PENDING,
            WorkflowRunStatus.PLANNING,
            WorkflowRunStatus.RUNNING,
            WorkflowRunStatus.SYNTHESIZING,
        ),
    }
    _NODE_ALLOWED_FROM: dict[str, tuple[str, ...]] = {
        # A loop body reset (conversation-runner's MultiAgentWorkflow, on
        # taking a loop-back edge) is the only way a node returns to
        # "pending" after a terminal outcome — initial rows are created
        # pending directly, never via this method.
        "pending": ("completed", "failed", "skipped"),
        "running": ("pending", "waiting_human"),
        "waiting_human": ("pending",),
        "completed": ("pending", "running"),
        "failed": ("pending", "running", "waiting_human"),
        "skipped": ("pending",),
        "cancelled": ("pending", "running", "waiting_human"),
    }

    async def get_by_id(
        self,
        session: AsyncSession,
        run_id: UUID,
        *,
        org_id: UUID | None = None,
        user_id: UUID | None = None,
        for_update: bool = False,
    ) -> WorkflowRun | None:
        statement = select(WorkflowRun).where(WorkflowRun.id == run_id)
        if org_id is not None:
            statement = statement.where(WorkflowRun.org_id == org_id)
        if user_id is not None:
            statement = statement.where(WorkflowRun.initiating_user_id == user_id)
        if for_update:
            statement = statement.with_for_update()
        return (await session.execute(statement)).scalar_one_or_none()

    async def list_node_runs(
        self,
        session: AsyncSession,
        run_id: UUID,
    ) -> list[WorkflowNodeRun]:
        return list(
            (
                await session.execute(
                    select(WorkflowNodeRun)
                    .where(WorkflowNodeRun.workflow_run_id == run_id)
                    .order_by(WorkflowNodeRun.phase_key)
                )
            ).scalars()
        )

    async def transition(
        self,
        session: AsyncSession,
        run_id: UUID,
        status: WorkflowRunStatus,
        error_code: str | None,
    ) -> WorkflowRun:
        allowed = tuple(item.value for item in self._ALLOWED_FROM.get(status, ()))
        values: dict[str, object] = {
            "status": status.value,
            # Repeating the current status without a code (the orchestrator
            # confirming a cancellation the API already recorded) keeps the
            # recorded reason instead of erasing it.
            "error_code": error_code
            if error_code is not None
            else case(
                (WorkflowRun.status == status.value, WorkflowRun.error_code),
                else_=None,
            ),
            "updated_at": func.now(),
        }
        if status is WorkflowRunStatus.RUNNING:
            values["started_at"] = case(
                (WorkflowRun.started_at.is_(None), func.now()),
                else_=WorkflowRun.started_at,
            )
        if status in {
            WorkflowRunStatus.COMPLETED,
            WorkflowRunStatus.FAILED,
            WorkflowRunStatus.CANCELLED,
        }:
            values["completed_at"] = case(
                (WorkflowRun.completed_at.is_(None), func.now()),
                else_=WorkflowRun.completed_at,
            )
        transitioned = await session.scalar(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run_id,
                WorkflowRun.status.in_((*allowed, status.value)),
            )
            .values(**values)
            .returning(WorkflowRun)
        )
        if transitioned is None:
            raise WorkflowTransitionRejected(run_id, status)
        return transitioned

    async def record_node_result(
        self,
        session: AsyncSession,
        run_id: UUID,
        phase_key: str,
        status: str,
        output: str | None,
        error_code: str | None,
    ) -> WorkflowNodeRun:
        await self.get_by_id(session, run_id, for_update=True)
        node_run = await session.scalar(
            select(WorkflowNodeRun)
            .where(
                WorkflowNodeRun.workflow_run_id == run_id,
                WorkflowNodeRun.phase_key == phase_key,
            )
            .with_for_update()
        )
        if node_run is None:
            raise LookupError(phase_key)
        encoded_size = len(output.encode("utf-8")) if output is not None else 0
        if encoded_size > 65_536:
            raise WorkflowOutputLimitExceeded("node_output_limit")
        aggregate = await session.scalar(
            select(func.coalesce(func.sum(func.octet_length(WorkflowNodeRun.output)), 0)).where(
                WorkflowNodeRun.workflow_run_id == run_id,
                WorkflowNodeRun.phase_key != phase_key,
            )
        )
        if int(aggregate or 0) + encoded_size > 524_288:
            raise WorkflowOutputLimitExceeded("aggregate_output_limit")
        if node_run.status == status:
            return node_run
        if node_run.status not in self._NODE_ALLOWED_FROM.get(status, ()):
            raise WorkflowNodeTransitionRejected(run_id, phase_key, status)
        node_run.status = status
        node_run.output = output
        node_run.error_code = error_code
        if status == "running":
            node_run.attempt_count += 1
            node_run.started_at = node_run.started_at or datetime.now(UTC)
        if status in {"completed", "failed", "cancelled"}:
            node_run.completed_at = node_run.completed_at or datetime.now(UTC)
            node_run.diagnostic_expires_at = node_run.completed_at + timedelta(
                days=settings.WORKFLOW_DIAGNOSTIC_RETENTION_DAYS
            )
        if status == "pending":
            # Loop body reset: clear the prior iteration's timestamps so the
            # write-once guards above set fresh values for the next
            # iteration instead of preserving the first iteration's forever.
            # attempt_count is deliberately left alone — it accumulates
            # across iterations as the node's total execution count.
            node_run.started_at = None
            node_run.completed_at = None
            node_run.diagnostic_expires_at = None
        await session.flush()
        return node_run

    async def clear_expired_diagnostics(
        self,
        session: AsyncSession,
        now: datetime,
    ) -> int:
        # Deliberately a SECURITY DEFINER function call, not an ORM UPDATE.
        # The sweep is cross-tenant, so it has no single org context to put in
        # app.current_org_id, and workflow_node_runs is FORCE ROW LEVEL
        # SECURITY — a plain UPDATE under the aurum_ai_app runtime role
        # matches zero rows and silently retains expired diagnostics forever.
        # See 2026_09_03_workflow_diag_sweep for why the privilege lives
        # in the function rather than in the backend's database credentials.
        cleared = await session.scalar(
            select(func.clear_expired_workflow_diagnostics(now))
        )
        return int(cleared or 0)


workflow_run_repository = WorkflowRunRepository()
