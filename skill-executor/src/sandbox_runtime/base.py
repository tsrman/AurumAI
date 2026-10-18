import io
import tarfile
import zipfile
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from src.config import Settings, settings


class SessionNotFoundError(Exception):
    pass


class BundleExtractionError(Exception):
    pass


class NetworkPolicyError(Exception):
    """Raised when a requested sandbox network policy cannot be applied safely."""


class SandboxExecError(Exception):
    """Raised when a runtime exec stream cannot produce a trustworthy result."""


@dataclass(frozen=True)
class SandboxSession:
    session_id: str
    resource_id: str
    resource_uid: str
    resource_name: str
    skill_md: str
    created_at: float
    last_active: float

    @property
    def container_id(self) -> str:
        return self.resource_id

    @property
    def container_name(self) -> str:
        return self.resource_name


class SandboxRuntime(Protocol):
    @property
    def kind(self) -> Literal["docker", "kubernetes"]: ...

    async def create_session(
        self,
        bundle_bytes: bytes,
        skill_md: str,
        network_policy: str = "none",
    ) -> SandboxSession: ...

    async def exec_command(
        self,
        session_id: str,
        command: str,
        timeout: float | None = None,
    ) -> dict[str, Any]: ...

    async def close_session(self, session_id: str) -> None: ...

    async def reap_expired(self) -> int: ...

    async def reap_orphans(self) -> int: ...

    async def run_reaper(self) -> None: ...

    async def ready(self) -> None: ...

    async def shutdown(self) -> None: ...


def _wrap_single_file_tar(name: str, data: bytes) -> bytes:
    """Wrap raw bytes as a one-entry tar archive for Docker put_archive."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _bundle_to_tar(bundle_bytes: bytes, runtime_settings: Settings = settings) -> bytes:
    """Re-package a validated zip bundle as a tar stream for sandbox upload."""
    tar_buf = io.BytesIO()
    try:
        with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as zf:
            infos = zf.infolist()
            if len(infos) > runtime_settings.bundle_max_entries:
                raise BundleExtractionError(
                    f"Bundle contains too many entries (max {runtime_settings.bundle_max_entries})"
                )
            total_uncompressed = sum(info.file_size for info in infos)
            total_compressed = sum(info.compress_size for info in infos)
            if total_uncompressed > runtime_settings.bundle_max_uncompressed_bytes:
                raise BundleExtractionError("Bundle uncompressed size exceeds limit")
            if (
                total_compressed > 0
                and total_uncompressed
                / total_compressed
                > runtime_settings.bundle_max_compression_ratio
            ):
                raise BundleExtractionError(
                    "Bundle compression ratio is too high (possible zip bomb)"
                )
            with tarfile.open(fileobj=tar_buf, mode="w") as tf:
                for info in infos:
                    if info.is_dir():
                        continue
                    name = info.filename
                    if name.startswith("/") or ".." in name.split("/"):
                        raise BundleExtractionError(f"Unsafe path in bundle: {name}")
                    data = zf.read(info)
                    tar_info = tarfile.TarInfo(name=name)
                    tar_info.size = len(data)
                    tf.addfile(tar_info, io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise BundleExtractionError(f"Bundle is not a valid zip archive: {exc}") from exc
    return tar_buf.getvalue()


def _decode_exec_result(
    stdout_chunks: list[bytes],
    stderr_chunks: list[bytes],
    truncated: dict[int, bool],
    max_bytes: int,
    exit_code: int | None,
    timed_out: bool,
) -> dict[str, Any]:
    def decode(chunks: list[bytes], fd: int) -> str:
        text = b"".join(chunks).decode(errors="replace")
        if truncated[fd]:
            text += f"\n[output truncated at {max_bytes} bytes]"
        return text

    return {
        "stdout": decode(stdout_chunks, 1),
        "stderr": decode(stderr_chunks, 2),
        "exit_code": exit_code,
        "timed_out": timed_out,
    }
