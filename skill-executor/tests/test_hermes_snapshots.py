"""Hermes workspace snapshots: the archive sanitizer, the S3 store, and the
sandbox lifecycle around them (retire, keep on failure, resume, restore)."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.config import Settings
from src.hermes_runtime import DockerHermesSandboxes, HermesSandboxError, resource_name
from src.hermes_snapshots import Snapshot, SnapshotError, SnapshotStore, sanitize
from tests.test_hermes_runtime import SESSION, TOKEN, FakeDocker

ORG, CONVERSATION = "org-1", "conv-1"


def _tar(entries: list[tuple[str, Any]]) -> bytes:
    """entries: (name, bytes) for a file, (name, None) for a directory, or
    (name, TarInfo) for anything else."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, value in entries:
            if isinstance(value, tarfile.TarInfo):
                value.name = name
                archive.addfile(value)
            elif value is None:
                info = tarfile.TarInfo(name)
                info.type, info.mode = tarfile.DIRTYPE, 0o777
                archive.addfile(info)
            else:
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.uid = len(value), 0o4755, 0
                archive.addfile(info, io.BytesIO(value))
    return buffer.getvalue()


def _members(data: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        return {member.name: member for member in archive.getmembers()}


def _contents(data: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        return {m.name: archive.extractfile(m).read() for m in archive.getmembers() if m.isfile()}  # type: ignore[union-attr]


def _sanitize(tmp_path: Path, data: bytes, *, strip: str | None = "workspace", **limits: int) -> bytes:
    source, target = tmp_path / "in.tar", tmp_path / "out.tar"
    source.write_bytes(data)
    target.unlink(missing_ok=True)
    sanitize(source, target, strip=strip, max_bytes=limits.get("max_bytes", 10**6), max_entries=limits.get("max_entries", 100))
    return target.read_bytes()


def _link(kind: bytes, target: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo()
    info.type, info.linkname = kind, target
    return info


def test_secrets_relay_state_memory_and_skills_never_enter_a_snapshot(tmp_path: Path) -> None:
    data = _tar(
        [
            ("workspace", None),
            ("workspace/.hermes/config.yaml", TOKEN.encode()),
            ("workspace/.hermes/.env", b"KEY=1"),
            ("workspace/.hermes/auth.json", b"{}"),
            ("workspace/.hermes/logs/agent.log", TOKEN.encode()),
            ("workspace/.hermes/memories/MEMORY.md", b"m"),
            ("workspace/.hermes/skills/s/SKILL.md", b"s"),
            # Hermes 0.22 keeps copies of the last config that parsed.
            ("workspace/.hermes/backups/config/config.yaml.good.20261026-233500", TOKEN.encode()),
            ("workspace/.aurum/proxy", TOKEN.encode()),
            ("workspace/.aurum/requests/k.json", b"{}"),
            ("workspace/.hermes/state.db", b"db"),
            ("workspace/.hermes/memories.txt", b"kept"),
            ("workspace/home/notes.md", b"n"),
        ]
    )

    out = _sanitize(tmp_path, data)

    assert TOKEN.encode() not in out
    assert _contents(out) == {".hermes/state.db": b"db", ".hermes/memories.txt": b"kept", "home/notes.md": b"n"}


def test_only_files_and_directories_under_relative_paths_survive(tmp_path: Path) -> None:
    device = tarfile.TarInfo()
    device.type = tarfile.CHRTYPE
    data = _tar(
        [
            ("workspace/link", _link(tarfile.SYMTYPE, "/etc/passwd")),
            ("workspace/hard", _link(tarfile.LNKTYPE, "workspace/home/a")),
            ("workspace/dev", device),
            ("workspace/../escape", b"x"),
            ("/abs", b"x"),
            ("other/file", b"x"),
            ("workspace/home/a", b"a"),
        ]
    )

    members = _members(_sanitize(tmp_path, data))

    assert sorted(members) == ["home", "home/a"]
    assert members["home"].isdir() and members["home"].mode == 0o700
    assert (members["home/a"].uid, members["home/a"].gid, members["home/a"].mode) == (10001, 10001, 0o700)


def test_missing_parents_are_added_with_the_sandbox_owner(tmp_path: Path) -> None:
    members = _members(_sanitize(tmp_path, _tar([("deep/er/file", b"x")]), strip=None))

    assert [name for name in members] == ["deep", "deep/er", "deep/er/file"]
    assert all(m.uid == 10001 for m in members.values())
    assert members["deep/er/file"].mode == 0o600 or members["deep/er/file"].mode == 0o700


def test_size_and_entry_bounds_refuse_the_archive(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError, match="too_large"):
        _sanitize(tmp_path, _tar([("a", b"x" * 10), ("b", b"x" * 10)]), strip=None, max_bytes=15)
    assert not (tmp_path / "out.tar").exists()
    with pytest.raises(SnapshotError, match="too_large"):
        _sanitize(tmp_path, _tar([(f"f{i}", b"") for i in range(5)]), strip=None, max_entries=3)
    with pytest.raises(SnapshotError, match="invalid"):
        _sanitize(tmp_path, b"not a tar at all" * 64, strip=None)


def test_sanitizing_a_snapshot_again_keeps_it(tmp_path: Path) -> None:
    once = _sanitize(tmp_path, _tar([("workspace/home/a", b"a"), ("workspace/bin/run", b"#!")]))
    twice = _sanitize(tmp_path, once, strip=None)

    assert _contents(twice) == _contents(once)


# The S3 store ---------------------------------------------------------------


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = io.BytesIO(data)

    async def __aenter__(self) -> FakeBody:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def read(self, n: int = -1) -> bytes:
        return self._data.read(n)


class FakeS3:
    class exceptions:  # noqa: N801 - boto's shape
        class NoSuchKey(Exception):
            pass

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, datetime]] = {}
        self.fail = False

    async def __aenter__(self) -> FakeS3:
        if self.fail:
            raise ConnectionError("down")
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def put_object(self, Bucket: str, Key: str, Body: Any, **_: Any) -> None:  # noqa: N803
        data = Body if isinstance(Body, bytes) else Body.read()
        self.objects[Key] = (data, datetime.now(timezone.utc))

    async def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey()
        data = self.objects[Key][0]
        return {"Body": FakeBody(data), "ContentLength": len(data)}

    async def list_objects_v2(self, Bucket: str, Prefix: str) -> dict[str, Any]:  # noqa: N803
        return {
            "Contents": [
                {"Key": key, "LastModified": modified}
                for key, (_, modified) in self.objects.items()
                if key.startswith(Prefix)
            ]
        }

    async def delete_object(self, Bucket: str, Key: str) -> None:  # noqa: N803
        self.objects.pop(Key, None)

    def get_paginator(self, name: str) -> Any:
        outer = self

        class Paginator:
            async def _pages(self, Prefix: str) -> Any:  # noqa: N803
                yield await outer.list_objects_v2("", Prefix)

            def paginate(self, Bucket: str, Prefix: str) -> Any:  # noqa: N803
                return self._pages(Prefix)

        return Paginator()


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> tuple[SnapshotStore, FakeS3]:
    store = SnapshotStore(Settings(s3_endpoint="http://minio:9000", hermes_snapshot_keep=2))
    fake = FakeS3()
    monkeypatch.setattr(store, "_client", lambda: fake)
    return store, fake


def _snapshot_file(tmp_path: Path, data: bytes, name: str = "snap.tar") -> tuple[Path, Snapshot]:
    path = tmp_path / name
    path.write_bytes(data)
    return path, Snapshot(sha256=hashlib.sha256(data).hexdigest(), size=len(data), created_at=time.time())


@pytest.mark.asyncio
async def test_store_writes_by_hash_points_latest_and_keeps_two(s3, tmp_path: Path) -> None:
    store, fake = s3
    shas = []
    for i in range(3):
        path, snapshot = _snapshot_file(tmp_path, f"archive {i}".encode(), f"{i}.tar")
        await store.put(ORG, CONVERSATION, path, snapshot, SESSION)
        shas.append(snapshot.sha256)
        fake.objects[f"hermes-sessions/{ORG}/{CONVERSATION}/{snapshot.sha256}.tar"] = (
            fake.objects[f"hermes-sessions/{ORG}/{CONVERSATION}/{snapshot.sha256}.tar"][0],
            datetime.fromtimestamp(1000 + i, tz=timezone.utc),
        )

    assert await store.latest(ORG, CONVERSATION) == shas[2]
    tars = {key for key in fake.objects if key.endswith(".tar")}
    assert tars == {f"hermes-sessions/{ORG}/{CONVERSATION}/{sha}.tar" for sha in shas[1:]}
    pointer = json.loads(fake.objects[f"hermes-sessions/{ORG}/{CONVERSATION}/latest.json"][0])
    assert pointer["session_id"] == SESSION and pointer["sha256"] == shas[2]


@pytest.mark.asyncio
async def test_fetch_checks_the_hash_and_the_bound(s3, tmp_path: Path) -> None:
    store, fake = s3
    path, snapshot = _snapshot_file(tmp_path, b"workspace bytes")
    await store.put(ORG, CONVERSATION, path, snapshot, SESSION)

    await store.fetch(ORG, CONVERSATION, snapshot.sha256, tmp_path / "ok.tar", 1000)
    assert (tmp_path / "ok.tar").read_bytes() == b"workspace bytes"

    key = f"hermes-sessions/{ORG}/{CONVERSATION}/{snapshot.sha256}.tar"
    fake.objects[key] = (b"tampered", fake.objects[key][1])
    with pytest.raises(SnapshotError, match="invalid"):
        await store.fetch(ORG, CONVERSATION, snapshot.sha256, tmp_path / "bad.tar", 1000)
    assert not (tmp_path / "bad.tar").exists()
    with pytest.raises(SnapshotError, match="too_large"):
        await store.fetch(ORG, CONVERSATION, snapshot.sha256, tmp_path / "big.tar", 3)
    with pytest.raises(SnapshotError, match="not_found"):
        await store.fetch(ORG, CONVERSATION, "0" * 64, tmp_path / "none.tar", 1000)


@pytest.mark.asyncio
async def test_latest_rejects_a_malformed_pointer_and_unsafe_keys(s3) -> None:
    store, fake = s3
    assert await store.latest(ORG, CONVERSATION) is None
    fake.objects[f"hermes-sessions/{ORG}/{CONVERSATION}/latest.json"] = (b'{"sha256":"../x"}', datetime.now(timezone.utc))
    with pytest.raises(SnapshotError, match="invalid"):
        await store.latest(ORG, CONVERSATION)
    with pytest.raises(SnapshotError, match="key_invalid"):
        await store.latest("../other", CONVERSATION)


@pytest.mark.asyncio
async def test_sweep_removes_objects_past_the_retention(s3) -> None:
    store, fake = s3
    old = datetime.fromtimestamp(time.time() - 10 * 86400, tz=timezone.utc)
    fake.objects["hermes-sessions/o/c/latest.json"] = (b"{}", old)
    fake.objects["hermes-sessions/o/d/latest.json"] = (b"{}", datetime.now(timezone.utc))

    assert await store.sweep(time.time() - 7 * 86400) == 1
    assert list(fake.objects) == ["hermes-sessions/o/d/latest.json"]


@pytest.mark.asyncio
async def test_store_failures_have_a_code(s3, tmp_path: Path) -> None:
    store, fake = s3
    fake.fail = True
    path, snapshot = _snapshot_file(tmp_path, b"x")
    with pytest.raises(SnapshotError, match="store_failed"):
        await store.put(ORG, CONVERSATION, path, snapshot, SESSION)
    with pytest.raises(SnapshotError, match="store_failed"):
        await store.latest(ORG, CONVERSATION)


# The sandbox lifecycle ------------------------------------------------------


class MemoryStore:
    """SnapshotStore's interface over a dict, with switchable failures."""

    enabled = True

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, bytes]] = {}
        self.latest_sha: dict[tuple[str, str], str] = {}
        self.fail_put = False

    async def put(self, org: str, conversation: str, path: Path, snapshot: Snapshot, session_id: str) -> None:
        if self.fail_put:
            raise SnapshotError("hermes_snapshot_store_failed")
        self.objects.setdefault((org, conversation), {})[snapshot.sha256] = path.read_bytes()
        self.latest_sha[(org, conversation)] = snapshot.sha256

    async def latest(self, org: str, conversation: str) -> str | None:
        return self.latest_sha.get((org, conversation))

    async def fetch(self, org: str, conversation: str, sha256: str, path: Path, limit: int) -> None:
        data = self.objects.get((org, conversation), {}).get(sha256)
        if data is None:
            raise SnapshotError("hermes_snapshot_not_found")
        if hashlib.sha256(data).hexdigest() != sha256:
            raise SnapshotError("hermes_snapshot_invalid")
        path.write_bytes(data)

    async def sweep(self, cutoff: float) -> int:
        return 0

    async def delete_conversation(self, org: str, conversation: str) -> int:
        removed = len(self.objects.pop((org, conversation), {}))
        return removed + (self.latest_sha.pop((org, conversation), None) is not None)


