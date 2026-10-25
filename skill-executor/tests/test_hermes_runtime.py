"""Hermes sandboxes on a fake Docker daemon: isolation settings, secrets, adoption and reaping."""

from __future__ import annotations

import io
import json
import tarfile
import time
from typing import Any

import pytest
from aiodocker.exceptions import DockerError
from fastapi.testclient import TestClient

from src import hermes_routes
from src.config import Settings
from src.hermes_runtime import (
    CREATED_LABEL,
    LABEL,
    SESSION_LABEL,
    DockerHermesSandboxes,
    HermesSandboxError,
    bounded_json_lines,
    hermes_config,
    resource_name,
)

SESSION = "a" * 32
TOKEN = "hermes-session-token-0123456789"


def _error(status: int, message: str) -> DockerError:
    return DockerError(status, {"message": message})


class FakeContainer:
    def __init__(self, name: str, config: dict[str, Any] | None = None) -> None:
        self.id = f"id-{name}"
        self.name = name
        self.config = config or {}
        self._container = {"Labels": (config or {}).get("Labels", {})}
        self.archives: list[tuple[str, bytes]] = []
        self.started = False
        self.deleted = False

    async def put_archive(self, path: str, data: bytes) -> None:
        self.archives.append((path, data))

    async def start(self) -> None:
        self.started = True

    async def delete(self, force: bool = False) -> None:
        self.deleted = True


class FakeNetwork:
    def __init__(self, daemon: FakeDocker, name: str, config: dict[str, Any]) -> None:
        self.daemon, self.name, self.config = daemon, name, config
        self.members: dict[str, dict[str, Any]] = {}

    async def show(self) -> dict[str, Any]:
        return {"Containers": self.members}

    async def connect(self, config: dict[str, Any]) -> None:
        self.members[config["Container"]] = config["EndpointConfig"]

    async def disconnect(self, config: dict[str, Any]) -> None:
        self.members.pop(f"id-{config['Container']}", None)

    async def delete(self) -> bool:
        self.daemon.network_map.pop(self.name, None)
        return True


class FakeVolume:
    def __init__(self, daemon: FakeDocker, name: str) -> None:
        self.daemon, self.name = daemon, name

    async def delete(self, force: bool = False) -> None:
        self.daemon.volume_map.pop(self.name, None)


