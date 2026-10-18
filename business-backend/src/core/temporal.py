from temporalio.client import Client

from src.core.config import settings

_client: Client | None = None


async def connect_temporal() -> None:
    global _client
    _client = await Client.connect(
        settings.TEMPORAL_HOST, namespace=settings.TEMPORAL_NAMESPACE
    )


async def disconnect_temporal() -> None:
    # temporalio's Client has no close()/shutdown() method — its gRPC
    # connection needs no explicit teardown, so dropping the reference is
    # the whole cleanup (a real AttributeError on every shutdown before
    # this fix, since Client never had a close() method to call).
    global _client
    _client = None


def get_temporal_client() -> Client:
    if _client is None:
        raise RuntimeError("Temporal client not initialised")
    return _client
