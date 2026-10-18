from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Session
from sqlalchemy.exc import DBAPIError
from src.core.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=20,
    max_overflow=40,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=settings.DEBUG,
    hide_parameters=settings.IDENTITY_SYNC_ENABLED,
)


class _AppSession(Session):
    """Dedicated sync-Session subclass (AsyncSession dispatches ORM events
    through its wrapped sync Session, not itself) so the after_begin hook
    below only applies to sessions from this app's own sessionmaker.

    Fixes a real, previously-deferred bug ("Could not refresh instance"):
    app.current_org_id is the RLS org-context GUC, but a pooled
    AsyncSession's *physical* connection can change out from under it at
    any commit — Session.commit() releases the connection back to the
    pool, and the session's next statement (e.g. a post-commit
    session.refresh(), which expire_on_commit=False makes a real query,
    not a no-op) can be handed a *different* connection, one that still
    carries whatever org_id (or none) some other request last set on it.
    A handful of call sites (dependencies.py, auth.py)
    each tried to re-set the GUC by hand after their own commits, but
    sync_keycloak_user's *internal* commit+refresh (auth_service.py) had
    no such guard — the crash always happened there, one connection-swap
    away from whichever site had most recently re-armed it.

    SQLAlchemy fires after_begin exactly once per transaction actually
    beginning on a session — including every autobegin after a prior
    commit — with the Connection actually in use for that transaction, so
    re-issuing SET LOCAL there is correct regardless of how many times the
    physical connection changes underneath a single logical request.
    is_local=true (SET LOCAL, not session-wide set_config) also means this
    can never leak into a future request that reuses the same pooled
    connection, even if cleanup is skipped (an exception, a dropped
    request) — closing the *other* half of this bug class for free.
    """


@event.listens_for(_AppSession, "after_begin")
def _set_rls_org_context(session: Session, transaction: object, connection: object) -> None:
    org_id = session.info.get("org_id")
    if org_id:
        connection.execute(  # type: ignore[attr-defined]
            text("SELECT set_config('app.current_org_id', :org_id, true)"),
            {"org_id": org_id},
        )


    fence = session.info.get("conversation_fence")
    if fence:
        from src.services.conversation_lifecycle import ConversationFrozen
        org, conversation, generation = fence
        try:
            connection.execute(  # type: ignore[attr-defined]
                text("SELECT platform_lock_live_conversation(:org,:conversation)"),
                {"org":org, "conversation":conversation},
            )
            current = connection.execute(  # type: ignore[attr-defined]
                text("SELECT platform_require_active_registry(:org,:conversation)"),
                {"org":org, "conversation":conversation},
            ).scalar_one()
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) in {"55000", "23503"}:
                raise ConversationFrozen() from exc
            raise
        if generation is not None and current != generation:
            raise ConversationFrozen()


async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    sync_session_class=_AppSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
        await conn.run_sync(Base.metadata.create_all)