class FakeDocker:
    def __init__(self) -> None:
        self.container_map: dict[str, FakeContainer] = {"aurum-ai-hermes-egress-guard": FakeContainer("aurum-ai-hermes-egress-guard")}
        self.network_map: dict[str, FakeNetwork] = {}
        self.volume_map: dict[str, dict[str, Any]] = {}
        self.overlaps_left = 0
        self.api_version = "1.56"
        outer = self

        class Containers:
            async def create(self, config: dict[str, Any], name: str) -> FakeContainer:
                container = FakeContainer(name, config)
                outer.container_map[name] = container
                return container

            async def get(self, ref: str) -> FakeContainer:
                for container in outer.container_map.values():
                    if ref in (container.name, container.id) and not container.deleted:
                        return container
                raise _error(404, "no such container")

            async def list(self, all: bool = False, filters: str = "") -> list[FakeContainer]:
                return [c for c in outer.container_map.values() if c._container["Labels"].get(LABEL) == "1" and not c.deleted]

        class Networks:
            async def list(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
                return [
                    {"Name": n.name, "Labels": n.config.get("Labels", {}), "IPAM": n.config.get("IPAM", {})}
                    for n in outer.network_map.values()
                ]

            async def create(self, config: dict[str, Any]) -> FakeNetwork:
                if outer.overlaps_left:
                    outer.overlaps_left -= 1
                    raise _error(400, "Pool overlaps with other one on this address space")
                network = FakeNetwork(outer, config["Name"], config)
                outer.network_map[config["Name"]] = network
                return network

            async def get(self, name: str) -> FakeNetwork:
                if name not in outer.network_map:
                    raise _error(404, "no such network")
                return outer.network_map[name]

        class Volumes:
            async def create(self, config: dict[str, Any]) -> None:
                outer.volume_map[config["Name"]] = config

            async def get(self, name: str) -> FakeVolume:
                if name not in outer.volume_map:
                    raise _error(404, "no such volume")
                return FakeVolume(outer, name)

            async def list(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
                return {"Volumes": [{"Name": n, "Labels": c.get("Labels", {})} for n, c in outer.volume_map.items()]}

        self.containers, self.networks, self.volumes = Containers(), Networks(), Volumes()

    async def version(self) -> dict[str, str]:
        return {"ApiVersion": self.api_version}


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> tuple[DockerHermesSandboxes, FakeDocker]:
    sandboxes = DockerHermesSandboxes(Settings(hermes_enabled=True))
    daemon = FakeDocker()
    sandboxes._docker = daemon  # type: ignore[assignment]
    sandboxes._ready = True

    async def healthy(sandbox: Any) -> None:
        return None

    monkeypatch.setattr(sandboxes, "_wait_healthy", healthy)
    return sandboxes, daemon


async def _create(sandboxes: DockerHermesSandboxes, session: str = SESSION) -> Any:
    return await sandboxes.create(session_id=session, org_id="org-1", conversation_id="conv-1", token=TOKEN, model="m")


def _files(archive: bytes) -> dict[str, tarfile.TarInfo | bytes]:
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        return {
            member.name: (tar.extractfile(member).read() if member.isfile() else member)  # type: ignore[union-attr]
            for member in tar.getmembers()
        }


@pytest.mark.asyncio
async def test_a_sandbox_gets_its_own_isolated_network_and_a_hardened_container(runtime) -> None:
    sandboxes, daemon = runtime
    sandbox, created = await _create(sandboxes)

    name = resource_name(SESSION)
    network = daemon.network_map[name]
    assert created and sandbox.network == name
    assert network.config["Internal"] is True
    assert network.config["Options"] == {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"}
    assert network.config["IPAM"]["Config"][0]["Subnet"].endswith("/28")
    assert network.members == {"id-aurum-ai-hermes-egress-guard": {"Aliases": ["hermes-egress"]}}
    host = daemon.container_map[name].config["HostConfig"]
    assert host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges"]
    assert host["ReadonlyRootfs"] is True
    assert host["NetworkMode"] == name
    assert host["Mounts"] == [{"Type": "volume", "Source": name, "Target": "/workspace"}]
    assert host["PidsLimit"] == 512 and host["Memory"] == 2 * 1024**3
    assert daemon.container_map[name].config["User"] == "10001:10001"


@pytest.mark.asyncio
async def test_secrets_reach_the_workspace_only(runtime) -> None:
    sandboxes, daemon = runtime
    await _create(sandboxes)

    container = daemon.container_map[resource_name(SESSION)]
    assert TOKEN not in json.dumps(container.config)
    [(path, archive)] = container.archives
    files = _files(archive)
    config = json.loads(files[".hermes/config.yaml"])  # type: ignore[arg-type]
    assert path == "/workspace" and container.started
    assert config["model"]["api_key"] == TOKEN
    assert config["model"]["base_url"] == "http://hermes-egress:8080/v1/hermes"
    assert config["platforms"]["api_server"]["extra"]["host"] == "127.0.0.1"
    assert len(config["platforms"]["api_server"]["extra"]["key"]) >= 32
    assert files[".aurum/proxy"] == f"http://hermes:{TOKEN}@hermes-egress:3128".encode()
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            assert (member.uid, member.gid) == (10001, 10001)
            assert member.mode == (0o700 if member.isdir() else 0o600)


def test_generated_config_disables_unneeded_toolsets() -> None:
    config = hermes_config(Settings(), model="m", token=TOKEN, api_key="k")

    assert config["approvals"] == {"mode": "off"}
    assert {"web", "browser", "cronjob", "clarify", "computer_use"} <= set(config["agent"]["disabled_toolsets"])
    assert config["terminal"] == {"backend": "local"}


@pytest.mark.asyncio
async def test_subnets_skip_used_ranges_and_retry_overlaps(runtime) -> None:
    sandboxes, daemon = runtime
    await _create(sandboxes, "b" * 32)
    daemon.overlaps_left = 2
    await _create(sandboxes, "c" * 32)

    subnets = [n.config["IPAM"]["Config"][0]["Subnet"] for n in daemon.network_map.values()]
    assert len(set(subnets)) == 2


@pytest.mark.asyncio
async def test_an_exhausted_pool_refuses(runtime) -> None:
    sandboxes, daemon = runtime
    sandboxes._settings = Settings(hermes_enabled=True, hermes_subnet_pool="172.31.0.0/28", hermes_subnet_prefix=28)
    await _create(sandboxes, "b" * 32)

    with pytest.raises(HermesSandboxError, match="hermes_subnet_pool_exhausted"):
        await _create(sandboxes, "c" * 32)
    assert resource_name("c" * 32) not in daemon.container_map


@pytest.mark.asyncio
async def test_creating_again_returns_the_same_sandbox_but_never_another_owners(runtime) -> None:
    sandboxes, _ = runtime
    first, _ = await _create(sandboxes)
    again, created = await _create(sandboxes)

    assert again == first and created is False
    with pytest.raises(HermesSandboxError, match="owner_mismatch"):
        await sandboxes.create(session_id=SESSION, org_id="org-2", conversation_id="conv-1", token=TOKEN, model="m")


@pytest.mark.asyncio
async def test_a_restarted_executor_adopts_live_sandboxes(runtime) -> None:
    sandboxes, daemon = runtime
    await _create(sandboxes)
    fresh = DockerHermesSandboxes(Settings(hermes_enabled=True))
    fresh._docker = daemon  # type: ignore[assignment]

    adopted = await fresh.adopt()

    sandbox = fresh.get(SESSION)
    assert adopted == 1
    assert (sandbox.org_id, sandbox.conversation_id) == ("org-1", "conv-1")
    assert not daemon.container_map[resource_name(SESSION)].deleted


@pytest.mark.asyncio
async def test_reconcile_reconnects_a_recreated_guard(runtime) -> None:
    sandboxes, daemon = runtime
    await _create(sandboxes)
    daemon.network_map[resource_name(SESSION)].members.clear()

    await sandboxes.reconcile()

    assert daemon.network_map[resource_name(SESSION)].members


@pytest.mark.asyncio
async def test_reaper_removes_expired_sandboxes_and_old_leftovers(runtime) -> None:
    sandboxes, daemon = runtime
    await _create(sandboxes)
    sandbox = sandboxes.get(SESSION)
    sandboxes._sandboxes[SESSION] = type(sandbox)(**{**sandbox.__dict__, "last_active": time.time() - 4000})
    old = str(int(time.time() - 4000))
    leftover = resource_name("d" * 32)
    daemon.network_map[leftover] = FakeNetwork(daemon, leftover, {"Labels": {LABEL: "1", CREATED_LABEL: old}})
    daemon.volume_map[leftover] = {"Labels": {LABEL: "1", CREATED_LABEL: old}}
    recent = resource_name("e" * 32)
    daemon.volume_map[recent] = {"Labels": {LABEL: "1", CREATED_LABEL: str(int(time.time()))}}

    reaped = await sandboxes.reap()

    assert reaped == 1
    assert daemon.container_map[resource_name(SESSION)].deleted
    assert resource_name(SESSION) not in daemon.network_map and resource_name(SESSION) not in daemon.volume_map
    assert leftover not in daemon.network_map and leftover not in daemon.volume_map
    assert recent in daemon.volume_map
    with pytest.raises(HermesSandboxError):
        sandboxes.get(SESSION)


@pytest.mark.asyncio
async def test_old_docker_is_refused(runtime) -> None:
    sandboxes, daemon = runtime
    daemon.api_version = "1.47"

    with pytest.raises(HermesSandboxError, match="docker_too_old"):
        await sandboxes.ready()


def test_relay_lines_are_bounded_json_objects() -> None:
    lines, rest = bounded_json_lines(
        b'{"type":"event","event":{}}\nnot json\n[1]\n{"no":"type"}\n' + b'{"type":"x","v":"' + b"a" * 100 + b'"}\n{"type":"part',
        64,
    )

    assert lines == [{"type": "event", "event": {}}]
    assert rest == b'{"type":"part'
    assert bounded_json_lines(b"x" * 100, 64) == ([], b"")


def test_routes_are_absent_while_hermes_is_off_and_refused_on_kubernetes(monkeypatch) -> None:
    from src.main import app

    client = TestClient(app)
    monkeypatch.setattr(hermes_routes, "settings", Settings(hermes_enabled=False))
    assert client.get(f"/hermes/sandboxes/{SESSION}").status_code == 404
    monkeypatch.setattr(hermes_routes, "settings", Settings(hermes_enabled=True, runtime="kubernetes"))
    assert client.get(f"/hermes/sandboxes/{SESSION}").status_code == 501


def test_create_request_validates_the_session_id() -> None:
    from src.main import app

    client = TestClient(app)
    response = client.post(
        "/hermes/sandboxes",
        json={"session_id": "../etc", "org_id": "o", "conversation_id": "c", "token": TOKEN, "model": "m"},
    )
    assert response.status_code == 422
    assert SESSION_LABEL


@pytest.mark.asyncio
async def test_an_unadopted_live_sandbox_is_never_reaped_as_a_leftover(runtime) -> None:
    sandboxes, daemon = runtime
    await _create(sandboxes)
    name = resource_name(SESSION)
    old = str(int(time.time() - 4000))
    daemon.container_map[name]._container["Labels"][CREATED_LABEL] = old
    daemon.network_map[name].config["Labels"][CREATED_LABEL] = old
    daemon.volume_map[name]["Labels"][CREATED_LABEL] = old
    fresh = DockerHermesSandboxes(Settings(hermes_enabled=True))
    fresh._docker = daemon  # type: ignore[assignment]

    await fresh._reap_leftovers(time.time())

    assert not daemon.container_map[name].deleted
    assert name in daemon.network_map and name in daemon.volume_map


@pytest.mark.asyncio
async def test_creation_is_refused_until_the_daemon_passes_the_checks(monkeypatch) -> None:
    sandboxes = DockerHermesSandboxes(Settings(hermes_enabled=True))
    daemon = FakeDocker()
    daemon.api_version = "1.47"
    sandboxes._docker = daemon  # type: ignore[assignment]

    with pytest.raises(HermesSandboxError, match="docker_too_old"):
        await _create(sandboxes)
    assert daemon.network_map == {} and daemon.volume_map == {}
    del daemon.container_map["aurum-ai-hermes-egress-guard"]
    daemon.api_version = "1.56"
    with pytest.raises(HermesSandboxError, match="guard_missing"):
        await _create(sandboxes)