WORKSPACE = _tar(
    [
        ("workspace", None),
        ("workspace/.hermes/config.yaml", f'{{"model":{{"api_key":"{TOKEN}"}}}}'.encode()),
        ("workspace/.aurum/proxy", f"http://hermes:{TOKEN}@hermes-egress:3128".encode()),
        ("workspace/.hermes/state.db", b"state"),
        ("workspace/home/work.py", b"print(1)"),
    ]
)


@pytest.fixture
def lifecycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[DockerHermesSandboxes, FakeDocker, MemoryStore]:
    store = MemoryStore()
    settings = Settings(hermes_enabled=True, hermes_snapshot_dir=str(tmp_path), hermes_snapshot_retry_seconds=300)
    sandboxes = DockerHermesSandboxes(settings, store=store)  # type: ignore[arg-type]
    daemon = FakeDocker()
    sandboxes._docker = daemon  # type: ignore[assignment]
    sandboxes._ready = True

    async def healthy(sandbox: Any) -> None:
        return None

    async def archive(container_id: str, path: Path) -> None:
        path.write_bytes(WORKSPACE)

    monkeypatch.setattr(sandboxes, "_wait_healthy", healthy)
    monkeypatch.setattr(sandboxes, "_workspace_archive", archive)
    return sandboxes, daemon, store


