"""Long-lived Hermes Agent sandboxes on Docker (docs/HERMES_AGENT.md, phase 2c).

A sandbox belongs to one Hermes session and outlives turns. Each gets:

* its own internal network, carved from a subnet pool, in isolated gateway mode
  (the host bridge has no address), joined only by the hermes-egress guard under
  the alias `hermes-egress`, so sandboxes cannot reach each other or the host;
* a workspace volume for Hermes's state, the agent's files and `pip --user`
  packages;
* a container with no capabilities, no new privileges, a read-only root
  filesystem and CPU, memory and PID limits.

The platform-generated config (session token, API server key) is placed in the
workspace before the container starts and never put in its environment or
labels. skill-executor talks to Hermes only through `docker exec` of
`hermes-relay`, whose output is untrusted: each line is size-bounded and must be
a JSON object.

Ownership is in Docker labels, so a restarted executor re-adopts live sandboxes
instead of reaping them. A sandbox past its idle or absolute lifetime is
retired: stopped, its workspace snapshotted (src/hermes_snapshots.py), then
removed; the session's next create restores the snapshot into a fresh sandbox.
A sandbox whose snapshot fails stays stopped with its volume, is retried, and
can be resumed, and is removed only when the snapshot retention runs out. The
reaper also removes networks or volumes left without a sandbox.

Capacity is capped operator-wide and per organization: at a cap the least
recently used idle sandbox (no open stream, and no run its relay reports as
active) is retired to make room. The workspace is either a Docker volume with a
soft size limit, checked from Docker's own volume usage (the agent cannot hide
files from it), or a size-bounded tmpfs volume (see Settings.hermes_workspace_mode).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import json
import logging
import secrets
import tarfile
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import aiodocker
from aiodocker.exceptions import DockerError

from src.config import Settings
from src.hermes_platform_skills import PlatformSkill, staging_tar
from src.hermes_snapshots import Snapshot, SnapshotError, SnapshotStore, to_thread_sanitize
from src.hermes_snapshots import prefix as snapshot_prefix

logger = logging.getLogger(__name__)

LABEL = "aurum-ai.hermes-sandbox"
SESSION_LABEL = "aurum-ai.hermes-session"
ORG_LABEL = "aurum-ai.hermes-org"
CONVERSATION_LABEL = "aurum-ai.hermes-conversation"
CREATED_LABEL = "aurum-ai.hermes-created"
SANDBOX_UID = 10001
# Isolated gateway mode for internal bridge networks arrived in Docker 28.
MIN_DOCKER_API = (1, 48)
DISABLED_TOOLSETS = (
    "browser", "computer_use", "cronjob", "discord", "discord_admin",
    "feishu_doc", "feishu_drive", "homeassistant", "image_gen", "kanban", "search",
    "spotify", "tts", "video", "video_gen", "web", "x_search", "yuanbao",
    # Hermes 0.22: remote connectors through Nous's tool gateway.
    "connections",
)
# The platform tools of the MCP bridge (business-backend's hermes_mcp).
PLATFORM_TOOLS = (
    "knowledge_search", "web_search", "web_read", "generate_image", "edit_image", "list_skills", "run_skill",
)
_CHUNK = 64 * 1024
# Written by the platform into every workspace; a snapshot source without it
# lost its workspace (see DockerHermesSandboxes._retire).
WORKSPACE_MARKER = ".aurum/proxy"
Outcome = Literal["existing", "created", "restored", "resumed"]


class HermesSandboxError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class HermesSandboxDropped(HermesSandboxError):
    """Retiring removed the sandbox without a snapshot; nothing is kept."""


@dataclass(frozen=True)
class HermesSandbox:
    session_id: str
    org_id: str
    conversation_id: str
    container_id: str
    network: str
    volume: str
    created_at: float
    last_active: float = field(default_factory=time.time)
    # "retiring" while it is being stopped and snapshotted.
    state: Literal["running", "stopped", "retiring"] = "running"
    stopped_at: float | None = None
    snapshot_attempt_at: float = 0.0
    # Over the soft workspace limit: no new runs until it is back under.
    workspace_full: bool = False


# hermes-relay's skills-read returns at most this many.
SKILLS_MAX = 16
# Hermes keeps the external directories it found when it started: this one
# exists from the start, with the config, and only its entries change.
PLATFORM_SKILLS_DIR = "/workspace/.aurum/platform-skills"


def resource_name(session_id: str) -> str:
    return f"aurum-ai-hermes-{session_id}"


def hermes_config(settings: Settings, *, model: str, token: str, api_key: str) -> dict[str, Any]:
    """Hermes config.yaml, as JSON (which YAML reads). The agent can edit the
    file, so no platform control depends on its contents."""
    return {
        "model": {
            "provider": "custom",
            "base_url": settings.hermes_platform_url,
            "default": model,
            "api_key": token,
        },
        "terminal": {"backend": "local"},
        # The sandbox is the boundary; Hermes's hardline patterns still apply.
        "approvals": {"mode": "off"},
        # Questions to the user go through the platform's question cards, which
        # expire after an hour; the platform answers "skip" then, so Hermes's
        # own wait is longer.
        "agent": {"disabled_toolsets": list(DISABLED_TOOLSETS), "clarify_timeout": 4200},
        # The API server's toolset leaves clarify out.
        "platform_toolsets": {"api_server": ["hermes-api-server", "clarify"]},
        # The platform tools stay listed by name instead of behind the
        # tool_search bridge; Nous's connector gateway and update checks are off.
        "tools": {"tool_search": {"enabled": "off"}, "connectors": {"enabled": False}},
        "updates": {"check": False},
        # The built-in store only, within the limits the platform keeps the
        # user's memory to (business-backend's hermes_memories).
        "memory": {
            "memory_enabled": True,
            "user_profile_enabled": True,
            "write_approval": False,
            "memory_char_limit": 2200,
            "user_char_limit": 1375,
            "provider": "",
        },
        # Skills land in HERMES_HOME/skills unstaged, where the platform reads
        # them after every turn as drafts for an administrator
        # (business-backend's hermes_skill_drafts); Hermes's own approval is
        # no gate, since the terminal writes there too.
        # The platform's code skills the user may read are hydrated into an
        # external directory before each turn (src/hermes_platform_skills.py).
        "skills": {"write_approval": False, "create_dir": "", "external_dirs": [PLATFORM_SKILLS_DIR]},
        # Hermes connects when it starts and lists the tools once. The bridge
        # answers POST only, so there is no preflight probe, and a rare ping
        # keeps idle sandboxes from calling the backend between turns.
        "mcp_servers": {
            "aurum": {
                "url": f"{settings.hermes_platform_url}/mcp",
                "headers": {"Authorization": f"Bearer {token}"},
                "skip_preflight": True,
                "timeout": 600,
                "connect_timeout": 30,
                "keepalive_interval": 1800,
                "sampling": {"enabled": False},
                "tools": {"include": list(PLATFORM_TOOLS), "resources": False, "prompts": False},
            }
        },
        "platforms": {
            "api_server": {
                "enabled": True,
                "extra": {"host": "127.0.0.1", "port": 8642, "key": api_key},
            }
        },
    }


def _tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        directories = sorted({path.rsplit("/", 1)[0] for path in files if "/" in path})
        for directory in directories:
            info = tarfile.TarInfo(directory)
            info.type, info.mode, info.uid, info.gid = tarfile.DIRTYPE, 0o700, SANDBOX_UID, SANDBOX_UID
            archive.addfile(info)
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size, info.mode, info.uid, info.gid = len(data), 0o600, SANDBOX_UID, SANDBOX_UID
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def bounded_json_lines(data: bytes, limit: int) -> tuple[list[dict[str, Any]], bytes]:
    """Complete relay lines parsed as JSON objects, and the unfinished rest."""
    *lines, rest = data.split(b"\n")
    parsed: list[dict[str, Any]] = []
    for line in lines:
        if not line or len(line) > limit:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("type"), str):
            parsed.append(value)
    if len(rest) > limit:
        rest = b""
    return parsed, rest


class DockerHermesSandboxes:
    def __init__(self, settings: Settings, store: SnapshotStore | None = None) -> None:
        self._settings = settings
        self._store = store or SnapshotStore(settings)
        self._docker: aiodocker.Docker | None = None
        self._sandboxes: dict[str, HermesSandbox] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._capacity_lock = asyncio.Lock()
        # Admitted creations not yet in _sandboxes, per organization.
        self._reserved: dict[str, int] = {}
        self._streams: dict[str, int] = {}
        self._ready = False
        self._swept_at = 0.0
        self._disk_checked_at = 0.0

    @property
    def _tmpfs(self) -> bool:
        return self._settings.hermes_workspace_mode == "tmpfs"

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    @property
    def docker(self) -> aiodocker.Docker:
        if self._docker is None:
            self._docker = aiodocker.Docker()
        return self._docker

    async def shutdown(self) -> None:
        if self._docker is not None:
            await self._docker.close()
            self._docker = None

    # Readiness and adoption -------------------------------------------------

    async def ready(self) -> None:
        """Refuse to run on a daemon that could give a sandbox network a host
        address, or without the guard; creation is refused until this passes."""
        version = await self.docker.version()
        api = tuple(int(part) for part in str(version.get("ApiVersion", "0.0")).split(".")[:2])
        if api < MIN_DOCKER_API:
            raise HermesSandboxError("docker_too_old_for_isolated_networks")
        try:
            await self.docker.containers.get(self._settings.hermes_guard_container)
        except DockerError:
            raise HermesSandboxError("hermes_egress_guard_missing") from None
        self._ready = True

    async def adopt(self) -> int:
        """Rebuild the session map from labels after a restart."""
        containers = await self.docker.containers.list(
            all=True, filters=json.dumps({"label": [f"{LABEL}=1"]})
        )
        adopted = 0
        for container in containers:
            labels = container._container.get("Labels") or {}
            session_id = labels.get(SESSION_LABEL, "")
            if not session_id or session_id in self._sandboxes:
                continue
            running = container._container.get("State") in ("running", "restarting")
            self._sandboxes[session_id] = self._from_labels(container.id, labels, running)
            adopted += 1
        return adopted

    def _from_labels(self, container_id: str, labels: dict[str, str], running: bool = True) -> HermesSandbox:
        session_id = labels[SESSION_LABEL]
        return HermesSandbox(
            session_id=session_id,
            org_id=labels.get(ORG_LABEL, ""),
            conversation_id=labels.get(CONVERSATION_LABEL, ""),
            container_id=container_id,
            network=resource_name(session_id),
            volume=resource_name(session_id),
            created_at=float(labels.get(CREATED_LABEL, "0") or 0),
            state="running" if running else "stopped",
            # Counted from adoption: a restart extends a stopped sandbox's retention.
            stopped_at=None if running else time.time(),
        )

    def get(self, session_id: str) -> HermesSandbox:
        sandbox = self._sandboxes.get(session_id)
        if sandbox is None:
            raise HermesSandboxError("hermes_sandbox_not_found")
        return sandbox

    def get_running(self, session_id: str) -> HermesSandbox:
        sandbox = self.get(session_id)
        if sandbox.state != "running":
            raise HermesSandboxError("hermes_sandbox_stopped")
        return sandbox

    def touch(self, session_id: str) -> None:
        sandbox = self._sandboxes.get(session_id)
        if sandbox is not None:
            self._sandboxes[session_id] = replace(sandbox, last_active=time.time())

    # Creation ---------------------------------------------------------------

    def _labels(self, session_id: str, org_id: str, conversation_id: str, created: float) -> dict[str, str]:
        return {
            LABEL: "1",
            SESSION_LABEL: session_id,
            ORG_LABEL: org_id,
            CONVERSATION_LABEL: conversation_id,
            CREATED_LABEL: str(int(created)),
        }

    async def _used_subnets(self) -> set[ipaddress.IPv4Network]:
        used: set[ipaddress.IPv4Network] = set()
        for network in await self.docker.networks.list():
            for config in (network.get("IPAM") or {}).get("Config") or []:
                try:
                    used.add(ipaddress.ip_network(config.get("Subnet", ""), strict=False))  # type: ignore[arg-type]
                except ValueError:
                    continue
        return used

    async def _create_network(self, name: str, labels: dict[str, str]) -> None:
        pool = ipaddress.ip_network(self._settings.hermes_subnet_pool)
        for _attempt in range(5):
            used = await self._used_subnets()
            candidate = next(
                (subnet for subnet in pool.subnets(new_prefix=self._settings.hermes_subnet_prefix)
                 if not any(subnet.overlaps(other) for other in used)),
                None,
            )
            if candidate is None:
                raise HermesSandboxError("hermes_subnet_pool_exhausted")
            try:
                await self.docker.networks.create(
                    {
                        "Name": name,
                        "Driver": "bridge",
                        "Internal": True,
                        "IPAM": {"Config": [{"Subnet": str(candidate)}]},
                        "Options": {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"},
                        "Labels": labels,
                    }
                )
                return
            except DockerError as exc:
                # Another create took the same subnet; look again.
                if exc.status in (400, 403, 409) and "overlap" in str(exc).lower():
                    continue
                raise
        raise HermesSandboxError("hermes_subnet_allocation_failed")

    async def _connect_guard(self, network_name: str) -> None:
        network = await self.docker.networks.get(network_name)
        info = await network.show()
        guard = await self.docker.containers.get(self._settings.hermes_guard_container)
        if guard.id in (info.get("Containers") or {}):
            return
        await network.connect(
            {"Container": guard.id, "EndpointConfig": {"Aliases": ["hermes-egress"]}}
        )

    def _volume_config(self, name: str, labels: dict[str, str]) -> dict[str, Any]:
        config: dict[str, Any] = {"Name": name, "Labels": labels}
        if self._tmpfs:
            size = self._settings.hermes_workspace_max_bytes
            config["DriverOpts"] = {
                "type": "tmpfs",
                "device": "tmpfs",
                "o": f"size={size},uid={SANDBOX_UID},gid={SANDBOX_UID},mode=0700",
            }
        return config

    def _container_config(self, name: str, labels: dict[str, str]) -> dict[str, Any]:
        memory = self._settings.hermes_memory_bytes
        if self._tmpfs:
            # tmpfs pages are charged to the container that writes them.
            memory += self._settings.hermes_workspace_max_bytes
        return {
            "Image": self._settings.hermes_sandbox_image,
            "User": f"{SANDBOX_UID}:{SANDBOX_UID}",
            "Labels": labels,
            "HostConfig": {
                "Init": True,
                "NetworkMode": name,
                "ReadonlyRootfs": True,
                "Tmpfs": {"/tmp": f"rw,nosuid,nodev,size={self._settings.hermes_tmp_mb}m"},
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "Memory": memory,
                "NanoCpus": int(self._settings.hermes_cpus * 1_000_000_000),
                "PidsLimit": self._settings.hermes_pids_limit,
                "Mounts": [{"Type": "volume", "Source": name, "Target": "/workspace"}],
                "RestartPolicy": {"Name": "unless-stopped"},
            },
        }

    async def create(
        self,
        *,
        session_id: str,
        org_id: str,
        conversation_id: str,
        token: str,
        model: str,
        snapshot_sha256: str | None = None,
        restore: bool = True,
    ) -> tuple[HermesSandbox, Outcome]:
        """The session's sandbox: the running one, a stopped one resumed, or a
        new one, restored from `snapshot_sha256` or else the latest snapshot."""
        if not self._ready:
            await self.ready()
        async with self._lock(session_id):
            existing = self._sandboxes.get(session_id)
            if existing is not None:
                if (existing.org_id, existing.conversation_id) != (org_id, conversation_id):
                    raise HermesSandboxError("hermes_sandbox_owner_mismatch")
                if existing.state == "running":
                    return existing, "existing"
                # A paused tmpfs sandbox would keep its old token: never resumed.
                fresh = time.time() - existing.created_at < self._settings.hermes_max_lifetime_seconds
                if fresh and not self._tmpfs:
                    await self._admit(org_id, session_id)
                    try:
                        return await self._resume(existing, token=token, model=model), "resumed"
                    finally:
                        self._release(org_id)
                # Otherwise a snapshot and a fresh sandbox, as for a running one.
                try:
                    await self._retire(existing)
                except HermesSandboxDropped:
                    pass
            if snapshot_sha256 is not None and not self._store.enabled:
                raise HermesSandboxError("hermes_snapshot_unavailable")
            # Before admission: a store that is down must not evict anyone.
            sha = snapshot_sha256
            if sha is None and restore and self._store.enabled:
                try:
                    sha = await self._store.latest(org_id, conversation_id)
                except SnapshotError as exc:
                    raise HermesSandboxError(exc.code) from None
            await self._admit(org_id, session_id)
            try:
                sandbox, restored = await self._provision(
                    session_id=session_id,
                    org_id=org_id,
                    conversation_id=conversation_id,
                    token=token,
                    model=model,
                    snapshot_sha256=sha,
                )
            finally:
                self._release(org_id)
            try:
                await self._wait_healthy(sandbox)
            except Exception:
                self._sandboxes.pop(session_id, None)
                await self._remove(sandbox.network)
                raise
        return sandbox, "restored" if restored else "created"

    # Capacity ---------------------------------------------------------------

    async def _admit(self, org_id: str, session_id: str) -> None:
        """Reserve a running slot, retiring least recently used idle sandboxes
        while a cap is reached. Called with the session's lock held; victims are
        running, so never a session that is itself being created."""
        settings = self._settings
        async with self._capacity_lock:
            while True:
                running = [
                    s for s in self._sandboxes.values() if s.state == "running" and s.session_id != session_id
                ]
                in_org = sum(1 for s in running if s.org_id == org_id) + self._reserved.get(org_id, 0)
                org_full = in_org >= settings.hermes_max_running_per_org
                all_full = len(running) + sum(self._reserved.values()) >= settings.hermes_max_running
                if not (org_full or all_full):
                    self._reserved[org_id] = self._reserved.get(org_id, 0) + 1
                    return
                pool = [s for s in running if s.org_id == org_id] if org_full else running
                victim = await self._idle_victim(pool)
                if victim is None:
                    raise HermesSandboxError("hermes_capacity_exhausted")
                logger.info("Retiring idle Hermes sandbox %s to make room", victim.session_id)
                async with self._lock(victim.session_id):
                    current = self._sandboxes.get(victim.session_id)
                    if current is not None and current.state == "running":
                        try:
                            await self._retire(current)
                        except HermesSandboxDropped:
                            pass
                        except HermesSandboxError as exc:
                            # Stopped (or paused) anyway, so the slot is free.
                            logger.warning("Hermes sandbox %s kept stopped: %s", victim.session_id, exc.code)

    def _release(self, org_id: str) -> None:
        left = self._reserved.get(org_id, 0) - 1
        if left > 0:
            self._reserved[org_id] = left
        else:
            self._reserved.pop(org_id, None)

    async def _idle_victim(self, pool: list[HermesSandbox]) -> HermesSandbox | None:
        for sandbox in sorted(pool, key=lambda s: s.last_active):
            if not self._streams.get(sandbox.session_id) and not await self._active_runs(sandbox):
                return sandbox
        return None

    async def _active_runs(self, sandbox: HermesSandbox) -> list[str]:
        """Runs the relay reports as queued or running. It is the sandbox's own
        word: a sandbox claiming to be busy still goes at its idle or absolute
        lifetime. A relay that does not answer reports none."""
        try:
            _, output = await self._exec(sandbox, ["hermes-relay", "active"])
        except DockerError:
            return []
        lines, _ = bounded_json_lines(output + b"\n", self._settings.hermes_event_line_max_bytes)
        for line in lines:
            run_ids = line.get("run_ids")
            if line["type"] == "active" and isinstance(run_ids, list):
                return [run_id for run_id in run_ids if isinstance(run_id, str) and len(run_id) <= 128]
        return []

    async def _secrets(self, container: Any, *, token: str, model: str) -> None:
        api_key = secrets.token_urlsafe(32)
        config = hermes_config(self._settings, model=model, token=token, api_key=api_key)
        proxy = f"http://hermes:{token}@{self._settings.hermes_proxy_address}"
        await container.put_archive(
            "/workspace",
            _tar(
                {
                    ".hermes/config.yaml": json.dumps(config, indent=1).encode(),
                    ".aurum/proxy": proxy.encode(),
                    ".aurum/platform-skills/.platform": b"",
                }
            ),
        )

    async def _provision(
        self,
        *,
        session_id: str,
        org_id: str,
        conversation_id: str,
        token: str,
        model: str,
        snapshot_sha256: str | None,
    ) -> tuple[HermesSandbox, bool]:
        name = resource_name(session_id)
        created = time.time()
        labels = self._labels(session_id, org_id, conversation_id, created)
        restored = False
        try:
            await self._create_network(name, labels)
            await self._connect_guard(name)
            await self.docker.volumes.create(self._volume_config(name, labels))  # type: ignore[no-untyped-call]
            container = await self.docker.containers.create(
                config=self._container_config(name, labels), name=name
            )
            sha = snapshot_sha256
            # A tmpfs volume is mounted only while the container runs, so its
            # files are written after the start; start.sh waits for them.
            if self._tmpfs:
                await container.start()
            if sha is not None:
                await self._restore(container, org_id, conversation_id, sha)
                restored = True
            # After the restore, so the platform's files are always the current ones.
            await self._secrets(container, token=token, model=model)
            if not self._tmpfs:
                await container.start()
        except SnapshotError as exc:
            await self._remove(name)
            raise HermesSandboxError(exc.code) from None
        except TimeoutError:
            await self._remove(name)
            raise HermesSandboxError("hermes_snapshot_timeout") from None
        except Exception:
            await self._remove(name)
            raise
        sandbox = HermesSandbox(
            session_id=session_id,
            org_id=org_id,
            conversation_id=conversation_id,
            container_id=container.id,
            network=name,
            volume=name,
            created_at=created,
        )
        self._sandboxes[session_id] = sandbox
        return sandbox, restored

    async def _resume(self, sandbox: HermesSandbox, *, token: str, model: str) -> HermesSandbox:
        """Start a stopped sandbox again with a fresh token and API key."""
        try:
            container = await self.docker.containers.get(sandbox.container_id)
            await self._connect_guard(sandbox.network)
            await self._secrets(container, token=token, model=model)
            await container.start()
        except DockerError:
            raise HermesSandboxError("hermes_resume_failed") from None
        resumed = replace(sandbox, state="running", stopped_at=None, last_active=time.time())
        self._sandboxes[sandbox.session_id] = resumed
        try:
            await self._wait_healthy(resumed)
        except Exception:
            await self._stop_container(resumed)
            self._sandboxes[sandbox.session_id] = sandbox
            raise
        return resumed

    async def _wait_healthy(self, sandbox: HermesSandbox) -> None:
        deadline = time.monotonic() + self._settings.hermes_start_timeout_seconds
        while time.monotonic() < deadline:
            exit_code, _ = await self._exec(sandbox, ["hermes-relay", "health"])
            if exit_code == 0:
                return
            await asyncio.sleep(0.5)
        raise HermesSandboxError("hermes_start_timeout")

    # Exec relay -------------------------------------------------------------

    async def _exec(self, sandbox: HermesSandbox, cmd: list[str]) -> tuple[int | None, bytes]:
        container = await self.docker.containers.get(sandbox.container_id)
        exec_ = await container.exec(cmd=cmd, stdout=True, stderr=False, user=f"{SANDBOX_UID}")
        stream = exec_.start(detach=False)
        chunks: list[bytes] = []
        try:
            while (message := await stream.read_out()) is not None:
                if message.stream == 1 and sum(map(len, chunks)) < self._settings.hermes_event_line_max_bytes:
                    chunks.append(message.data)
        finally:
            await stream.close()
        return (await exec_.inspect()).get("ExitCode"), b"".join(chunks)

    async def _stream(self, sandbox: HermesSandbox, cmd: list[str]) -> AsyncIterator[dict[str, Any]]:
        container = await self.docker.containers.get(sandbox.container_id)
        exec_ = await container.exec(cmd=cmd, stdout=True, stderr=False, user=f"{SANDBOX_UID}")
        stream = exec_.start(detach=False)
        pending = b""
        session_id = sandbox.session_id
        self._streams[session_id] = self._streams.get(session_id, 0) + 1
        try:
            while (message := await stream.read_out()) is not None:
                if message.stream != 1:
                    continue
                self.touch(sandbox.session_id)
                lines, pending = bounded_json_lines(
                    pending + message.data, self._settings.hermes_event_line_max_bytes
                )
                for line in lines:
                    yield line
        finally:
            self._streams[session_id] -= 1
            if not self._streams[session_id]:
                del self._streams[session_id]
            await stream.close()

    async def run(self, session_id: str, request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        sandbox = self.get_running(session_id)
        if sandbox.workspace_full:
            raise HermesSandboxError("hermes_workspace_full")
        key = hashlib.sha256(str(request["key"]).encode()).hexdigest()
        container = await self.docker.containers.get(sandbox.container_id)
        await container.put_archive(  # type: ignore[no-untyped-call]
            "/workspace", _tar({f".aurum/requests/{key}.json": json.dumps(request).encode()})
        )
        self.touch(session_id)
        async for line in self._stream(sandbox, ["hermes-relay", "run", f"/workspace/.aurum/requests/{key}.json"]):
            yield line

    async def events(self, session_id: str, run_id: str) -> AsyncIterator[dict[str, Any]]:
        sandbox = self.get_running(session_id)
        async for line in self._stream(sandbox, ["hermes-relay", "events", run_id]):
            yield line

    async def stop(self, session_id: str, run_id: str) -> None:
        sandbox = self.get_running(session_id)
        await self._exec(sandbox, ["hermes-relay", "stop", run_id])

    async def write_memory(self, session_id: str, memory: dict[str, str]) -> None:
        """Write the user's memory files before a turn; through a file, as answers."""
        sandbox = self.get_running(session_id)
        name = secrets.token_hex(16)
        container = await self.docker.containers.get(sandbox.container_id)
        await container.put_archive(  # type: ignore[no-untyped-call]
            "/workspace", _tar({f".aurum/memory/{name}.json": json.dumps(memory).encode()})
        )
        _, output = await self._exec(sandbox, ["hermes-relay", "memory-write", f"/workspace/.aurum/memory/{name}.json"])
        if b'"memory-written"' not in output:
            raise HermesSandboxError("hermes_memory_failed")

    async def read_memory(self, session_id: str) -> dict[str, str | None]:
        """The memory files as the turn left them; None where one is not usable."""
        sandbox = self.get_running(session_id)
        _, output = await self._exec(sandbox, ["hermes-relay", "memory-read"])
        lines, _ = bounded_json_lines(output + b"\n", self._settings.hermes_event_line_max_bytes)
        found = next((line for line in lines if line.get("type") == "memory"), None)
        if found is None:
            raise HermesSandboxError("hermes_memory_failed")
        return {kind: value if isinstance(value, str) else None for kind, value in found.items() if kind in ("memory", "user")}

    async def read_skills(self, session_id: str) -> dict[str, Any]:
        """The skills the sandbox holds beyond the bundled ones, as the relay
        bounds them; one per line, since together they outgrow an exec's output."""
        sandbox = self.get_running(session_id)
        skills: list[dict[str, Any]] = []
        async for line in self._stream(sandbox, ["hermes-relay", "skills-read"]):
            if line.get("type") == "skills":
                return {"skills": skills, "skipped": int(line.get("skipped") or 0)}
            files = line.get("files")
            if (
                line.get("type") == "skill"
                and isinstance(line.get("path"), str)
                and isinstance(files, list)
                and len(skills) < SKILLS_MAX
                and all(isinstance(item, dict) and isinstance(item.get("path"), str) and isinstance(item.get("content"), str) for item in files)
            ):
                skills.append(
                    {"path": line["path"], "files": [{"path": item["path"], "content": item["content"]} for item in files]}
                )
        raise HermesSandboxError("hermes_skills_failed")

    async def hydrate_skills(
        self, session_id: str, skills: list[PlatformSkill], fetch: Any = None
    ) -> dict[str, int]:
        """Replace the sandbox's platform skills with these; an empty list
        removes them all, so a failure upstream can still revoke."""
        if fetch is None:
            from src.object_storage import object_storage

            fetch = object_storage.get_bytes
        sandbox = self.get_running(session_id)
        staging = f".aurum/hydrate/{secrets.token_hex(16)}"
        archive, hydrated, skipped = await staging_tar(skills, fetch, staging)
        container = await self.docker.containers.get(sandbox.container_id)
        await container.put_archive("/workspace", archive)  # type: ignore[no-untyped-call]
        _, output = await self._exec(sandbox, ["hermes-relay", "skills-hydrate", f"/workspace/{staging}"])
        if b'"skills-hydrated"' not in output:
            raise HermesSandboxError("hermes_skills_failed")
        return {"hydrated": hydrated, "skipped": skipped}

    async def clarify(self, session_id: str, run_id: str, body: dict[str, Any]) -> None:
        """Answer a run's pending question. The answer is the user's text, so it
        goes in a file, never on the command line."""
        sandbox = self.get_running(session_id)
        name = hashlib.sha256(f"{run_id}:{body['request_id']}".encode()).hexdigest()
        container = await self.docker.containers.get(sandbox.container_id)
        await container.put_archive(  # type: ignore[no-untyped-call]
            "/workspace", _tar({f".aurum/clarify/{name}.json": json.dumps(body).encode()})
        )
        _, output = await self._exec(
            sandbox, ["hermes-relay", "clarify", run_id, f"/workspace/.aurum/clarify/{name}.json"]
        )
        lines, _ = bounded_json_lines(output + b"\n", self._settings.hermes_event_line_max_bytes)
        status = next((line.get("status") for line in lines if line.get("type") == "clarify"), None)
        if status in (404, 409):
            raise HermesSandboxError("hermes_clarify_settled")
        if not isinstance(status, int) or status >= 300:
            raise HermesSandboxError("hermes_clarify_failed")

    # Snapshots ---------------------------------------------------------------

    def _archive_limit(self) -> int:
        # Tar headers and padding on top of the content bound.
        return self._settings.hermes_snapshot_max_bytes + 1536 * self._settings.hermes_snapshot_max_entries

    def _tempdir(self) -> tempfile.TemporaryDirectory[str]:
        return tempfile.TemporaryDirectory(prefix="hermes-snapshot-", dir=self._settings.hermes_snapshot_dir)

    async def _workspace_archive(self, container_id: str, path: Path) -> None:
        """Docker's archive of /workspace, written to `path` within the bound."""
        limit, size = self._archive_limit(), 0
        async with self.docker._query(
            f"containers/{container_id}/archive", method="GET", params={"path": "/workspace"}, timeout=None
        ) as response:
            with path.open("xb") as target:
                path.chmod(0o600)
                async for block in response.content.iter_chunked(_CHUNK):
                    size += len(block)
                    if size > limit:
                        raise SnapshotError("hermes_snapshot_too_large")
                    target.write(block)

    async def _snapshot(self, sandbox: HermesSandbox) -> Snapshot:
        try:
            async with asyncio.timeout(self._settings.hermes_snapshot_timeout_seconds):
                with self._tempdir() as directory:
                    raw, clean = Path(directory, "workspace.tar"), Path(directory, "snapshot.tar")
                    await self._workspace_archive(sandbox.container_id, raw)
                    snapshot = await to_thread_sanitize(
                        raw, clean, strip="workspace", settings=self._settings, marker=WORKSPACE_MARKER
                    )
                    raw.unlink()
                    await self._store.put(sandbox.org_id, sandbox.conversation_id, clean, snapshot, sandbox.session_id)
                    return snapshot
        except SnapshotError as exc:
            raise HermesSandboxError(exc.code) from None
        except TimeoutError:
            raise HermesSandboxError("hermes_snapshot_timeout") from None
        except (DockerError, OSError):
            raise HermesSandboxError("hermes_snapshot_failed") from None

    async def _restore(self, container: Any, org_id: str, conversation_id: str, sha256: str) -> None:
        """Verify the snapshot, rebuild it as a clean archive and unpack that."""
        async with asyncio.timeout(self._settings.hermes_snapshot_timeout_seconds):
            with self._tempdir() as directory:
                fetched, clean = Path(directory, "fetched.tar"), Path(directory, "restore.tar")
                await self._store.fetch(org_id, conversation_id, sha256, fetched, self._archive_limit())
                await to_thread_sanitize(fetched, clean, strip=None, settings=self._settings)
                fetched.unlink()
                with clean.open("rb") as archive:
                    await container.put_archive("/workspace", archive)

    async def snapshot(self, session_id: str) -> Snapshot:
        """Snapshot a sandbox in place; it keeps running."""
        if not self._store.enabled:
            raise HermesSandboxError("hermes_snapshot_unavailable")
        async with self._lock(session_id):
            return await self._snapshot(self.get(session_id))

    async def _stop_container(self, sandbox: HermesSandbox) -> None:
        """Stop it, or pause it on tmpfs, where stopping would lose the workspace."""
        try:
            container = await self.docker.containers.get(sandbox.container_id)
            if self._tmpfs:
                await container.pause()
            else:
                await container.stop(t=self._settings.hermes_stop_timeout_seconds)
        except DockerError:
            pass

    async def _retire(self, sandbox: HermesSandbox) -> Snapshot | None:
        """Stop, snapshot and remove; on a failed snapshot the sandbox stays
        stopped with its volume. A workspace over the snapshot bound is removed
        without one (it cannot shrink while stopped, and the last snapshot that
        fit stays the latest). Called with the session's lock held."""
        session_id = sandbox.session_id
        self._sandboxes[session_id] = replace(sandbox, state="retiring")
        await self._stop_container(sandbox)
        snapshot = None
        if self._store.enabled:
            try:
                snapshot = await self._snapshot(sandbox)
            except HermesSandboxError as exc:
                if exc.code not in ("hermes_workspace_lost", "hermes_snapshot_too_large"):
                    self._keep_stopped(sandbox)
                    raise
                # Lost: nothing of the platform's left (a tmpfs workspace after
                # a restart), and a snapshot would replace the last good one.
                logger.warning("Hermes sandbox %s removed without a snapshot: %s", session_id, exc.code)
                self._sandboxes.pop(session_id, None)
                await self._remove(sandbox.network)
                raise HermesSandboxDropped(exc.code) from None
            except Exception as exc:
                # Never left "retiring": the reaper would skip it for good.
                self._keep_stopped(sandbox)
                raise HermesSandboxError("hermes_snapshot_failed") from exc
        self._sandboxes.pop(session_id, None)
        await self._remove(sandbox.network)
        return snapshot

    def _keep_stopped(self, sandbox: HermesSandbox) -> None:
        now = time.time()
        self._sandboxes[sandbox.session_id] = replace(
            sandbox, state="stopped", stopped_at=sandbox.stopped_at or now, snapshot_attempt_at=now
        )

    # Removal and reaping ----------------------------------------------------

    async def close(self, session_id: str, *, snapshot: bool = False) -> Snapshot | None:
        """Idempotent; also removes what a crashed create left behind. With
        `snapshot`, a failed snapshot keeps the sandbox, stopped, except that
        one over the bound or without a workspace is removed anyway and
        raises HermesSandboxDropped."""
        async with self._lock(session_id):
            sandbox = self._sandboxes.get(session_id)
            if snapshot and sandbox is not None:
                if not self._store.enabled:
                    raise HermesSandboxError("hermes_snapshot_unavailable")
                return await self._retire(sandbox)
            self._sandboxes.pop(session_id, None)
            await self._remove(resource_name(session_id))
            return None

    async def _remove(self, name: str) -> None:
        try:
            container = await self.docker.containers.get(name)
            await container.delete(force=True)
        except DockerError:
            pass
        try:
            network = await self.docker.networks.get(name)
            try:
                await network.disconnect(
                    {"Container": self._settings.hermes_guard_container, "Force": True}
                )
            except DockerError:
                pass
            await network.delete()
        except DockerError:
            pass
        try:
            volume = await self.docker.volumes.get(name)  # type: ignore[no-untyped-call]
            await volume.delete()
        except DockerError:
            pass

    async def purge_conversation(self, org_id: str, conversation_id: str) -> dict[str, int]:
        """Remove a conversation's sandboxes, without a snapshot, then all its
        snapshots. Closing waits for a snapshot in progress, so none is written
        after the deletion. Idempotent."""
        try:
            snapshot_prefix(org_id, conversation_id)
        except SnapshotError as exc:
            raise HermesSandboxError(exc.code) from None
        owned = [
            sandbox.session_id
            for sandbox in list(self._sandboxes.values())
            if sandbox.org_id == org_id and sandbox.conversation_id == conversation_id
        ]
        for session_id in owned:
            await self.close(session_id)
        snapshots = 0
        if self._store.enabled:
            try:
                snapshots = await self._store.delete_conversation(org_id, conversation_id)
            except SnapshotError as exc:
                raise HermesSandboxError(exc.code) from None
        return {"sandboxes": len(owned), "snapshots": snapshots}

    async def reconcile(self) -> None:
        """Reconnect the guard (a recreated guard loses runtime connections)."""
        for sandbox in list(self._sandboxes.values()):
            try:
                await self._connect_guard(sandbox.network)
            except DockerError:
                logger.warning("could not reconnect hermes-egress to %s", sandbox.network, exc_info=True)

    def _expired(self, sandbox: HermesSandbox, now: float) -> bool:
        return (
            now - sandbox.created_at >= self._settings.hermes_max_lifetime_seconds
            or now - sandbox.last_active >= self._settings.hermes_idle_ttl_seconds
        )

    async def reap(self) -> int:
        """Retire expired sandboxes and retry stopped ones; returns how many went."""
        settings = self._settings
        reaped = 0
        for session_id in list(self._sandboxes):
            async with self._lock(session_id):
                sandbox = self._sandboxes.get(session_id)
                now = time.time()
                if sandbox is None or sandbox.state == "retiring":
                    continue
                if sandbox.state == "stopped":
                    if now - (sandbox.stopped_at or now) >= settings.hermes_snapshot_retention_seconds:
                        logger.warning("Removing Hermes sandbox %s: no snapshot within the retention", session_id)
                        self._sandboxes.pop(session_id, None)
                        await self._remove(sandbox.network)
                        reaped += 1
                        continue
                    if now - sandbox.snapshot_attempt_at < settings.hermes_snapshot_retry_seconds:
                        continue
                elif not self._expired(sandbox, now):
                    continue
                logger.info("Retiring Hermes sandbox %s", session_id)
                try:
                    await self._retire(sandbox)
                    reaped += 1
                except HermesSandboxDropped:
                    reaped += 1
                except HermesSandboxError as exc:
                    logger.warning("Hermes sandbox %s kept stopped: %s", session_id, exc.code)
        await self._reap_leftovers(time.time())
        await self._sweep_snapshots()
        await self._check_disk()
        return reaped

    async def _volume_usage(self) -> dict[str, int]:
        """Docker's own size of each volume (dockerd walks it as root)."""
        data = await self.docker._query_json("system/df", params={"type": "volume"})
        return {
            str(volume.get("Name")): int((volume.get("UsageData") or {}).get("Size", -1))
            for volume in data.get("Volumes") or []
        }

    async def _check_disk(self) -> None:
        """The soft workspace limit: over it, a sandbox's active runs are stopped
        and it gets no new ones until it is back under. A tmpfs workspace is
        bounded by the kernel instead."""
        now = time.time()
        if self._tmpfs or now - self._disk_checked_at < self._settings.hermes_disk_check_interval_seconds:
            return
        self._disk_checked_at = now
        try:
            usage = await self._volume_usage()
        except DockerError:
            logger.warning("Could not read Docker volume usage", exc_info=True)
            return
        limit = self._settings.hermes_workspace_max_bytes
        for session_id, sandbox in list(self._sandboxes.items()):
            if sandbox.state != "running":
                continue
            full = usage.get(sandbox.volume, -1) > limit
            if full != sandbox.workspace_full:
                current = self._sandboxes.get(session_id)
                if current is not None:
                    self._sandboxes[session_id] = replace(current, workspace_full=full)
            if full:
                logger.warning("Hermes sandbox %s is over its workspace limit", session_id)
                for run_id in await self._active_runs(sandbox):
                    await self._exec(sandbox, ["hermes-relay", "stop", run_id])

    async def _sweep_snapshots(self) -> None:
        now = time.time()
        if not self._store.enabled or now - self._swept_at < self._settings.hermes_snapshot_sweep_interval_seconds:
            return
        self._swept_at = now
        try:
            removed = await self._store.sweep(now - self._settings.hermes_snapshot_retention_seconds)
            if removed:
                logger.info("Removed %s Hermes snapshot objects past the retention", removed)
        except Exception:
            logger.warning("Hermes snapshot sweep failed", exc_info=True)

    async def _reap_leftovers(self, now: float) -> None:
        # A labelled container still running is a sandbox, adopted or not: only
        # networks and volumes whose container is gone are leftovers.
        known = {resource_name(session_id) for session_id in self._sandboxes}
        for container in await self.docker.containers.list(
            all=True, filters=json.dumps({"label": [f"{LABEL}=1"]})
        ):
            session_id = (container._container.get("Labels") or {}).get(SESSION_LABEL)
            if session_id:
                known.add(resource_name(session_id))
        grace = self._settings.hermes_orphan_grace_seconds
        for network in await self.docker.networks.list(filters={"label": [f"{LABEL}=1"]}):
            labels = network.get("Labels") or {}
            name = network.get("Name", "")
            if name not in known and now - float(labels.get(CREATED_LABEL, "0") or 0) >= grace:
                await self._remove(name)
        listed = await self.docker.volumes.list(filters={"label": [f"{LABEL}=1"]})  # type: ignore[no-untyped-call]
        volumes = listed.get("Volumes") or []
        for volume in volumes:
            labels = volume.get("Labels") or {}
            name = volume.get("Name", "")
            if name not in known and now - float(labels.get(CREATED_LABEL, "0") or 0) >= grace:
                await self._remove(name)

    async def run_reaper(self, interval: float) -> None:
        while True:
            try:
                await asyncio.sleep(interval)
                # Adopt first: a sandbox missed at startup must never look
                # like a leftover to the reap below.
                await self.adopt()
                await self.reconcile()
                await self.reap()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Hermes sandbox reaper iteration failed", exc_info=True)
