import uuid

from fastapi import Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from src.core.config import settings
from src.core.logging import logger
from src.middleware.rate_limit import _rate_limit_identity

# Request-scoped dependencies may retain their connections until the response
# body is sent. Awaited audit writes must not wait for that same pool to drain.
audit_engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=1,
    max_overflow=0,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=settings.DEBUG,
    hide_parameters=settings.IDENTITY_SYNC_ENABLED,
)
async_session = async_sessionmaker(audit_engine, expire_on_commit=False)

# Reads are not logged here: this journal tracks who changed what.
# Frequent GET requests would drown out useful mutation records.
_AUDITED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Login/refresh precede an organization context; Keycloak already records
# their relevant identity events. hermes-egress checks every sandbox
# connection, and the preview service every browser connection, and each
# keeps its own journal of them.
_SKIPPED_PREFIXES = ("/v1/auth/", "/v1/internal/hermes/egress/", "/v1/internal/hermes/previews/check")


def _route_template(request: Request) -> str:
    """Return a route template such as /v1/skills/{skill_id}, not an object URL.

    Group records by operation without leaking resource IDs through the path.
    Replace actual path parameter values with their names. FastAPI0.141 keeps
    included routers nested, so route.path can omit the /v1 prefix; derive
    the template from the actual request path instead.
    """
    path = request.url.path
    for name, value in (request.scope.get("path_params") or {}).items():
        if isinstance(value, (str, int, float)):
            path = path.replace(str(value), "{" + name + "}", 1)
    return path[:255]


class AuditMiddleware(BaseHTTPMiddleware):
    """Write an audit_logs row for each mutating request.

    Wait for the audit write before returning the response, so acknowledged
    mutations do not leave their audit records in detached process tasks.
    A storage failure remains visible in application logs without changing the
    result of a business mutation that may already have committed.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)

        if request.method not in _AUDITED_METHODS:
            return response
        if not request.url.path.startswith("/v1"):
            return response
        if request.url.path.startswith(_SKIPPED_PREFIXES):
            return response

        await self._record_logged(request, response.status_code)
        return response

    async def _record_logged(self, request: Request, status_code: int) -> None:
        try:
            await self._record(request, status_code)
        except Exception:
            logger.warning("audit_log_write_failed", exc_info=True)

    async def _record(self, request: Request, status_code: int) -> None:
        org_id, user_id = await _rate_limit_identity(request)

        # Unauthenticated attempts before token parsing have no tenant context
        # for this RLS-scoped table; HTTP logs already record those failures.
        try:
            org_uuid = uuid.UUID(org_id)
        except (ValueError, AttributeError):
            return

        try:
            user_uuid: uuid.UUID | None = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            user_uuid = None

        client_host = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")

        async with async_session() as session:
            # As in get_org_db_session, audit_logs RLS permits INSERT only with
            # app.current_org_id set for this transaction.
            await session.execute(
                text("SELECT set_config('app.current_org_id', :org_id, true)"),
                {"org_id": str(org_uuid)},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO audit_logs
                        (id, org_id, user_id, method, path, status_code, ip_address, user_agent)
                    VALUES
                        (:id, :org_id, :user_id, :method, :path, :status_code, :ip, :ua)
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "org_id": org_uuid,
                    "user_id": user_uuid,
                    "method": request.method,
                    "path": _route_template(request),
                    "status_code": status_code,
                    "ip": client_host,
                    "ua": (user_agent or "")[:255] or None,
                },
            )
            await session.commit()