async def _create(sandboxes: DockerHermesSandboxes, token: str = TOKEN, **kwargs: Any) -> Any:
    return await sandboxes.create(
        session_id=SESSION, org_id=ORG, conversation_id=CONVERSATION, token=token, model="m", **kwargs
    )


def _idle(sandboxes: DockerHermesSandboxes, seconds: float = 4000) -> None:
    sandbox = sandboxes.get(SESSION)
    sandboxes._sandboxes[SESSION] = type(sandbox)(**{**sandbox.__dict__, "last_active": time.time() - seconds})


@pytest.mark.asyncio
async def test_an_idle_sandbox_is_stopped_snapshotted_and_removed(lifecycle) -> None:
    sandboxes, daemon, store = lifecycle
    await _create(sandboxes)
    container = daemon.container_map[resource_name(SESSION)]
    _idle(sandboxes)

    assert await sandboxes.reap() == 1

    assert container.stopped and container.deleted
    assert resource_name(SESSION) not in daemon.volume_map
    [stored] = store.objects[(ORG, CONVERSATION)].values()
    assert TOKEN.encode() not in stored
    assert _contents(stored) == {".hermes/state.db": b"state", "home/work.py": b"print(1)"}


@pytest.mark.asyncio
async def test_the_next_create_restores_the_snapshot_before_writing_fresh_secrets(lifecycle) -> None:
    sandboxes, daemon, store = lifecycle
    await _create(sandboxes)
    _idle(sandboxes)
    await sandboxes.reap()

    sandbox, outcome = await _create(sandboxes, token="a-new-session-token-0123")

    container = daemon.container_map[resource_name(SESSION)]
    assert outcome == "restored" and sandbox.state == "running" and container.started
    (_, restored), (_, secrets) = container.archives
    assert _contents(restored) == {".hermes/state.db": b"state", "home/work.py": b"print(1)"}
    assert all(m.uid == 10001 for m in _members(restored).values())
    assert b"a-new-session-token-0123" in _contents(secrets)[".aurum/proxy"]


