import asyncio
import io
import tarfile
import zipfile
from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest
from aiodocker.exceptions import DockerError
from aiodocker.stream import Message

from src.config import Settings, settings
from src.container_manager import (
    BundleExtractionError,
    ContainerManager,
    NetworkPolicyError,
    SandboxSession,
    SessionNotFoundError,
    _bundle_to_tar,
)


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_bundle_to_tar_converts_valid_zip():
    raw = _make_zip({"SKILL.md": b"hello", "scripts/a.py": b"print(1)"})
    tar_bytes = _bundle_to_tar(raw)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        names = sorted(tf.getnames())
        assert names == ["SKILL.md", "scripts/a.py"]
        assert tf.extractfile("SKILL.md").read() == b"hello"


def test_bundle_to_tar_rejects_unsafe_path():
    raw = _make_zip({"SKILL.md": b"hello", "../../etc/passwd": b"pwned"})
    with pytest.raises(BundleExtractionError, match="Unsafe path"):
        _bundle_to_tar(raw)


def test_bundle_to_tar_rejects_bad_zip():
    with pytest.raises(BundleExtractionError, match="not a valid zip"):
        _bundle_to_tar(b"not a zip")


def test_bundle_to_tar_rejects_too_many_entries(monkeypatch):
    monkeypatch.setattr(settings, "bundle_max_entries", 2)
    raw = _make_zip({"SKILL.md": b"hello", "a.py": b"1", "b.py": b"2"})
    with pytest.raises(BundleExtractionError, match="too many entries"):
        _bundle_to_tar(raw)


def test_bundle_to_tar_rejects_oversized_uncompressed_content(monkeypatch):
    monkeypatch.setattr(settings, "bundle_max_uncompressed_bytes", 10)
    raw = _make_zip({"SKILL.md": b"x" * 1000})
    with pytest.raises(BundleExtractionError, match="uncompressed size exceeds limit"):
        _bundle_to_tar(raw)


def test_bundle_to_tar_rejects_high_compression_ratio_zip_bomb(monkeypatch):
    monkeypatch.setattr(settings, "bundle_max_compression_ratio", 10)
    monkeypatch.setattr(settings, "bundle_max_uncompressed_bytes", 10 * 1024 * 1024)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # Highly compressible payload -> compression ratio far above the cap,
        # while staying under the uncompressed-size cap on its own.
        zf.writestr("SKILL.md", b"0" * (5 * 1024 * 1024))
    raw = buf.getvalue()
    with pytest.raises(BundleExtractionError, match="compression ratio"):
        _bundle_to_tar(raw)


class FakeStream:
    def __init__(self, messages: list[Message]) -> None:
        self._messages = list(messages)
        self.close = AsyncMock()

    async def read_out(self):
        if self._messages:
            return self._messages.pop(0)
        return None


class ClosingStream(FakeStream):
    def __init__(self, messages: list[Message], close_session: Callable[[], object]) -> None:
        super().__init__(messages)
        self._close_session = close_session
        self._closed_session = False

    async def read_out(self):
        if self._messages:
            return self._messages.pop(0)
        if not self._closed_session:
            self._closed_session = True
            await self._close_session()
        return None


