"""Parent-side isolated parser runner and Linux seccomp installation."""

import asyncio
import base64
import ctypes
import errno
from functools import lru_cache
import json
from pathlib import Path
import resource
import subprocess
import sys
import time
from typing import Any

from src.extraction import ExtractedText, MAX_DOCUMENT_BYTES
from src.transport import Download


MAX_PARSER_RESPONSE_BYTES = 256 * 1024
MAX_PARSER_CHILDREN = 4
_SCMP_ACT_ALLOW = 0x7FFF0000
_SCMP_ACT_ERRNO = 0x00050000 | errno.EPERM
_PR_SET_NO_NEW_PRIVS = 38
_DENIED_SYSCALLS = (
    "socket",
    "socketpair",
    "connect",
    "bind",
    "listen",
    "accept",
    "accept4",
    "sendto",
    "sendmsg",
    "sendmmsg",
    "open",
    "openat",
    "openat2",
    "creat",
    "kill",
    "tkill",
    "tgkill",
    "pidfd_open",
    "pidfd_send_signal",
    "process_vm_readv",
    "process_vm_writev",
    "prlimit64",
    "io_uring_setup",
    "io_uring_register",
    "io_uring_enter",
    "unshare",
    "setns",
    "clone",
    "clone3",
    "fork",
    "vfork",
    "execve",
    "execveat",
    "mount",
    "umount2",
    "pivot_root",
    "ptrace",
    "bpf",
    "perf_event_open",
    "keyctl",
    "add_key",
    "request_key",
)
# Created lazily against the running loop: asyncio primitives bind to the loop
# on first await, and a module-level one built at import time cannot be awaited
# from a different loop. A service process has exactly one loop for its
# lifetime; the rebind only happens where the loop genuinely changed.
_PARSER_SLOTS: asyncio.Semaphore | None = None
_PARSER_SLOTS_LOOP: asyncio.AbstractEventLoop | None = None


def parser_slots() -> asyncio.Semaphore:
    global _PARSER_SLOTS, _PARSER_SLOTS_LOOP
    loop = asyncio.get_running_loop()
    if _PARSER_SLOTS is None or _PARSER_SLOTS_LOOP is not loop:
        _PARSER_SLOTS = asyncio.Semaphore(MAX_PARSER_CHILDREN)
        _PARSER_SLOTS_LOOP = loop
    return _PARSER_SLOTS