@pytest.mark.asyncio
async def test_restore_can_be_skipped_or_pinned(lifecycle) -> None:
    sandboxes, daemon, store = lifecycle
    await _create(sandboxes)
    _idle(sandboxes)
    await sandboxes.reap()

    _, outcome = await _create(sandboxes, restore=False)
    assert outcome == "created" and len(daemon.container_map[resource_name(SESSION)].archives) == 1
    await sandboxes.close(SESSION)

    with pytest.raises(HermesSandboxError, match="snapshot_not_found"):
        await _create(sandboxes, snapshot_sha256="0" * 64)
    assert resource_name(SESSION) not in daemon.network_map and resource_name(SESSION) not in daemon.volume_map


@pytest.mark.asyncio
async def test_a_failed_snapshot_keeps_the_sandbox_stopped_with_its_volume(lifecycle) -> None:
    sandboxes, daemon, store = lifecycle
    await _create(sandboxes)
    _idle(sandboxes)
    store.fail_put = True

    assert await sandboxes.reap() == 0

    sandbox = sandboxes.get(SESSION)
    container = daemon.container_map[resource_name(SESSION)]
    assert sandbox.state == "stopped" and container.stopped and not container.deleted
    assert resource_name(SESSION) in daemon.volume_map
    with pytest.raises(HermesSandboxError, match="sandbox_stopped"):
        [line async for line in sandboxes.run(SESSION, {"key": "k", "input": "i", "session_id": "c"})]

    # Retried only after the interval, then removed once it succeeds.
    store.fail_put = False
    assert await sandboxes.reap() == 0 and sandboxes.get(SESSION).state == "stopped"
    sandboxes._sandboxes[SESSION] = type(sandbox)(**{**sandboxes.get(SESSION).__dict__, "snapshot_attempt_at": 0.0})
    assert await sandboxes.reap() == 1
    assert container.deleted and store.objects


