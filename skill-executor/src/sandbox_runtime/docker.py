import asyncio
import json
import logging
import time
import uuid
from dataclasses import replace
from typing import Any, Literal

import aiodocker
from aiodocker.exceptions import DockerError

from src.config import Settings, settings
from src.sandbox_runtime.base import (
    BundleExtractionError,
    NetworkPolicyError,
    SandboxSession,
    SessionNotFoundError,
    _bundle_to_tar,
    _decode_exec_result,
    _wrap_single_file_tar,
)

logger = logging.getLogger(__name__)

# Every sandbox container carries this label so it stays findable even after
# this process (and its in-memory session map) is gone — see reap_orphans().
SANDBOX_LABEL = "aurum-ai.skill-sandbox"

# Networks any other service in this stack is reachable from — an egress
# sandbox must never land on one of these, or "egress" would also mean
# "reach every internal service," not just "reach the internet."
_FORBIDDEN_EGRESS_NETWORKS = {
    "none",
    "default",
    "bridge",
    "host",
    "skills-net",
    "runner-net",
    "rag-net",
}


class DockerSandboxRuntime:
    """Ephemeral, single-use Docker sandbox containers for code skill sessions."""

    kind: Literal["docker"] = "docker"

    def __init__(
        self,
        runtime_settings: Settings | None = None,
        docker: aiodocker.Docker | None = None,
    ) -> None:
        self._settings = runtime_settings or settings
        self._docker = docker
        self._owns_docker = docker is None
        self._sessions: dict[str, SandboxSession] = {}

    @property
    def docker(self) -> aiodocker.Docker:
        if self._docker is None:
            self._docker = aiodocker.Docker()
            self._owns_docker = True
        return self._docker

    async def close(self) -> None:
        if self._owns_docker and self._docker is not None:
            await self._docker.close()
            self._docker = None
            self._owns_docker = False

    async def ready(self) -> None:
        await self.ensure_egress_network()

    async def shutdown(self) -> None:
        await self.close_all()
        await self.close()

    async def ensure_egress_network(self) -> None:
        """Idempotently create the configured dedicated egress network."""
        network_name = self._settings.sandbox_network_default
        if network_name in _FORBIDDEN_EGRESS_NETWORKS:
            return
        try:
            await self.docker.networks.get(network_name)
            return
        except DockerError:
            pass
        await self.docker.networks.create({"Name": network_name, "Driver": "bridge"})
        logger.info("Created egress sandbox network %s", network_name)

    def _container_config(self, network_policy: str) -> dict[str, Any]:
        if network_policy == "egress":
            network_mode = self._settings.sandbox_network_default
            if network_mode in _FORBIDDEN_EGRESS_NETWORKS:
                raise NetworkPolicyError(
                    f"network_policy=egress requires SKILL_EXECUTOR_SANDBOX_"
                    f"NETWORK_DEFAULT to be set to a dedicated egress network "
                    f"(got {network_mode!r}, which is unset or an internal "
                    f"network) — see docker-compose.yml's skills-egress-net"
                )
        else:
            network_mode = "none"
        return {
            "Image": self._settings.sandbox_image,
            "Cmd": ["sleep", "infinity"],
            "Labels": {SANDBOX_LABEL: "1"},
            "HostConfig": {
                "NetworkMode": network_mode,
                "Memory": self._settings.sandbox_memory_limit,
                "CpuQuota": self._settings.sandbox_cpu_quota,
                "PidsLimit": self._settings.sandbox_pids_limit,
                "AutoRemove": False,
                # mode=1777 (world-writable + sticky, same as /tmp): the
                # container runs as non-root `skilluser` (uid 10001, see
                # skill-sandbox/Dockerfile), which doesn't own a freshly
                # mounted tmpfs root otherwise.
                "Tmpfs": {
                    "/skill": f"rw,size={self._settings.sandbox_skill_tmpfs_mb}m,mode=1777"
                },
            },
        }

    async def _exec_and_check(self, container: Any, cmd: list[str], user: str = "") -> None:
        """Run a short bootstrap command and raise if it does not exit 0."""
        exec_ = await container.exec(cmd=cmd, stdout=True, stderr=True, user=user)
        stream = exec_.start(detach=False)
        chunks: list[bytes] = []
        try:
            while True:
                message = await stream.read_out()
                if message is None:
                    break
                chunks.append(message.data)
        finally:
            await stream.close()
        info = await exec_.inspect()
        exit_code = info.get("ExitCode")
        if exit_code != 0:
            output = b"".join(chunks).decode(errors="replace")
            raise BundleExtractionError(
                f"Command {cmd} failed with exit code {exit_code}: {output}"
            )

    async def create_session(
        self,
        bundle_bytes: bytes,
        skill_md: str,
        network_policy: str = "none",
    ) -> SandboxSession:
        tar_bytes = _bundle_to_tar(bundle_bytes, self._settings)

        session_id = uuid.uuid4().hex
        name = f"skill-sandbox-{session_id[:12]}"
        container = await self.docker.containers.create(
            config=self._container_config(network_policy), name=name
        )
        try:
            await container.start()
            # /skill is a tmpfs mount (see _container_config) — Docker's
            # put_archive extracts through the storage-driver snapshot, not
            # the live mount namespace, so it silently writes nothing when
            # the target is a tmpfs mount. Land the tar as a single opaque
            # file on the regular writable layer, then extract inside the
            # container where the live tmpfs mount is visible.
            await container.put_archive(  # type: ignore[no-untyped-call]
                path="/tmp", data=_wrap_single_file_tar("bundle.tar", tar_bytes)
            )
            await self._exec_and_check(
                container, ["tar", "-x", "-C", "/skill", "-f", "/tmp/bundle.tar"]
            )
            await self._exec_and_check(
                container, ["rm", "-f", "/tmp/bundle.tar"], user="root"
            )
        except Exception:
            await self._force_remove(container)
            raise

        now = time.time()
        session = SandboxSession(
            session_id=session_id,
            resource_id=container.id,
            resource_uid=container.id,
            resource_name=name,
            skill_md=skill_md,
            created_at=now,
            last_active=now,
        )
        self._sessions[session_id] = session
        return session

    async def exec_command(
        self, session_id: str, command: str, timeout: float | None = None
    ) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(f"Unknown or expired session: {session_id}")

        exec_timeout = timeout or self._settings.exec_timeout_seconds
        container = await self.docker.containers.get(session.resource_id)
        # coreutils `timeout` enforces the budget *inside* the container —
        # Docker's API cannot kill an exec'd process, so without it a timed-out
        # command would keep running until container teardown.
        exec_ = await container.exec(
            cmd=["timeout", "-k", "5", str(int(exec_timeout)), "/bin/sh", "-c", command],
            workdir="/skill",
            stdout=True,
            stderr=True,
        )

        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        collected = {1: 0, 2: 0}
        truncated = {1: False, 2: False}
        max_bytes = self._settings.exec_output_max_bytes
        timed_out = False
        stream = exec_.start(detach=False)
        try:
            # One overall deadline for the whole command; +10s of slack so
            # the in-container `timeout` normally fires first and we still
            # collect its output.
            async with asyncio.timeout(exec_timeout + 10):
                while True:
                    message = await stream.read_out()
                    if message is None:
                        break
                    fd = 2 if message.stream == 2 else 1
                    if collected[fd] < max_bytes:
                        take = message.data[: max_bytes - collected[fd]]
                        collected[fd] += len(take)
                        (stdout_chunks if fd == 1 else stderr_chunks).append(take)
                        if len(take) < len(message.data):
                            truncated[fd] = True
                    else:
                        truncated[fd] = True
        except TimeoutError:
            timed_out = True
        finally:
            await stream.close()

        exit_code: int | None = None
        if not timed_out:
            info = await exec_.inspect()
            exit_code = info.get("ExitCode")
            if exit_code == 124:
                timed_out = True

        current_session = self._sessions.get(session_id)
        if current_session is not None:
            self._sessions[session_id] = replace(current_session, last_active=time.time())
        return _decode_exec_result(
            stdout_chunks=stdout_chunks,
            stderr_chunks=stderr_chunks,
            truncated=truncated,
            max_bytes=max_bytes,
            exit_code=exit_code,
            timed_out=timed_out,
        )

    async def close_session(self, session_id: str) -> None:
        """Idempotent session close; callers may race with the reaper."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        try:
            container = await self.docker.containers.get(session.resource_id)
        except DockerError:
            return
        await self._force_remove(container)

    async def _force_remove(self, container: Any) -> None:
        try:
            await container.kill()
        except DockerError:
            pass
        try:
            await container.delete(force=True)
        except DockerError:
            pass

    async def reap_expired(self) -> int:
        """Force-close sessions past their absolute or idle TTL."""
        now = time.time()
        expired = [
            sid
            for sid, session in self._sessions.items()
            if now - session.created_at >= self._settings.session_ttl_seconds
            or now - session.last_active >= self._settings.session_idle_ttl_seconds
        ]
        for sid in expired:
            logger.info("Reaping expired skill sandbox session %s", sid)
            await self.close_session(sid)
        return len(expired)

    async def reap_orphans(self) -> int:
        """Remove labeled sandbox containers this process does not know about."""
        now = time.time()
        known_ids = {session.resource_id for session in self._sessions.values()}
        try:
            containers = await self.docker.containers.list(
                all=True, filters=json.dumps({"label": [f"{SANDBOX_LABEL}=1"]})
            )
        except DockerError:
            logger.warning("Could not list sandbox containers for orphan reaping", exc_info=True)
            return 0

        reaped = 0
        for container in containers:
            if container.id in known_ids:
                continue
            created = container._container.get("Created") or 0
            if now - created < self._settings.session_ttl_seconds:
                continue
            logger.info("Reaping orphaned skill sandbox container %s", container.id)
            await self._force_remove(container)
            reaped += 1
        return reaped

    async def run_reaper(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._settings.reap_interval_seconds)
                await self.reap_expired()
                await self.reap_orphans()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("skill sandbox reaper loop iteration failed", exc_info=True)

    async def close_all(self) -> None:
        for sid in list(self._sessions.keys()):
            await self.close_session(sid)
