"""Hermes sandbox capacity caps, least-recently-used eviction and workspace limits."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from src.config import Settings
from src.hermes_runtime import DockerHermesSandboxes, HermesSandboxError, resource_name
from tests.test_hermes_runtime import TOKEN, FakeDocker
from tests.test_hermes_snapshots import WORKSPACE, MemoryStore


def _session(n: int) -> str:
    return f"{n:032x}"


@pytest.fixture
def make(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    def build(**overrides: Any) -> tuple[DockerHermesSandboxes, FakeDocker, MemoryStore, dict[str, list[str]]]:
        store = MemoryStore()
        settings = Settings(
            hermes_enabled=True,
            hermes_snapshot_dir=str(tmp_path),
            hermes_max_running=3,
            hermes_max_running_per_org=2,
            **overrides,
        )
        sandboxes = DockerHermesSandboxes(settings, store=store)  # type: ignore[arg-type]
        daemon = FakeDocker()
        sandboxes._docker = daemon  # type: ignore[assignment]
        sandboxes._ready = True
        active: dict[str, list[str]] = {}
        stopped: list[tuple[str, list[str]]] = []

        async def healthy(sandbox: Any) -> None:
            return None

        async def archive(container_id: str, path: Path) -> None:
            path.write_bytes(WORKSPACE)

        async def runs(sandbox: Any) -> list[str]:
            return active.get(sandbox.session_id, [])

        async def exec_(sandbox: Any, cmd: list[str]) -> tuple[int, bytes]:
            stopped.append((sandbox.session_id, cmd))
            return 0, b""

        monkeypatch.setattr(sandboxes, "_wait_healthy", healthy)
        monkeypatch.setattr(sandboxes, "_workspace_archive", archive)
        monkeypatch.setattr(sandboxes, "_active_runs", runs)
        monkeypatch.setattr(sandboxes, "_exec", exec_)
        sandboxes.stopped_runs = stopped  # type: ignore[attr-defined]
        return sandboxes, daemon, store, active

    return build


async def _create(sandboxes: DockerHermesSandboxes, n: int, org: str = "org-1") -> Any:
    return await sandboxes.create(
        session_id=_session(n), org_id=org, conversation_id=f"conv-{n}", token=TOKEN, model="m"
    )


def _age(sandboxes: DockerHermesSandboxes, n: int, seconds: float) -> None:
    sandbox = sandboxes.get(_session(n))
    sandboxes._sandboxes[sandbox.session_id] = type(sandbox)(**{**sandbox.__dict__, "last_active": time.time() - seconds})


@pytest.mark.asyncio
async def test_the_org_cap_retires_its_least_recently_used_idle_sandbox(make) -> None:
    sandboxes, daemon, store, _ = make()
    await _create(sandboxes, 1)
    await _create(sandboxes, 2)
    _age(sandboxes, 1, 600)
    _age(sandboxes, 2, 60)

    await _create(sandboxes, 3)

    assert daemon.container_map[resource_name(_session(1))].deleted
    assert not daemon.container_map[resource_name(_session(2))].deleted
    assert ("org-1", "conv-1") in store.latest_sha


@pytest.mark.asyncio
async def test_busy_sandboxes_are_skipped_and_all_busy_refuses(make) -> None:
    sandboxes, daemon, _, active = make()
    await _create(sandboxes, 1)
    await _create(sandboxes, 2)
    _age(sandboxes, 1, 600)
    active[_session(1)] = ["run_1"]

    await _create(sandboxes, 3)
    assert not daemon.container_map[resource_name(_session(1))].deleted
    assert daemon.container_map[resource_name(_session(2))].deleted

    sandboxes._streams[_session(3)] = 1
    with pytest.raises(HermesSandboxError, match="capacity_exhausted"):
        await _create(sandboxes, 4)
    assert resource_name(_session(4)) not in daemon.network_map
    assert sandboxes._reserved == {}


@pytest.mark.asyncio
async def test_the_operator_cap_evicts_across_organizations(make) -> None:
    sandboxes, daemon, _, _ = make()
    await _create(sandboxes, 1, "org-a")
    await _create(sandboxes, 2, "org-b")
    await _create(sandboxes, 3, "org-c")
    _age(sandboxes, 2, 900)

    await _create(sandboxes, 4, "org-d")

    assert daemon.container_map[resource_name(_session(2))].deleted
    assert sum(s.state == "running" for s in sandboxes._sandboxes.values()) == 3


@pytest.mark.asyncio
async def test_stopped_sandboxes_do_not_count_and_a_failed_eviction_still_frees(make) -> None:
    sandboxes, daemon, store, _ = make()
    await _create(sandboxes, 1)
    await _create(sandboxes, 2)
    store.fail_put = True

    await _create(sandboxes, 3)

    states = {n: sandboxes.get(_session(n)).state for n in (1, 2, 3)}
    assert sorted(states.values()) == ["running", "running", "stopped"]
    await _create(sandboxes, 4, "org-2")


@pytest.mark.asyncio
async def test_resuming_a_stopped_sandbox_is_admitted_too(make) -> None:
    sandboxes, daemon, store, _ = make()
    await _create(sandboxes, 1)
    store.fail_put = True
    _age(sandboxes, 1, 4000)
    await sandboxes.reap()
    assert sandboxes.get(_session(1)).state == "stopped"
    await _create(sandboxes, 2)
    await _create(sandboxes, 3)
    _age(sandboxes, 2, 100)

    _, outcome = await _create(sandboxes, 1)

    assert outcome == "resumed"
    assert sandboxes.get(_session(2)).state == "stopped"


@pytest.mark.asyncio
async def test_a_tmpfs_workspace_is_bounded_and_paused_instead_of_stopped(make) -> None:
    sandboxes, daemon, store, _ = make(hermes_workspace_mode="tmpfs", hermes_workspace_max_bytes=512 * 1024**2)
    await _create(sandboxes, 1)
    name = resource_name(_session(1))

    options = daemon.volume_map[name]["DriverOpts"]
    assert options["type"] == "tmpfs" and "size=536870912" in options["o"] and "uid=10001" in options["o"]
    assert daemon.container_map[name].config["HostConfig"]["Memory"] == 2 * 1024**3 + 512 * 1024**2
    # Files land after the start, when the tmpfs is mounted.
    assert daemon.container_map[name].started and daemon.container_map[name].archives

    store.fail_put = True
    _age(sandboxes, 1, 4000)
    await sandboxes.reap()
    container = daemon.container_map[name]
    assert container.paused and not container.stopped and not container.deleted

    store.fail_put = False
    _, outcome = await _create(sandboxes, 1)
    assert outcome == "restored" and container.deleted


@pytest.mark.asyncio
async def test_a_lost_workspace_is_removed_without_replacing_the_snapshot(make, monkeypatch) -> None:
    sandboxes, daemon, store, _ = make()
    await _create(sandboxes, 1)
    store.latest_sha[("org-1", "conv-1")] = "good"

    async def empty(container_id: str, path: Path) -> None:
        from tests.test_hermes_snapshots import _tar

        path.write_bytes(_tar([("workspace", None), ("workspace/.hermes", None)]))

    monkeypatch.setattr(sandboxes, "_workspace_archive", empty)
    _age(sandboxes, 1, 4000)

    assert await sandboxes.reap() == 1
    assert store.latest_sha[("org-1", "conv-1")] == "good"
    assert daemon.container_map[resource_name(_session(1))].deleted


@pytest.mark.asyncio
async def test_over_the_soft_limit_runs_stop_and_new_ones_are_refused(make) -> None:
    sandboxes, daemon, _, active = make(hermes_workspace_max_bytes=1000)
    await _create(sandboxes, 1)
    name = resource_name(_session(1))
    daemon.volume_sizes[name] = 5000
    active[_session(1)] = ["run_7"]

    await sandboxes.reap()

    assert sandboxes.get(_session(1)).workspace_full
    assert (_session(1), ["hermes-relay", "stop", "run_7"]) in sandboxes.stopped_runs  # type: ignore[attr-defined]
    with pytest.raises(HermesSandboxError, match="workspace_full"):
        [line async for line in sandboxes.run(_session(1), {"key": "k", "input": "i", "session_id": "c"})]

    daemon.volume_sizes[name] = 10
    sandboxes._disk_checked_at = 0.0
    await sandboxes.reap()
    assert not sandboxes.get(_session(1)).workspace_full


@pytest.mark.asyncio
async def test_the_soft_limit_is_checked_at_its_interval_only(make) -> None:
    sandboxes, daemon, _, _ = make(hermes_workspace_max_bytes=1000, hermes_disk_check_interval_seconds=3600)
    await _create(sandboxes, 1)
    await sandboxes.reap()
    daemon.volume_sizes[resource_name(_session(1))] = 5000

    await sandboxes.reap()

    assert not sandboxes.get(_session(1)).workspace_full


def test_a_snapshot_source_without_the_platform_marker_is_refused(tmp_path: Path) -> None:
    from src.hermes_snapshots import SnapshotError, sanitize
    from tests.test_hermes_snapshots import _tar

    source = tmp_path / "in.tar"
    source.write_bytes(_tar([("workspace/home/a", b"a")]))
    with pytest.raises(SnapshotError, match="workspace_lost"):
        sanitize(source, tmp_path / "out.tar", strip="workspace", max_bytes=100, max_entries=10, marker=".aurum/proxy")
    source.write_bytes(_tar([("workspace/.aurum/proxy", b"p"), ("workspace/home/a", b"a")]))
    sanitize(source, tmp_path / "ok.tar", strip="workspace", max_bytes=100, max_entries=10, marker=".aurum/proxy")


@pytest.mark.asyncio
async def test_active_runs_parses_only_the_relays_bounded_answer(monkeypatch) -> None:
    sandboxes = DockerHermesSandboxes(Settings(hermes_enabled=True))
    answers = iter(
        [
            b'noise\n{"type":"active","run_ids":["run_1",5,"' + b"x" * 200 + b'"]}',
            b'{"type":"error","status":500}',
        ]
    )

    async def exec_(sandbox: Any, cmd: list[str]) -> tuple[int, bytes]:
        assert cmd == ["hermes-relay", "active"]
        return 0, next(answers)

    monkeypatch.setattr(sandboxes, "_exec", exec_)

    assert await sandboxes._active_runs(None) == ["run_1"]  # type: ignore[arg-type]
    assert await sandboxes._active_runs(None) == []  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_store_that_is_down_evicts_no_one(make, monkeypatch) -> None:
    from src.hermes_snapshots import SnapshotError

    sandboxes, daemon, store, _ = make()
    await _create(sandboxes, 1)
    await _create(sandboxes, 2)

    async def down(org: str, conversation: str) -> None:
        raise SnapshotError("hermes_snapshot_store_failed")

    monkeypatch.setattr(store, "latest", down)
    with pytest.raises(HermesSandboxError, match="store_failed"):
        await _create(sandboxes, 3)

    assert all(not daemon.container_map[resource_name(_session(n))].stopped for n in (1, 2))
    assert resource_name(_session(3)) not in daemon.network_map
    assert sandboxes._reserved == {}


@pytest.mark.asyncio
async def test_an_oversized_workspace_is_removed_keeping_the_last_snapshot_that_fit(make) -> None:
    from src.hermes_runtime import HermesSandboxDropped

    sandboxes, daemon, store, _ = make(hermes_snapshot_max_bytes=4)
    await _create(sandboxes, 1)
    store.latest_sha[("org-1", "conv-1")] = "fit"
    _age(sandboxes, 1, 4000)

    assert await sandboxes.reap() == 1

    assert daemon.container_map[resource_name(_session(1))].deleted
    assert resource_name(_session(1)) not in daemon.volume_map
    assert store.latest_sha[("org-1", "conv-1")] == "fit"

    await _create(sandboxes, 2)
    with pytest.raises(HermesSandboxDropped, match="too_large"):
        await sandboxes.close(_session(2), snapshot=True)
    assert resource_name(_session(2)) not in daemon.volume_map


@pytest.mark.asyncio
async def test_a_clarify_answer_reaches_the_relay_through_a_file(make, monkeypatch) -> None:
    sandboxes, daemon, _, _ = make()
    await _create(sandboxes, 1)
    answers = iter([b'{"type":"clarify","status":200}', b'{"type":"clarify","status":409}'])
    commands: list[list[str]] = []

    async def exec_(sandbox: Any, cmd: list[str]) -> tuple[int, bytes]:
        commands.append(cmd)
        return 0, next(answers)

    monkeypatch.setattr(sandboxes, "_exec", exec_)
    body = {"request_id": "abc123", "execution_generation": 1, "answer": "Rome; rm -rf /"}

    await sandboxes.clarify(_session(1), "run_1", body)
    with pytest.raises(HermesSandboxError, match="hermes_clarify_settled"):
        await sandboxes.clarify(_session(1), "run_1", body)

    path = commands[0][3]
    assert commands[0][:3] == ["hermes-relay", "clarify", "run_1"] and path.startswith("/workspace/.aurum/clarify/")
    [(_, archive)] = daemon.container_map[resource_name(_session(1))].archives[-1:]
    assert b"Rome; rm -rf /" in archive


@pytest.mark.asyncio
async def test_memory_goes_into_the_sandbox_as_a_file_and_comes_back_bounded(make, monkeypatch) -> None:
    sandboxes, daemon, _, _ = make()
    await _create(sandboxes, 1)
    answers = iter([b'{"type":"memory-written"}', b'{"type":"memory","memory":"notes","user":null}'])
    commands: list[list[str]] = []

    async def exec_(sandbox: Any, cmd: list[str]) -> tuple[int, bytes]:
        commands.append(cmd)
        return 0, next(answers)

    monkeypatch.setattr(sandboxes, "_exec", exec_)

    await sandboxes.write_memory(_session(1), {"memory": "notes", "user": "likes tea"})
    read = await sandboxes.read_memory(_session(1))

    assert commands[0][:2] == ["hermes-relay", "memory-write"] and commands[0][2].startswith("/workspace/.aurum/memory/")
    assert commands[1] == ["hermes-relay", "memory-read"]
    [(_, archive)] = daemon.container_map[resource_name(_session(1))].archives[-1:]
    assert b"likes tea" in archive
    assert read == {"memory": "notes", "user": None}


@pytest.mark.asyncio
async def test_skills_come_back_one_per_line_and_only_in_their_shape(make, monkeypatch) -> None:
    sandboxes, _, _, _ = make()
    await _create(sandboxes, 1)
    commands: list[list[str]] = []
    lines = [
        {"type": "skill", "path": "mine", "files": [{"path": "SKILL.md", "content": "---\nname: mine\n---\n"}]},
        {"type": "skill", "path": "odd", "files": [{"path": "SKILL.md", "content": 7}]},
        {"type": "event", "event": {"event": "run.completed"}},
        {"type": "skills", "count": 2, "skipped": 1},
    ]

    async def stream(sandbox: Any, cmd: list[str]):  # noqa: ANN202
        commands.append(cmd)
        for line in lines:
            yield line

    monkeypatch.setattr(sandboxes, "_stream", stream)

    read = await sandboxes.read_skills(_session(1))

    assert commands == [["hermes-relay", "skills-read"]]
    assert read == {"skills": [{"path": "mine", "files": lines[0]["files"]}], "skipped": 1}

    lines.pop()
    with pytest.raises(HermesSandboxError) as failed:
        await sandboxes.read_skills(_session(1))
    assert failed.value.code == "hermes_skills_failed"


@pytest.mark.asyncio
async def test_a_file_comes_back_only_when_its_chunks_match_the_closing_line(make, monkeypatch) -> None:
    import base64 as _base64
    import hashlib as _hashlib

    sandboxes, _, _, _ = make()
    await _create(sandboxes, 1)
    data = b"month,revenue\nJan,10\n"
    commands: list[list[str]] = []
    lines: list[dict[str, Any]] = []

    async def stream(sandbox: Any, cmd: list[str]):  # noqa: ANN202
        commands.append(cmd)
        for line in lines:
            yield line

    monkeypatch.setattr(sandboxes, "_stream", stream)
    chunks = [{"type": "chunk", "data": _base64.b64encode(part).decode()} for part in (data[:5], data[5:])]
    closing = {"type": "file", "size": len(data), "sha256": _hashlib.sha256(data).hexdigest()}

    lines[:] = [*chunks, closing]
    assert await sandboxes.read_file(_session(1), "out/data.csv") == data
    assert commands == [["hermes-relay", "file-read", "out/data.csv"]]

    failures = {
        "hermes_file_failed": [
            [*chunks],  # no closing line
            [chunks[0], closing],  # a chunk lost
            [*chunks, {**closing, "sha256": "0" * 64}],
            [{"type": "chunk", "data": "not base64!"}, closing],
            [{"type": "file-refused", "code": "something the agent made up"}],
        ],
        "file_not_allowed": [[{"type": "file-refused", "code": "file_not_allowed"}]],
        "file_too_large": [[{"type": "chunk", "data": _base64.b64encode(b"x" * (20 * 1024 * 1024 + 1)).decode()}]],
    }
    for code, cases in failures.items():
        for case in cases:
            lines[:] = case
            with pytest.raises(HermesSandboxError) as failed:
                await sandboxes.read_file(_session(1), "out/data.csv")
            assert failed.value.code == code


@pytest.mark.asyncio
async def test_inputs_are_listed_by_hash_and_written_through_a_staging_folder(make, monkeypatch) -> None:
    sandboxes, daemon, _, _ = make()
    await _create(sandboxes, 1)
    commands: list[list[str]] = []
    digest = "a" * 64
    answers = iter(
        [
            json.dumps({"type": "inputs", "files": {"sales.xlsx": digest, "odd": "not a hash", "a/b": digest}}).encode(),
            b'{"type":"inputs-written","count":1}',
        ]
    )

    async def exec_(sandbox: Any, cmd: list[str]) -> tuple[int, bytes]:
        commands.append(cmd)
        return 0, next(answers)

    monkeypatch.setattr(sandboxes, "_exec", exec_)

    assert await sandboxes.list_inputs(_session(1)) == {"sales.xlsx": digest}
    await sandboxes.write_input(_session(1), "sales q1.xlsx", b"PK workbook")

    assert commands[0] == ["hermes-relay", "inputs-list"]
    assert commands[1][:2] == ["hermes-relay", "inputs-write"] and commands[1][2].startswith("/workspace/.aurum/inputs/")
    [(_, archive)] = daemon.container_map[resource_name(_session(1))].archives[-1:]
    assert b"PK workbook" in archive and b"sales q1.xlsx" in archive
    for name in ("../escape", "a/b", ".."):
        with pytest.raises(HermesSandboxError) as invalid:
            await sandboxes.write_input(_session(1), name, b"x")
        assert invalid.value.code == "hermes_input_invalid"


@pytest.mark.asyncio
async def test_platform_skills_are_staged_then_swapped_in_by_the_relay(make, monkeypatch) -> None:
    import hashlib as _hashlib
    import io as _io
    import zipfile as _zipfile

    from src.hermes_platform_skills import PlatformSkill

    sandboxes, daemon, _, _ = make()
    await _create(sandboxes, 1)
    buffer = _io.BytesIO()
    with _zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("SKILL.md", "---\nname: standup\n---\n")
    bundle = buffer.getvalue()
    commands: list[list[str]] = []

    async def exec_(sandbox: Any, cmd: list[str]) -> tuple[int, bytes]:
        commands.append(cmd)
        return 0, b'{"type":"skills-hydrated","count":1}'

    async def fetch(key: str) -> bytes:
        return bundle

    monkeypatch.setattr(sandboxes, "_exec", exec_)
    skill = PlatformSkill("standup-1a", "skills/k.zip", _hashlib.sha256(bundle).hexdigest())

    counts = await sandboxes.hydrate_skills(_session(1), [skill], fetch)

    assert counts == {"hydrated": 1, "skipped": 0}
    assert commands[0][:2] == ["hermes-relay", "skills-hydrate"]
    assert commands[0][2].startswith("/workspace/.aurum/hydrate/")
    [(_, archive)] = daemon.container_map[resource_name(_session(1))].archives[-1:]
    assert b"name: standup" in archive

    async def failed(sandbox: Any, cmd: list[str]) -> tuple[int, bytes]:
        return 3, b'{"type":"error","code":"hermes_skills_staging_invalid"}'

    monkeypatch.setattr(sandboxes, "_exec", failed)
    with pytest.raises(HermesSandboxError) as refused:
        await sandboxes.hydrate_skills(_session(1), [], fetch)
    assert refused.value.code == "hermes_skills_failed"