@pytest.mark.asyncio
async def test_a_stopped_sandbox_goes_only_when_the_retention_runs_out(lifecycle) -> None:
    sandboxes, daemon, store = lifecycle
    await _create(sandboxes)
    _idle(sandboxes)
    store.fail_put = True
    await sandboxes.reap()
    sandbox = sandboxes.get(SESSION)
    sandboxes._sandboxes[SESSION] = type(sandbox)(**{**sandbox.__dict__, "stopped_at": time.time() - 8 * 86400})

    assert await sandboxes.reap() == 1
    assert resource_name(SESSION) not in daemon.volume_map


@pytest.mark.asyncio
async def test_a_stopped_sandbox_resumes_with_a_fresh_token(lifecycle) -> None:
    sandboxes, daemon, store = lifecycle
    await _create(sandboxes)
    _idle(sandboxes)
    store.fail_put = True
    await sandboxes.reap()
    container = daemon.container_map[resource_name(SESSION)]
    container.started = False

    sandbox, outcome = await _create(sandboxes, token="a-new-session-token-0123")

    assert outcome == "resumed" and sandbox.state == "running" and container.started
    assert b"a-new-session-token-0123" in _contents(container.archives[-1][1])[".aurum/proxy"]


@pytest.mark.asyncio
async def test_a_stopped_sandbox_past_its_lifetime_is_replaced_from_a_snapshot(lifecycle) -> None:
    sandboxes, daemon, store = lifecycle
    await _create(sandboxes)
    _idle(sandboxes)
    store.fail_put = True
    await sandboxes.reap()
    old = daemon.container_map[resource_name(SESSION)]
    sandbox = sandboxes.get(SESSION)
    sandboxes._sandboxes[SESSION] = type(sandbox)(**{**sandbox.__dict__, "created_at": time.time() - 2 * 86400})
    store.fail_put = False

    _, outcome = await _create(sandboxes)

    assert outcome == "restored" and old.deleted
    assert not daemon.container_map[resource_name(SESSION)].deleted


