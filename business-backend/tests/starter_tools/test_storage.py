"""Create-only upload and reconciliation at the external object-storage boundary."""

from contextlib import asynccontextmanager

from botocore.exceptions import ClientError
import pytest

from src.services import starter_tools


class ConditionalObjectStore:
    def __init__(self):
        self.objects = {}
        self.requests = []

    async def put_object(self, *, Bucket, Key, Body, IfNoneMatch):
        self.requests.append((Bucket, Key, IfNoneMatch))
        if Key in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[Key] = Body

    async def get_bytes(self, key):
        return self.objects[key]

    @asynccontextmanager
    async def client(self):
        yield self


async def test_bundle_upload_is_create_only_and_reconciles_same_content(monkeypatch):
    remote = ConditionalObjectStore()
    monkeypatch.setattr(starter_tools.object_storage, "_client", remote.client)
    monkeypatch.setattr(starter_tools.object_storage, "get_bytes", remote.get_bytes)
    storage = starter_tools.StarterBundleStorage()
    await storage.put_bundle("owned-content-key", b"bundle")
    await storage.put_bundle("owned-content-key", b"bundle")
    assert remote.objects == {"owned-content-key": b"bundle"}
    assert all(request[2] == "*" for request in remote.requests)
    with pytest.raises(ValueError, match="identity collision"):
        await storage.put_bundle("owned-content-key", b"different")
    assert remote.objects == {"owned-content-key": b"bundle"}
