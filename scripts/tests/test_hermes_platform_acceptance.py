"""The pieces of the platform acceptance driver that need no cluster."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import os
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

DRIVER = Path(__file__).parents[1] / "hermes-k8s-platform-acceptance.py"
ENVIRONMENT = {
    "K8S_HERMES_BACKEND_URL": "http://backend",
    "K8S_HERMES_ADMIN_URL": "http://admin",
    "K8S_HERMES_KEYCLOAK_URL": "http://keycloak",
    "K8S_HERMES_USER": "user",
    "K8S_HERMES_PASSWORD": "password",
    "K8S_HERMES_NAMESPACE": "hermes",
    "K8S_HERMES_RELEASE": "rel",
    "K8S_HERMES_PLATFORM_NAMESPACE": "platform",
}


@pytest.fixture(scope="module")
def driver():
    previous = {key: os.environ.get(key) for key in ENVIRONMENT}
    os.environ.update(ENVIRONMENT)
    try:
        spec = importlib.util.spec_from_file_location("hermes_k8s_platform_acceptance", DRIVER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_the_workbook_is_a_package_with_the_parts_a_spreadsheet_reader_needs(driver) -> None:
    data = driver.make_xlsx([["region", "month", "sales"], ["North", "Jan", 100]])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        sheet = archive.read("xl/worksheets/sheet1.xml").decode()
        types = archive.read("[Content_Types].xml").decode()
    assert {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/worksheets/sheet1.xml"} <= names
    assert 't="inlineStr"' in sheet and "<v>100</v>" in sheet and 'r="C2"' in sheet
    assert "spreadsheetml.sheet.main+xml" in types and data[:2] == b"PK"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_preview_app_serves_a_page_that_reports_the_cookie_and_echoes_websocket_frames(driver) -> None:
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "-c", driver.PREVIEW_APP.replace(str(driver.PREVIEW_PORT), str(port))],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        with socket.create_connection(("127.0.0.1", port), 5) as http:
            http.sendall(b"GET /a/path HTTP/1.1\r\nHost: x\r\nCookie: a=b\r\n\r\n")
            reply = http.recv(4096)
        assert reply.startswith(b"HTTP/1.1 200") and reply.endswith(b"preview-ok;cookie=a=b;path=/a/path")

        with socket.create_connection(("127.0.0.1", port), 5) as ws:
            key = base64.b64encode(os.urandom(16)).decode()
            ws.sendall(
                f"GET /socket HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
            )
            handshake = b""
            while b"\r\n\r\n" not in handshake:
                handshake += ws.recv(4096)
            expected = base64.b64encode(
                hashlib.sha1(key.encode() + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
            )
            assert b" 101 " in handshake.split(b"\r\n")[0] and expected in handshake
            assert driver.ws_exchange(ws, b"ping") == b"echo:ping"
            assert driver.ws_exchange(ws, b"again") == b"echo:again"
    finally:
        process.kill()
        process.wait()


def test_the_driver_reports_the_preview_origin_it_needs_and_skips_cleanly_without_one(driver) -> None:
    assert driver.PREVIEW_DOMAIN == "preview.example.test" and driver.PREVIEW_PORT == 8898
    assert driver.PREVIEW_HTTPS_PORT == ""  # not given here: the previews are reported as not checked