@pytest.mark.asyncio
async def test_close_with_a_snapshot_and_a_live_snapshot(lifecycle) -> None:
    sandboxes, daemon, store = lifecycle
    await _create(sandboxes)

    live = await sandboxes.snapshot(SESSION)
    assert sandboxes.get(SESSION).state == "running"
    assert store.latest_sha[(ORG, CONVERSATION)] == live.sha256

    store.fail_put = True
    with pytest.raises(HermesSandboxError, match="store_failed"):
        await sandboxes.close(SESSION, snapshot=True)
    assert sandboxes.get(SESSION).state == "stopped"
    store.fail_put = False
    final = await sandboxes.close(SESSION, snapshot=True)
    assert final is not None and resource_name(SESSION) not in daemon.volume_map


@pytest.mark.asyncio
async def test_an_exited_container_is_adopted_as_stopped(lifecycle) -> None:
    sandboxes, daemon, store = lifecycle
    await _create(sandboxes)
    daemon.container_map[resource_name(SESSION)]._container["State"] = "exited"
    fresh = DockerHermesSandboxes(Settings(hermes_enabled=True), store=store)  # type: ignore[arg-type]
    fresh._docker = daemon  # type: ignore[assignment]

    await fresh.adopt()

    assert fresh.get(SESSION).state == "stopped"


@pytest.mark.asyncio
async def test_without_a_store_expiry_removes_and_explicit_snapshots_refuse(monkeypatch) -> None:
    sandboxes = DockerHermesSandboxes(Settings(hermes_enabled=True, s3_endpoint=None))
    sandboxes._docker = FakeDocker()  # type: ignore[assignment]
    sandboxes._ready = True

    async def healthy(sandbox: Any) -> None:
        return None

    monkeypatch.setattr(sandboxes, "_wait_healthy", healthy)
    await _create(sandboxes)

    with pytest.raises(HermesSandboxError, match="snapshot_unavailable"):
        await sandboxes.snapshot(SESSION)
    with pytest.raises(HermesSandboxError, match="snapshot_unavailable"):
        await sandboxes.close(SESSION, snapshot=True)
    _idle(sandboxes)
    assert await sandboxes.reap() == 1


@pytest.mark.asyncio
async def test_an_unexpected_snapshot_error_still_leaves_the_sandbox_stopped(lifecycle, monkeypatch) -> None:
    sandboxes, daemon, store = lifecycle
    await _create(sandboxes)
    _idle(sandboxes)

    async def broken(container_id: str, path: Path) -> None:
        raise RuntimeError("payload error")

    monkeypatch.setattr(sandboxes, "_workspace_archive", broken)

    assert await sandboxes.reap() == 0
    assert sandboxes.get(SESSION).state == "stopped"


