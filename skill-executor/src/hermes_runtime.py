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
instead of reaping them; a reaper removes sandboxes past their idle or absolute
lifetime and networks or volumes left without a sandbox.
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
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any

import aiodocker
from aiodocker.exceptions import DockerError

from src.config import Settings

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
    "browser", "clarify", "computer_use", "cronjob", "discord", "discord_admin",
    "feishu_doc", "feishu_drive", "homeassistant", "image_gen", "kanban", "search",
    "spotify", "tts", "video", "video_gen", "web", "x_search", "yuanbao",
)


class HermesSandboxError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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
        "agent": {"disabled_toolsets": list(DISABLED_TOOLSETS)},
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
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._docker: aiodocker.Docker | None = None
        self._sandboxes: dict[str, HermesSandbox] = {}
        self._create_lock = asyncio.Lock()
        self._ready = False

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
            self._sandboxes[session_id] = self._from_labels(container.id, labels)
            adopted += 1
        return adopted

    def _from_labels(self, container_id: str, labels: dict[str, str]) -> HermesSandbox:
        session_id = labels[SESSION_LABEL]
        return HermesSandbox(
            session_id=session_id,
            org_id=labels.get(ORG_LABEL, ""),
            conversation_id=labels.get(CONVERSATION_LABEL, ""),
            container_id=container_id,
            network=resource_name(session_id),
            volume=resource_name(session_id),
            created_at=float(labels.get(CREATED_LABEL, "0") or 0),
        )

    def get(self, session_id: str) -> HermesSandbox:
        sandbox = self._sandboxes.get(session_id)
        if sandbox is None:
            raise HermesSandboxError("hermes_sandbox_not_found")
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

    def _container_config(self, name: str, labels: dict[str, str]) -> dict[str, Any]:
        return {
            "Image": self._settings.hermes_sandbox_image,
            "User": f"{SANDBOX_UID}:{SANDBOX_UID}",
            "Labels": labels,
            "HostConfig": {
                "NetworkMode": name,
                "ReadonlyRootfs": True,
                "Tmpfs": {"/tmp": f"rw,nosuid,nodev,size={self._settings.hermes_tmp_mb}m"},
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "Memory": self._settings.hermes_memory_bytes,
                "NanoCpus": int(self._settings.hermes_cpus * 1_000_000_000),
                "PidsLimit": self._settings.hermes_pids_limit,
                "Mounts": [{"Type": "volume", "Source": name, "Target": "/workspace"}],
                "RestartPolicy": {"Name": "unless-stopped"},
            },
        }

    async def create(
        self, *, session_id: str, org_id: str, conversation_id: str, token: str, model: str
    ) -> tuple[HermesSandbox, bool]:
        """The session's sandbox, created if it does not exist yet."""
        if not self._ready:
            await self.ready()
        async with self._create_lock:
            existing = self._sandboxes.get(session_id)
            if existing is not None:
                if (existing.org_id, existing.conversation_id) != (org_id, conversation_id):
                    raise HermesSandboxError("hermes_sandbox_owner_mismatch")
                return existing, False
            name = resource_name(session_id)
            created = time.time()
            labels = self._labels(session_id, org_id, conversation_id, created)
            try:
                await self._create_network(name, labels)
                await self._connect_guard(name)
                await self.docker.volumes.create({"Name": name, "Labels": labels})  # type: ignore[no-untyped-call]
                container = await self.docker.containers.create(
                    config=self._container_config(name, labels), name=name
                )
                api_key = secrets.token_urlsafe(32)
                config = hermes_config(self._settings, model=model, token=token, api_key=api_key)
                proxy = f"http://hermes:{token}@{self._settings.hermes_proxy_address}"
                await container.put_archive(  # type: ignore[no-untyped-call]
                    "/workspace",
                    _tar(
                        {
                            ".hermes/config.yaml": json.dumps(config, indent=1).encode(),
                            ".aurum/proxy": proxy.encode(),
                        }
                    ),
                )
                await container.start()
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
        try:
            await self._wait_healthy(sandbox)
        except Exception:
            await self.close(session_id)
            raise
        return sandbox, True

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
            await stream.close()

    async def run(self, session_id: str, request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        sandbox = self.get(session_id)
        key = hashlib.sha256(str(request["key"]).encode()).hexdigest()
        container = await self.docker.containers.get(sandbox.container_id)
        await container.put_archive(  # type: ignore[no-untyped-call]
            "/workspace", _tar({f".aurum/requests/{key}.json": json.dumps(request).encode()})
        )
        self.touch(session_id)
        async for line in self._stream(sandbox, ["hermes-relay", "run", f"/workspace/.aurum/requests/{key}.json"]):
            yield line

    async def events(self, session_id: str, run_id: str) -> AsyncIterator[dict[str, Any]]:
        sandbox = self.get(session_id)
        async for line in self._stream(sandbox, ["hermes-relay", "events", run_id]):
            yield line

    async def stop(self, session_id: str, run_id: str) -> None:
        sandbox = self.get(session_id)
        await self._exec(sandbox, ["hermes-relay", "stop", run_id])

    # Removal and reaping ----------------------------------------------------

    async def close(self, session_id: str) -> None:
        """Idempotent; also removes what a crashed create left behind."""
        self._sandboxes.pop(session_id, None)
        await self._remove(resource_name(session_id))

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

    async def reconcile(self) -> None:
        """Reconnect the guard (a recreated guard loses runtime connections)."""
        for sandbox in list(self._sandboxes.values()):
            try:
                await self._connect_guard(sandbox.network)
            except DockerError:
                logger.warning("could not reconnect hermes-egress to %s", sandbox.network, exc_info=True)

    async def reap(self) -> int:
        now = time.time()
        expired = [
            sandbox.session_id
            for sandbox in self._sandboxes.values()
            if now - sandbox.created_at >= self._settings.hermes_max_lifetime_seconds
            or now - sandbox.last_active >= self._settings.hermes_idle_ttl_seconds
        ]
        for session_id in expired:
            logger.info("Reaping Hermes sandbox %s", session_id)
            await self.close(session_id)
        await self._reap_leftovers(now)
        return len(expired)

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
