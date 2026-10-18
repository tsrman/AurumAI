import asyncio
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from src.parser_sandbox import (
    MAX_PARSER_CHILDREN,
    ParserFailed,
    parse_isolated,
    parser_slots,
    sandbox_available,
)
from src.transport import Download


SERVICE_ROOT = Path(__file__).parents[1]


async def test_isolated_parser_accepts_html_but_rejects_untrusted_content_type() -> None:
    parsed = await parse_isolated(
        Download(
            final_url="https://example.org/report",
            content_type="text/html",
            body=b"<title>Report</title><p>Safe text</p><script>secret()</script>",
        ),
        remaining_seconds=5,
    )

    assert parsed.title == "Report"
    assert parsed.text == "Safe text"

    with pytest.raises(ParserFailed, match="unsupported_document"):
        await parse_isolated(
            Download(
                final_url="https://example.org/blob",
                content_type="application/octet-stream",
                body=b"<p>looks like html but is not trusted as html</p>",
            ),
            remaining_seconds=5,
        )


async def test_pdf_magic_selects_pdf_parser_even_with_generic_content_type() -> None:
    # Minimal one-page PDF containing a text drawing operation.
    document = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Resources"
        b"<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
        b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"5 0 obj<</Length 45>>stream\nBT /F1 12 Tf 72 720 Td (PDF text) Tj ET\n"
        b"endstream endobj\nxref\n0 6\n0000000000 65535 f \n"
        b"0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n"
        b"0000000241 00000 n \n0000000311 00000 n \n"
        b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n405\n%%EOF"
    )

    result = await parse_isolated(
        Download(
            final_url="https://example.org/report",
            content_type="application/octet-stream",
            body=document,
        ),
        remaining_seconds=5,
    )

    assert "PDF text" in result.text


async def test_pdf_body_mislabeled_as_html_never_uses_html_extraction() -> None:
    with pytest.raises(ParserFailed, match="unsupported_document"):
        await parse_isolated(
            Download(
                final_url="https://example.org/not-really-html",
                content_type="text/html",
                body=b"%PDF-1.4\nvisible payload text but malformed PDF",
            ),
            remaining_seconds=5,
        )


def test_child_seccomp_denies_network_filesystem_and_parent_signal_syscalls() -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux seccomp is required")
    program = """
import errno
import ctypes
import os
import socket
from src.parser_sandbox import install_sandbox
seccomp = ctypes.CDLL('libseccomp.so.2')
seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
io_uring_setup = seccomp.seccomp_syscall_resolve_name(b'io_uring_setup')
libc = ctypes.CDLL(None, use_errno=True)
install_sandbox()
outcomes = []
try:
    socket.socket()
except OSError as exc:
    outcomes.append(exc.errno)
try:
    open('/proc/self/status')
except OSError as exc:
    outcomes.append(exc.errno)
if io_uring_setup >= 0:
    result = libc.syscall(io_uring_setup, 1, 0)
    outcomes.append(ctypes.get_errno() if result == -1 else 0)
try:
    os.kill(os.getppid(), 0)
except OSError as exc:
    outcomes.append(exc.errno)
raise SystemExit(0 if outcomes == [errno.EPERM] * 4 else 3)
"""
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=SERVICE_ROOT,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": str(SERVICE_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr


async def test_timeout_kills_and_reaps_owned_parser_child(monkeypatch) -> None:
    processes: list[asyncio.subprocess.Process] = []
    original = asyncio.create_subprocess_exec

    async def observed_spawn(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        del args
        process = await original(
            sys.executable,
            "-c",
            "import sys,time; sys.stdin.buffer.read(); time.sleep(30)",
            **kwargs,
        )
        processes.append(process)
        assert "APP_SECRET_KEY" not in kwargs["env"]
        assert "WEB_TOOLS_SERVICE_SECRET" not in kwargs["env"]
        return process

    monkeypatch.setenv("APP_SECRET_KEY", "must-not-reach-child")
    monkeypatch.setenv("WEB_TOOLS_SERVICE_SECRET", "must-not-reach-child")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", observed_spawn)

    with pytest.raises(ParserFailed, match="web_timeout"):
        await parse_isolated(
            Download(
                final_url="https://example.org/report",
                content_type="text/html",
                body=b"<p>startup cannot finish inside this deadline</p>",
            ),
            remaining_seconds=0.1,
        )

    assert len(processes) == 1
    assert processes[0].returncode is not None


async def test_cancellation_kills_and_reaps_owned_parser_child(monkeypatch) -> None:
    original = asyncio.create_subprocess_exec
    processes: list[asyncio.subprocess.Process] = []
    spawned = asyncio.Event()

    async def sleeping_spawn(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        del args
        process = await original(
            sys.executable,
            "-c",
            "import sys,time; sys.stdin.buffer.read(); time.sleep(30)",
            **kwargs,
        )
        processes.append(process)
        spawned.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", sleeping_spawn)
    task = asyncio.create_task(
        parse_isolated(
            Download(
                final_url="https://example.org/report",
                content_type="text/html",
                body=b"<p>content</p>",
            ),
            remaining_seconds=10,
        )
    )
    await asyncio.wait_for(spawned.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(processes) == 1
    assert processes[0].returncode is not None


async def test_parser_stdout_is_bounded_and_flooding_child_is_reaped(monkeypatch) -> None:
    original = asyncio.create_subprocess_exec
    processes: list[asyncio.subprocess.Process] = []

    async def flooding_spawn(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        del args
        process = await original(
            sys.executable,
            "-c",
            "import sys,time; sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write(b'x' * (512 * 1024)); sys.stdout.flush(); time.sleep(5)",
            **kwargs,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", flooding_spawn)
    started = time.monotonic()
    with pytest.raises(ParserFailed, match="web_unavailable"):
        await parse_isolated(
            Download(
                final_url="https://example.org/report",
                content_type="text/html",
                body=b"<p>content</p>",
            ),
            remaining_seconds=1,
        )

    assert time.monotonic() - started < 0.5
    assert processes[0].returncode is not None


async def test_parser_spawn_failure_uses_stable_unavailable_error(monkeypatch) -> None:
    async def failed_spawn(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        del args, kwargs
        raise BlockingIOError("pid limit")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failed_spawn)

    with pytest.raises(ParserFailed, match="web_unavailable"):
        await parse_isolated(
            Download(
                final_url="https://example.org/report",
                content_type="text/html",
                body=b"<p>content</p>",
            ),
            remaining_seconds=1,
        )


async def test_parser_child_concurrency_is_capped_at_four(monkeypatch) -> None:
    original = asyncio.create_subprocess_exec
    release = asyncio.Event()
    four_waiting = asyncio.Event()
    entered = 0

    async def held_spawn(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal entered
        entered += 1
        if entered == 4:
            four_waiting.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", held_spawn)
    download = Download(
        final_url="https://example.org/report",
        content_type="text/html",
        body=b"<p>content</p>",
    )
    tasks = [asyncio.create_task(parse_isolated(download, 5)) for _ in range(5)]
    try:
        await asyncio.wait_for(four_waiting.wait(), timeout=1)
        await asyncio.sleep(0.05)
        # The cap still holds: only four children are ever spawned at once.
        assert entered == 4
        # The fifth now waits for a slot instead of being rejected outright.
        assert [task for task in tasks if task.done()] == []
    finally:
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    assert entered == 5
    assert all(getattr(result, "text", None) == "content" for result in results)


def test_sandbox_installation_is_available_before_enabling_read() -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux seccomp is required")
    assert sandbox_available() is True


async def test_concurrent_reads_queue_for_a_slot_instead_of_failing() -> None:
    """Ordinary concurrency must not look like an outage.

    The slot was taken with get_nowait, so the read after MAX_PARSER_CHILDREN
    concurrent ones got a hard, non-retryable web_unavailable even though the
    caller still had its whole budget left.
    """
    download = Download(
        final_url="https://example.org/",
        content_type="text/html",
        body=b"<html><body><p>ok</p></body></html>",
    )
    results = await asyncio.gather(
        *(parse_isolated(download, 25) for _ in range(MAX_PARSER_CHILDREN * 2)),
        return_exceptions=True,
    )
    assert [getattr(r, "text", r) for r in results] == ["ok"] * (MAX_PARSER_CHILDREN * 2)


async def test_a_slot_wait_that_exhausts_the_budget_is_a_timeout() -> None:
    """Waiting is bounded by the caller's deadline, and reported as such."""
    download = Download(
        final_url="https://example.org/",
        content_type="text/html",
        body=b"<html><body><p>ok</p></body></html>",
    )
    slots = parser_slots()
    for _ in range(MAX_PARSER_CHILDREN):
        await slots.acquire()
    try:
        with pytest.raises(ParserFailed) as failure:
            await parse_isolated(download, 0.2)
        assert failure.value.code == "web_timeout"
    finally:
        for _ in range(MAX_PARSER_CHILDREN):
            slots.release()
