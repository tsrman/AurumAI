"""One submitted Milvus mutation, with a bounded underlying RPC.

Audited against pymilvus 2.6.17: numeric timeout overrides retry_times in
retry_on_rpc_failure. Upsert also has retry_on_schema_mismatch. Bypass both
wrappers, retain raw request/result conversion and disable gRPC core retries.
Unknown/cancelled outcome propagates to the caller's durable pending intent.
"""

import inspect
from importlib.metadata import version
from typing import Any

from pymilvus.client.grpc_handler import GrpcHandler

RPC_TIMEOUT = 15.0
GRPC_OPTIONS = {"grpc.enable_retries": 0}


def mutate(client: Any, operation: str, collection: str, value: Any) -> dict[str, Any]:
    if version("pymilvus") != "2.6.17" or operation not in ("delete", "upsert"):
        raise RuntimeError("mutation_sdk_contract_unsupported")
    connection = client._get_connection()
    if (
        not isinstance(connection, GrpcHandler)
        or connection._grpc_options.get("grpc.enable_retries") != 0
    ):
        raise RuntimeError("mutation_transport_retries_enabled")
    name = "delete" if operation == "delete" else "upsert_rows"
    wrapped = getattr(GrpcHandler, name)
    raw = inspect.unwrap(wrapped)
    required = {"self", "collection_name", "timeout", "context"}
    if raw is wrapped or not required.issubset(inspect.signature(raw).parameters):
        raise RuntimeError("mutation_sdk_contract_unsupported")
    # Read-only schema preparation may retry; exactly one raw mutation follows.
    result = raw(
        connection, collection, value, timeout=RPC_TIMEOUT, context=client._generate_call_context()
    )
    if operation == "delete":
        return {"delete_count": int(result.delete_count)}
    return {"upsert_count": int(result.upsert_count), "ids": list(result.primary_keys)}