def test_runs_on_a_stopped_sandbox_answer_409(lifecycle, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from src import hermes_routes
    from src.hermes_runtime import HermesSandbox
    from src.main import app

    sandboxes, _, _ = lifecycle
    sandboxes._sandboxes[SESSION] = HermesSandbox(
        session_id=SESSION, org_id=ORG, conversation_id=CONVERSATION, container_id="c",
        network="n", volume="v", created_at=time.time(), state="stopped",
    )
    monkeypatch.setattr(hermes_routes, "sandboxes", sandboxes)
    monkeypatch.setattr(hermes_routes, "settings", Settings(hermes_enabled=True))

    response = TestClient(app).post(
        f"/hermes/sandboxes/{SESSION}/runs", json={"key": "k", "input": "i", "session_id": "c"}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "hermes_sandbox_stopped"


@pytest.mark.asyncio
async def test_restore_timeouts_and_a_vanished_stopped_container_have_codes(lifecycle, monkeypatch) -> None:
    sandboxes, daemon, store = lifecycle
    await _create(sandboxes)
    _idle(sandboxes)
    store.fail_put = True
    await sandboxes.reap()
    daemon.container_map[resource_name(SESSION)].deleted = True

    with pytest.raises(HermesSandboxError, match="resume_failed"):
        await _create(sandboxes)

    await sandboxes.close(SESSION)
    store.latest_sha[(ORG, CONVERSATION)] = "f" * 64

    async def slow(*args: Any) -> None:
        raise TimeoutError

    monkeypatch.setattr(sandboxes, "_restore", slow)
    with pytest.raises(HermesSandboxError, match="snapshot_timeout"):
        await _create(sandboxes)
    assert resource_name(SESSION) not in daemon.volume_map


@pytest.mark.parametrize(
    "body",
    [
        {"request_id": "../x", "execution_generation": 1, "answer": "a"},
        {"request_id": "abc", "execution_generation": "1", "answer": "a"},
        {"request_id": "abc", "execution_generation": 1, "answer": ""},
        {"request_id": "abc", "execution_generation": 1, "answer": "a" * 16001},
        {"request_id": "abc", "execution_generation": 1, "answer": "a", "extra": 1},
    ],
)
def test_a_clarify_answer_is_bounded(lifecycle, monkeypatch, body) -> None:
    from fastapi.testclient import TestClient

    from src import hermes_routes
    from src.main import app

    sandboxes, _, _ = lifecycle
    monkeypatch.setattr(hermes_routes, "sandboxes", sandboxes)
    monkeypatch.setattr(hermes_routes, "settings", Settings(hermes_enabled=True))

    response = TestClient(app).post(f"/hermes/sandboxes/{SESSION}/runs/run_1/clarify", json=body)

    assert response.status_code in (400, 422)


@pytest.mark.asyncio
async def test_purging_a_conversation_removes_its_sandboxes_and_every_snapshot(lifecycle) -> None:
    sandboxes, daemon, store = lifecycle
    await _create(sandboxes)
    await sandboxes.snapshot(SESSION)
    other = "b" * 32
    await sandboxes.create(session_id=other, org_id=ORG, conversation_id="conv-2", token=TOKEN, model="m")
    store.objects[(ORG, "conv-2")] = {"x": b"kept"}

    purged = await sandboxes.purge_conversation(ORG, CONVERSATION)
    again = await sandboxes.purge_conversation(ORG, CONVERSATION)

    assert purged == {"sandboxes": 1, "snapshots": 2} and again == {"sandboxes": 0, "snapshots": 0}
    assert (ORG, CONVERSATION) not in store.objects and store.latest_sha.get((ORG, CONVERSATION)) is None
    assert daemon.container_map[resource_name(SESSION)].deleted
    assert sandboxes.get(other).conversation_id == "conv-2" and store.objects[(ORG, "conv-2")] == {"x": b"kept"}


def test_the_purge_route_checks_its_identifiers(lifecycle, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from src import hermes_routes
    from src.main import app

    sandboxes, _, _ = lifecycle
    monkeypatch.setattr(hermes_routes, "sandboxes", sandboxes)
    monkeypatch.setattr(hermes_routes, "settings", Settings(hermes_enabled=True))
    client = TestClient(app)

    assert client.delete(f"/hermes/conversations/{ORG}/{CONVERSATION}").json() == {"sandboxes": 0, "snapshots": 0}
    assert client.delete(f"/hermes/conversations/{ORG}/conv_1").status_code == 400
    assert client.delete(f"/hermes/conversations/{ORG}/conv%2F..").status_code == 404


def test_a_file_is_served_as_bytes_and_a_refusal_keeps_its_code(lifecycle, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from src import hermes_routes
    from src.main import app

    sandboxes, _, _ = lifecycle
    paths: list[str] = []

    async def read_file(session_id: str, path: str) -> bytes:
        paths.append(path)
        if path == "secret":
            raise HermesSandboxError("file_not_allowed")
        return b"\x00binary"

    monkeypatch.setattr(sandboxes, "read_file", read_file)
    monkeypatch.setattr(hermes_routes, "sandboxes", sandboxes)
    monkeypatch.setattr(hermes_routes, "settings", Settings(hermes_enabled=True))
    client = TestClient(app)

    served = client.get(f"/hermes/sandboxes/{SESSION}/files", params={"path": "out/chart.png"})
    refused = client.get(f"/hermes/sandboxes/{SESSION}/files", params={"path": "secret"})
    unbounded = client.get(f"/hermes/sandboxes/{SESSION}/files", params={"path": "x" * 1025})

    assert served.status_code == 200 and served.content == b"\x00binary"
    assert served.headers["content-type"] == "application/octet-stream"
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "file_not_allowed"
    assert unbounded.status_code == 422
    assert paths == ["out/chart.png", "secret"]


def test_an_input_is_written_from_its_bytes_and_bounded(lifecycle, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from src import hermes_routes
    from src.main import app

    sandboxes, _, _ = lifecycle
    written: list[tuple[str, bytes]] = []

    async def write_input(session_id: str, name: str, data: bytes) -> None:
        written.append((name, data))

    async def list_inputs(session_id: str) -> dict[str, str]:
        return {"sales.xlsx": "a" * 64}

    monkeypatch.setattr(sandboxes, "write_input", write_input)
    monkeypatch.setattr(sandboxes, "list_inputs", list_inputs)
    monkeypatch.setattr(hermes_routes, "sandboxes", sandboxes)
    monkeypatch.setattr(hermes_routes, "settings", Settings(hermes_enabled=True))
    monkeypatch.setattr(hermes_routes, "FILE_MAX_BYTES", 8)
    client = TestClient(app)

    listed = client.get(f"/hermes/sandboxes/{SESSION}/inputs")
    stored = client.put(f"/hermes/sandboxes/{SESSION}/inputs", params={"name": "sales q1.xlsx"}, content=b"PK book")
    large = client.put(f"/hermes/sandboxes/{SESSION}/inputs", params={"name": "big.csv"}, content=b"x" * 9)

    assert listed.json() == {"files": {"sales.xlsx": "a" * 64}}
    assert stored.status_code == 204 and written == [("sales q1.xlsx", b"PK book")]
    assert large.status_code == 413


def test_a_preview_stream_carries_binary_frames_both_ways_and_refuses_low_ports(lifecycle, monkeypatch) -> None:
    import asyncio as _asyncio
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from src import hermes_routes
    from src.main import app

    sandboxes, _, _ = lifecycle
    opened: list[tuple[str, int]] = []

    class Echo:
        def __init__(self) -> None:
            self.queue: _asyncio.Queue[bytes | None] = _asyncio.Queue()
            self.closed = False

        async def write_in(self, data: bytes) -> None:
            await self.queue.put(b"app:" + data)

        async def read_out(self):  # noqa: ANN202
            data = await self.queue.get()
            return None if data is None else SimpleNamespace(stream=1, data=data)

        async def close(self) -> None:
            self.closed = True

    streams: list[Echo] = []

    async def connect(session_id: str, port: int) -> Echo:
        opened.append((session_id, port))
        streams.append(Echo())
        return streams[-1]

    monkeypatch.setattr(sandboxes, "connect", connect)
    monkeypatch.setattr(hermes_routes, "sandboxes", sandboxes)
    monkeypatch.setattr(hermes_routes, "settings", Settings(hermes_enabled=True, hermes_preview_secret="s3cret"))
    client = TestClient(app)
    auth = {"Authorization": "Bearer s3cret"}

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/hermes/sandboxes/{SESSION}/connect?port=8899") as socket:
            socket.receive_bytes()
    assert opened == []
    with client.websocket_connect(f"/hermes/sandboxes/{SESSION}/connect?port=8899", headers=auth) as socket:
        socket.send_bytes(b"GET / HTTP/1.1\r\n\r\n")
        assert socket.receive_bytes() == b"app:GET / HTTP/1.1\r\n\r\n"
        # Leaving the block cancels the route at once, so a cleanup still in
        # flight is cut short and the cancellation escapes as an error. Close
        # first and let the route finish, as a disconnecting client does.
        socket.close()
        deadline = time.monotonic() + 10
        while not streams[0].closed and time.monotonic() < deadline:
            time.sleep(0.01)

    assert opened == [(SESSION, 8899)] and streams[0].closed
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/hermes/sandboxes/{SESSION}/connect?port=80", headers=auth) as socket:
            socket.receive_bytes()
    assert opened == [(SESSION, 8899)]


def test_the_server_can_serve_the_preview_streams_websocket_route() -> None:
    """The route works under Starlette's test client without uvicorn; served
    for real, uvicorn needs a WebSocket implementation or answers it 404."""
    from uvicorn.config import Config

    config = Config(app="src.main:app", ws="auto")
    config.load()

    assert config.ws_protocol_class is not None
