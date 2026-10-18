"""Installed SDK transport must never hide an accepted-but-uncertain attempt."""

import importlib.util


import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import grpc
import pytest
from pymilvus.client.grpc_handler import GrpcHandler
from pymilvus.grpc_gen import milvus_pb2

from src.mutation_transport import mutate, RPC_TIMEOUT


def test_single_attempt_adapter_exists():
    assert importlib.util.find_spec("src.mutation_transport") is not None


class AcceptedLost(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE

    def details(self):
        return "fixture_accepted_reply_lost"


@pytest.mark.parametrize("operation", ["delete", "upsert"])
@pytest.mark.parametrize("failure", [AcceptedLost, asyncio.CancelledError])
def test_installed_raw_transport_submits_once_with_deadline(operation, failure):
    attempts = []
    handler = GrpcHandler.__new__(GrpcHandler)
    handler._grpc_options = {"grpc.enable_retries": 0}
    # Read-only schema preparation is excluded; the installed raw Upsert method
    # and its real transport boundary remain in use.
    handler._prepare_row_upsert_request = Mock(return_value=milvus_pb2.UpsertRequest())

    def send(request, timeout=None, **kwargs):
        attempts.append(timeout)
        if len(attempts) == 1:
            raise failure()
        return milvus_pb2.MutationResult(upsert_cnt=1, delete_cnt=1)

    handler._stub = SimpleNamespace(Upsert=send, Delete=SimpleNamespace(future=send))
    client = SimpleNamespace(_get_connection=lambda: handler, _generate_call_context=lambda: None)
    with pytest.raises(failure):
        mutate(
            client,
            operation,
            "fixture",
            'pk == "fixture"' if operation == "delete" else [{"pk": "fixture"}],
        )
    assert attempts == [RPC_TIMEOUT]


def test_future_sdk_or_retry_enabled_connection_fails_before_transport(monkeypatch):
    from src import mutation_transport

    monkeypatch.setattr(mutation_transport, "version", lambda _: "2.7.0")
    with pytest.raises(RuntimeError, match="contract_unsupported"):
        mutate(object(), "delete", "fixture", "true")


@pytest.mark.parametrize("operation", ["delete_object", "put_object"])
def test_installed_botocore_transport_does_not_retry_accepted_timeout(operation):
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ReadTimeoutError

    client = boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:1",
        aws_access_key_id="fixture",
        aws_secret_access_key="fixture",
        region_name="us-east-1",
        config=Config(retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=15),
    )
    attempts = []

    def accepted(request):
        attempts.append(request.method)
        if len(attempts) == 1:
            raise ReadTimeoutError(endpoint_url="fixture")
        raise AssertionError("hidden_second_submission")

    client._endpoint.http_session.send = accepted
    with pytest.raises(ReadTimeoutError):
        getattr(client, operation)(
            Bucket="fixture",
            Key="fixture",
            **({"Body": b"fixture"} if operation == "put_object" else {}),
        )
    assert len(attempts) == 1


@pytest.mark.parametrize("operation", ["delete", "upsert"])
@pytest.mark.parametrize("outcome", ["lost", "deadline"])
def test_real_grpc_server_accepts_only_one_bounded_mutation(operation, outcome, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from pymilvus.grpc_gen import milvus_pb2_grpc
    from src import mutation_transport

    accepted = []
    drained = threading.Event()

    def receive(request, context):
        accepted.append(context.time_remaining())
        if outcome == "deadline":
            drained.wait(2)
        if len(accepted) == 1:
            context.abort(grpc.StatusCode.UNAVAILABLE, "fixture_outcome_unknown")
        return milvus_pb2.MutationResult(upsert_cnt=1, delete_cnt=1)

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                "milvus.proto.milvus.MilvusService",
                {
                    "Delete": grpc.unary_unary_rpc_method_handler(
                        receive,
                        request_deserializer=milvus_pb2.DeleteRequest.FromString,
                        response_serializer=milvus_pb2.MutationResult.SerializeToString,
                    ),
                    "Upsert": grpc.unary_unary_rpc_method_handler(
                        receive,
                        request_deserializer=milvus_pb2.UpsertRequest.FromString,
                        response_serializer=milvus_pb2.MutationResult.SerializeToString,
                    ),
                },
            ),
        )
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}", options=[("grpc.enable_retries", 0)])
    handler = GrpcHandler.__new__(GrpcHandler)
    handler._grpc_options = {"grpc.enable_retries": 0}
    handler._stub = milvus_pb2_grpc.MilvusServiceStub(channel)
    handler._prepare_row_upsert_request = Mock(return_value=milvus_pb2.UpsertRequest())
    client = SimpleNamespace(_get_connection=lambda: handler, _generate_call_context=lambda: None)
    monkeypatch.setattr(mutation_transport, "RPC_TIMEOUT", 0.15 if outcome == "deadline" else 2)
    try:
        with pytest.raises(grpc.RpcError):
            mutate(
                client,
                operation,
                "fixture",
                'pk == "fixture"' if operation == "delete" else [{"pk": "fixture"}],
            )
        assert len(accepted) == 1
        assert 0 < accepted[0] <= mutation_transport.RPC_TIMEOUT + 0.1
    finally:
        drained.set()
        channel.close()
        server.stop(0).wait(2)


def test_installed_retry_decorator_numeric_timeout_overrides_zero_retries():
    from pymilvus.decorators import retry_on_rpc_failure

    calls = []

    @retry_on_rpc_failure()
    def predecessor_transport(self, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise AcceptedLost()
        return "later_attempt_succeeded"

    assert (
        predecessor_transport(SimpleNamespace(), retry_times=0, timeout=1.0)
        == "later_attempt_succeeded"
    )
    assert len(calls) == 2