class ReleaseControlledStream(FakeStream):
    def __init__(
        self,
        messages: list[Message],
        started: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        super().__init__(messages)
        self._release = release
        started.set()

    async def read_out(self):
        if self._messages:
            return self._messages.pop(0)
        await self._release.wait()
        return None


class FakeExec:
    def __init__(
        self,
        messages: list[Message],
        exit_code: int | None = 0,
        stream_factory: Callable[[list[Message]], FakeStream] | None = None,
    ) -> None:
        self._messages = messages
        self._exit_code = exit_code
        self._stream_factory = stream_factory
        self.started_with_detach: bool | None = None

    def start(self, detach: bool = False):
        self.started_with_detach = detach
        if self._stream_factory is not None:
            return self._stream_factory(self._messages)
        return FakeStream(self._messages)

    async def inspect(self):
        return {"ExitCode": self._exit_code}


class FakeContainer:
    def __init__(
        self,
        cid: str = "c1",
        exec_result: FakeExec | None = None,
        bootstrap_result: FakeExec | None = None,
    ) -> None:
        self.id = cid
        self.start = AsyncMock()
        self.kill = AsyncMock()
        self.delete = AsyncMock()
        self.put_archive = AsyncMock()
        # create_session()'s own bootstrap tar-extraction/cleanup execs
        # always get a clean success by default, independent of whatever a
        # test wants the *real* skill command (the one under test, via
        # exec_command) to return — otherwise a test fixture built for "the
        # command times out" would make bundle bootstrap itself "time out"
        # too. bootstrap_result overrides this for tests specifically about
        # bootstrap failure.
        self._bootstrap_result = bootstrap_result or FakeExec(messages=[], exit_code=0)
        self._exec_result = exec_result
        self._exec_results: list[FakeExec] = []
        self.exec_calls: list[dict] = []

    async def exec(self, **kwargs):
        self.exec_calls.append(kwargs)
        cmd = kwargs.get("cmd") or []
        if cmd and cmd[0] in ("tar", "rm"):
            return self._bootstrap_result
        if self._exec_results:
            return self._exec_results.pop(0)
        return self._exec_result if self._exec_result is not None else self._bootstrap_result


class FakeContainers:
    def __init__(self, container: FakeContainer | None = None) -> None:
        self.create = AsyncMock(return_value=container)
        self.get = AsyncMock(return_value=container)
        self.list = AsyncMock(return_value=[])


class FakeNetworks:
    def __init__(self, existing: bool = False) -> None:
        if existing:
            self.get = AsyncMock(return_value=None)
        else:
            self.get = AsyncMock(side_effect=DockerError(404, {"message": "network not found"}))
        self.create = AsyncMock(return_value=None)


class FakeDocker:
    def __init__(
        self, container: FakeContainer | None = None, networks: FakeNetworks | None = None
    ) -> None:
        self.containers = FakeContainers(container)
        self.networks = networks or FakeNetworks()
        self.close = AsyncMock()


def make_manager(
    container: FakeContainer | None = None, networks: FakeNetworks | None = None
) -> ContainerManager:
    return ContainerManager(docker=FakeDocker(container, networks))  # type: ignore[arg-type]


def test_factory_keeps_docker_default() -> None:
    # Catches accidentally switching the default runtime away from the
    # existing Docker implementation while Kubernetes support is optional.
    from src.sandbox_runtime import create_sandbox_runtime
    from src.sandbox_runtime.docker import DockerSandboxRuntime

    assert isinstance(create_sandbox_runtime(Settings(_env_file=None)), DockerSandboxRuntime)


def test_container_manager_positional_docker_injection_is_preserved() -> None:
    # Catches breaking the old ContainerManager(docker) ABI; callers that pass
    # a Docker client positionally must not have it interpreted as settings.
    fake_docker = FakeDocker(FakeContainer())

    manager = ContainerManager(fake_docker)  # type: ignore[arg-type]

    assert manager.docker is fake_docker


@pytest.mark.asyncio
async def test_docker_shutdown_closes_live_sessions(monkeypatch) -> None:
    # Catches shutdown implementations that close only the Docker client or
    # only the session map, leaving real sandbox containers alive.
    from src.sandbox_runtime.docker import DockerSandboxRuntime

    fake_container = FakeContainer(cid="container1")
    fake_docker = FakeDocker(fake_container)
    monkeypatch.setattr("src.sandbox_runtime.docker.aiodocker.Docker", lambda: fake_docker)
    runtime = DockerSandboxRuntime()
    runtime._sessions["session1"] = SandboxSession(
        session_id="session1",
        resource_id="container1",
        resource_uid="container1",
        resource_name="skill-sandbox-session1",
        skill_md="instructions",
        created_at=1.0,
        last_active=1.0,
    )

    await runtime.shutdown()

    assert runtime._sessions == {}
    fake_docker.containers.get.assert_awaited_once_with("container1")
    fake_container.kill.assert_awaited_once()
    fake_container.delete.assert_awaited_once_with(force=True)
    fake_docker.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_session_copies_bundle_and_tracks_session():
    container = FakeContainer()
    manager = make_manager(container)
    raw = _make_zip({"SKILL.md": b"hello"})

    session = await manager.create_session(raw, "instructions text", network_policy="none")

    container.start.assert_awaited_once()
    container.put_archive.assert_awaited_once()
    # /skill is tmpfs (see the dedicated test below) — put_archive can't
    # write through a tmpfs mount, so the tar lands as a plain file on the
    # regular writable layer first...
    assert container.put_archive.call_args.kwargs["path"] == "/tmp"
    # ...and gets extracted into /skill by a real process inside the
    # container instead, which does see the live tmpfs mount.
    assert container.exec_calls[0]["cmd"] == [
        "tar", "-x", "-C", "/skill", "-f", "/tmp/bundle.tar",
    ]
    assert container.exec_calls[1]["cmd"] == ["rm", "-f", "/tmp/bundle.tar"]
    # put_archive's extraction leaves it root-owned; /tmp's sticky bit means
    # only the owner (or root) can unlink it there.
    assert container.exec_calls[1]["user"] == "root"
    assert session.skill_md == "instructions text"
    assert session.session_id in manager._sessions


@pytest.mark.asyncio
async def test_create_session_mounts_skill_dir_as_tmpfs():
    # The bundle is one-shot content for a one-shot container — /skill must
    # never land on the container's overlay writable layer (host disk), only
    # in-memory tmpfs, world-writable (mode=1777) since the container runs
    # as non-root skilluser which doesn't own a freshly mounted tmpfs root.
    container = FakeContainer()
    manager = make_manager(container)
    raw = _make_zip({"SKILL.md": b"hello"})

    await manager.create_session(raw, "instructions text", network_policy="none")

    create_kwargs = manager.docker.containers.create.call_args.kwargs
    tmpfs = create_kwargs["config"]["HostConfig"]["Tmpfs"]
    assert "/skill" in tmpfs
    assert "mode=1777" in tmpfs["/skill"]
    assert f"size={settings.sandbox_skill_tmpfs_mb}m" in tmpfs["/skill"]


@pytest.mark.asyncio
async def test_create_session_rejects_invalid_bundle_before_creating_container():
    container = FakeContainer()
    manager = make_manager(container)

    with pytest.raises(BundleExtractionError):
        await manager.create_session(b"not a zip", "docs", network_policy="none")

    # The bundle is validated/converted before any container is created —
    # no wasted container for a bundle that was never going to work.
    manager.docker.containers.create.assert_not_awaited()
    assert manager._sessions == {}


@pytest.mark.asyncio
async def test_create_session_removes_container_on_put_archive_failure():
    container = FakeContainer()
    container.put_archive = AsyncMock(side_effect=RuntimeError("docker daemon error"))
    manager = make_manager(container)
    raw = _make_zip({"SKILL.md": b"hello"})

    with pytest.raises(RuntimeError):
        await manager.create_session(raw, "docs", network_policy="none")

    container.kill.assert_awaited_once()
    container.delete.assert_awaited_once()
    assert manager._sessions == {}


@pytest.mark.asyncio
async def test_create_session_removes_container_when_tar_extraction_fails():
    # A non-zero exit from the in-container `tar -x` (e.g. a corrupt archive
    # that still passed the earlier zip-level validation somehow) must not
    # leave a half-initialized session behind.
    bootstrap_result = FakeExec(messages=[Message(2, b"tar: unexpected EOF")], exit_code=2)
    container = FakeContainer(bootstrap_result=bootstrap_result)
    manager = make_manager(container)
    raw = _make_zip({"SKILL.md": b"hello"})

    with pytest.raises(BundleExtractionError, match="exit code 2"):
        await manager.create_session(raw, "docs", network_policy="none")

    container.kill.assert_awaited_once()
    container.delete.assert_awaited_once()
    assert manager._sessions == {}


def test_container_config_none_policy_ignores_sandbox_network_default(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_network_default", "aurum-ai_skills-egress-net")
    manager = make_manager()

    config = manager._container_config("none")

    assert config["HostConfig"]["NetworkMode"] == "none"


def test_container_config_egress_uses_configured_network(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_network_default", "aurum-ai_skills-egress-net")
    manager = make_manager()

    config = manager._container_config("egress")

    assert config["HostConfig"]["NetworkMode"] == "aurum-ai_skills-egress-net"


@pytest.mark.parametrize("forbidden", ["none", "default", "skills-net", "runner-net", "rag-net"])
def test_container_config_egress_rejects_unconfigured_or_internal_networks(monkeypatch, forbidden):
    """SKILL_EXECUTOR_SANDBOX_NETWORK_DEFAULT left unset
    (still "none") must fail loudly, not silently give an egress-marked
    skill NetworkMode:"none" — and it must never be one of this stack's own
    internal networks, which would expose Postgres/Keycloak/skill-executor
    itself to LLM-controlled sandbox code."""
    monkeypatch.setattr(settings, "sandbox_network_default", forbidden)
    manager = make_manager()

    with pytest.raises(NetworkPolicyError, match="egress"):
        manager._container_config("egress")


@pytest.mark.asyncio
async def test_create_session_egress_without_configured_network_creates_no_container(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_network_default", "none")
    container = FakeContainer()
    manager = make_manager(container)
    raw = _make_zip({"SKILL.md": b"hello"})

    with pytest.raises(NetworkPolicyError):
        await manager.create_session(raw, "docs", network_policy="egress")

    manager.docker.containers.create.assert_not_awaited()
    assert manager._sessions == {}


@pytest.mark.asyncio
async def test_ensure_egress_network_is_noop_when_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_network_default", "none")
    networks = FakeNetworks()
    manager = make_manager(networks=networks)

    await manager.ensure_egress_network()

    networks.get.assert_not_awaited()
    networks.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_egress_network_skips_create_when_already_present(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_network_default", "aurum-ai-skill-egress")
    networks = FakeNetworks(existing=True)
    manager = make_manager(networks=networks)

    await manager.ensure_egress_network()

    networks.get.assert_awaited_once_with("aurum-ai-skill-egress")
    networks.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_egress_network_creates_it_when_missing(monkeypatch):
    monkeypatch.setattr(settings, "sandbox_network_default", "aurum-ai-skill-egress")
    networks = FakeNetworks(existing=False)
    manager = make_manager(networks=networks)

    await manager.ensure_egress_network()

    networks.create.assert_awaited_once_with(
        {"Name": "aurum-ai-skill-egress", "Driver": "bridge"}
    )


@pytest.mark.asyncio
async def test_exec_command_returns_stdout_stderr_exit_code():
    exec_result = FakeExec(
        messages=[Message(1, b"hello "), Message(1, b"world"), Message(2, b"warn")],
        exit_code=0,
    )
    container = FakeContainer(exec_result=exec_result)
    manager = make_manager(container)
    raw = _make_zip({"SKILL.md": b"hello"})
    session = await manager.create_session(raw, "docs")

    result = await manager.exec_command(session.session_id, "echo hi")

    assert result["stdout"] == "hello world"
    assert result["stderr"] == "warn"
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    # The in-container coreutils `timeout` wrapper enforces the budget on the
    # command itself (Docker can't kill an exec'd process from outside).
    # exec_calls[0:2] are create_session()'s own bootstrap tar/rm calls.
    assert container.exec_calls[-1]["cmd"] == [
        "timeout", "-k", "5", "60", "/bin/sh", "-c", "echo hi",
    ]


@pytest.mark.asyncio
async def test_exec_command_unknown_session_raises():
    manager = make_manager(FakeContainer())
    with pytest.raises(SessionNotFoundError):
        await manager.exec_command("nope", "echo hi")


@pytest.mark.asyncio
async def test_close_session_is_idempotent():
    container = FakeContainer()
    manager = make_manager(container)
    raw = _make_zip({"SKILL.md": b"hello"})
    session = await manager.create_session(raw, "docs")

    await manager.close_session(session.session_id)
    assert session.session_id not in manager._sessions
    container.kill.assert_awaited_once()

    # Closing again must not raise or re-kill.
    await manager.close_session(session.session_id)
    container.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_reap_expired_closes_stale_sessions(monkeypatch):
    monkeypatch.setattr(settings, "session_ttl_seconds", 0)
    container = FakeContainer()
    manager = make_manager(container)
    raw = _make_zip({"SKILL.md": b"hello"})
    await manager.create_session(raw, "docs")

    reaped = await manager.reap_expired()

    assert reaped == 1
    assert manager._sessions == {}
    container.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_exec_command_truncates_oversized_output(monkeypatch):
    monkeypatch.setattr(settings, "exec_output_max_bytes", 10)
    exec_result = FakeExec(
        messages=[Message(1, b"0123456789ABCDEF"), Message(2, b"tail")],
        exit_code=0,
    )
    container = FakeContainer(exec_result=exec_result)
    manager = make_manager(container)
    session = await manager.create_session(_make_zip({"SKILL.md": b"x"}), "docs")

    result = await manager.exec_command(session.session_id, "spam")

    assert result["stdout"] == "0123456789\n[output truncated at 10 bytes]"
    # stderr stayed under its own (per-stream) cap — no marker.
    assert result["stderr"] == "tail"
    assert result["timed_out"] is False


@pytest.mark.asyncio
async def test_exec_command_maps_timeout_exit_code_to_timed_out():
    # coreutils `timeout` inside the container exits 124 when the budget
    # expires — the API result must report that as a timeout.
    exec_result = FakeExec(messages=[Message(1, b"partial")], exit_code=124)
    container = FakeContainer(exec_result=exec_result)
    manager = make_manager(container)
    session = await manager.create_session(_make_zip({"SKILL.md": b"x"}), "docs")

    result = await manager.exec_command(session.session_id, "sleep 999")

    assert result["timed_out"] is True
    assert result["exit_code"] == 124
    assert result["stdout"] == "partial"


@pytest.mark.asyncio
async def test_exec_command_does_not_resurrect_session_closed_during_command():
    # Catches exec_command writing a stale session back after close_session()
    # wins the race while the Docker exec stream is still in flight.
    container = FakeContainer()
    manager = make_manager(container)
    session = await manager.create_session(_make_zip({"SKILL.md": b"x"}), "docs")

    async def close_current_session() -> None:
        await manager.close_session(session.session_id)

    container._exec_result = FakeExec(
        messages=[Message(1, b"done")],
        exit_code=0,
        stream_factory=lambda messages: ClosingStream(messages, close_current_session),
    )

    result = await manager.exec_command(session.session_id, "echo done")

    assert result["stdout"] == "done"
    assert session.session_id not in manager._sessions
    container.kill.assert_awaited_once()
    container.delete.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_exec_command_keeps_latest_completion_timestamp_for_overlapping_execs(
    monkeypatch,
) -> None:
    # Catches an exec completion skipping last_active just because an earlier
    # overlapping exec already replaced the same live session entry.
    bundle_bytes = _make_zip({"SKILL.md": b"x"})
    completion_times = iter([1_800_000_000.0, 1_800_000_001.0, 1_800_000_002.0])
    monkeypatch.setattr("src.sandbox_runtime.docker.time.time", lambda: next(completion_times))
    container = FakeContainer()
    manager = make_manager(container)
    session = await manager.create_session(bundle_bytes, "docs")
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    container._exec_results = [
        FakeExec(
            messages=[Message(1, b"first")],
            stream_factory=lambda messages: ReleaseControlledStream(
                messages, first_started, release_first
            ),
        ),
        FakeExec(
            messages=[Message(1, b"second")],
            stream_factory=lambda messages: ReleaseControlledStream(
                messages, second_started, release_second
            ),
        ),
    ]

    first = asyncio.create_task(manager.exec_command(session.session_id, "first"))
    second = asyncio.create_task(manager.exec_command(session.session_id, "second"))
    await first_started.wait()
    await second_started.wait()

    release_first.set()
    assert (await first)["stdout"] == "first"
    assert manager._sessions[session.session_id].last_active == 1_800_000_001.0

    release_second.set()
    assert (await second)["stdout"] == "second"
    assert manager._sessions[session.session_id].last_active == 1_800_000_002.0


class FakeListedContainer:
    """Shape of aiodocker's containers.list() items: .id + raw JSON dict."""

    def __init__(self, cid: str, created: float) -> None:
        self.id = cid
        self._container = {"Id": cid, "Created": created}
        self.kill = AsyncMock()
        self.delete = AsyncMock()


@pytest.mark.asyncio
async def test_reap_orphans_removes_expired_unknown_containers(monkeypatch):
    monkeypatch.setattr(settings, "session_ttl_seconds", 100)
    import time

    now = time.time()
    orphan = FakeListedContainer("orphan-1", created=now - 500)
    fresh = FakeListedContainer("fresh-1", created=now - 5)
    container = FakeContainer(cid="known-1")
    manager = make_manager(container)
    session = await manager.create_session(_make_zip({"SKILL.md": b"x"}), "docs")
    known = FakeListedContainer("known-1", created=now - 500)
    manager.docker.containers.list.return_value = [orphan, fresh, known]

    reaped = await manager.reap_orphans()

    # Only the expired container nobody tracks is removed: known sessions and
    # young containers (possibly a live session of another instance) survive.
    assert reaped == 1
    orphan.kill.assert_awaited_once()
    orphan.delete.assert_awaited_once()
    fresh.kill.assert_not_awaited()
    known.kill.assert_not_awaited()
    assert session.session_id in manager._sessions