class ParserFailed(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _seccomp_library() -> ctypes.CDLL:
    try:
        library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    except OSError as exc:
        raise ParserFailed("web_unavailable") from exc
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_release.restype = None
    return library


def _child_env(service_root: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONPATH": str(service_root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
    }


@lru_cache(maxsize=1)
def sandbox_available() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    try:
        library = _seccomp_library()
    except ParserFailed:
        return False
    if library.seccomp_syscall_resolve_name(b"socket") < 0:
        return False
    service_root = Path(__file__).parents[1]
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "from src.parser_sandbox import install_sandbox; install_sandbox()",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            cwd=service_root,
            env=_child_env(service_root),
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _set_limit(kind: int, maximum: int) -> None:
    _soft, hard = resource.getrlimit(kind)
    bounded_hard = maximum if hard == resource.RLIM_INFINITY else min(maximum, hard)
    resource.setrlimit(kind, (bounded_hard, bounded_hard))


def install_sandbox() -> None:
    if not sys.platform.startswith("linux"):
        raise ParserFailed("web_unavailable")
    _set_limit(resource.RLIMIT_AS, 256 * 1024 * 1024)
    _set_limit(resource.RLIMIT_CPU, 10)
    _set_limit(resource.RLIMIT_FSIZE, 10 * 1024 * 1024)
    _set_limit(resource.RLIMIT_NOFILE, 32)
    _set_limit(resource.RLIMIT_CORE, 0)

    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise ParserFailed("web_unavailable")

    library = _seccomp_library()
    context = library.seccomp_init(_SCMP_ACT_ALLOW)
    if not context:
        raise ParserFailed("web_unavailable")
    try:
        for name in _DENIED_SYSCALLS:
            syscall = library.seccomp_syscall_resolve_name(name.encode())
            if syscall < 0:
                continue
            if library.seccomp_rule_add(context, _SCMP_ACT_ERRNO, syscall, 0) != 0:
                raise ParserFailed("web_unavailable")
        if library.seccomp_load(context) != 0:
            raise ParserFailed("web_unavailable")
    finally:
        library.seccomp_release(context)


def _request_payload(download: Download) -> bytes:
    if len(download.body) > MAX_DOCUMENT_BYTES:
        raise ParserFailed("web_too_large")
    return json.dumps(
        {
            "content_type": download.content_type,
            "charset": download.charset,
            "body": base64.b64encode(download.body).decode("ascii"),
        },
        separators=(",", ":"),
    ).encode()


def _result_payload(raw: bytes) -> ExtractedText:
    try:
        value: Any = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParserFailed("web_unavailable") from exc
    if not isinstance(value, dict):
        raise ParserFailed("web_unavailable")
    error = value.get("error")
    if isinstance(error, str):
        if error in {"web_unavailable", "web_too_large", "unsupported_document", "web_timeout"}:
            raise ParserFailed(error)
        raise ParserFailed("web_unavailable")
    try:
        text = value["text"]
        title = value["title"]
        truncated = value["truncated"]
        page_count = value["page_count"]
    except KeyError as exc:
        raise ParserFailed("web_unavailable") from exc
    if (
        not isinstance(text, str)
        or not text
        or len(text) > 50000
        or (title is not None and not isinstance(title, str))
        or (isinstance(title, str) and len(title) > 2000)
        or not isinstance(truncated, bool)
        or (
            page_count is not None
            and (
                not isinstance(page_count, int)
                or isinstance(page_count, bool)
                or page_count < 0
                or page_count > 1_000_000
            )
        )
    ):
        raise ParserFailed("web_unavailable")
    return ExtractedText(text, title, truncated, page_count)


async def parse_isolated(download: Download, remaining_seconds: float) -> ExtractedText:
    if remaining_seconds <= 0:
        raise ParserFailed("web_timeout")
    payload = _request_payload(download)
    service_root = Path(__file__).parents[1]
    deadline = time.monotonic() + remaining_seconds
    slots = parser_slots()
    try:
        # Queue for a worker slot inside the caller's own budget rather than
        # failing the instant MAX_PARSER_CHILDREN reads are already running:
        # a hard, non-retryable 503 for the next concurrent read made ordinary
        # concurrency indistinguishable from an outage. Exhausting the budget
        # here is a timeout, and deliberately not retryable — the platform's
        # attempt budget is already spent by the time we get there.
        async with asyncio.timeout(remaining_seconds):
            await slots.acquire()
    except TimeoutError as exc:
        raise ParserFailed("web_timeout") from exc
    process: asyncio.subprocess.Process | None = None
    try:
        parse_seconds = deadline - time.monotonic()
        if parse_seconds <= 0:
            raise ParserFailed("web_timeout")
        try:
            async with asyncio.timeout(parse_seconds):
                try:
                    process = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "src.parser_worker",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        close_fds=True,
                        cwd=service_root,
                        env=_child_env(service_root),
                    )
                except OSError as exc:
                    raise ParserFailed("web_unavailable") from exc
                stdout = await _exchange_bounded(process, payload)
        except TimeoutError as exc:
            if process is not None:
                await _kill_and_reap(process)
            raise ParserFailed("web_timeout") from exc
        except asyncio.CancelledError:
            if process is not None:
                await _kill_and_reap(process)
            raise
        except ParserFailed:
            if process is not None:
                await _kill_and_reap(process)
            raise
        if process.returncode != 0:
            raise ParserFailed("web_unavailable")
        return _result_payload(stdout)
    finally:
        slots.release()


async def _exchange_bounded(process: asyncio.subprocess.Process, payload: bytes) -> bytes:
    if process.stdin is None or process.stdout is None:
        raise ParserFailed("web_unavailable")
    try:
        process.stdin.write(payload)
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        process.stdin.close()
        try:
            await process.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass
    output = bytearray()
    while True:
        chunk = await process.stdout.read(65536)
        if not chunk:
            break
        output.extend(chunk)
        if len(output) > MAX_PARSER_RESPONSE_BYTES:
            raise ParserFailed("web_unavailable")
    await process.wait()
    return bytes(output)


async def _kill_and_reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()
