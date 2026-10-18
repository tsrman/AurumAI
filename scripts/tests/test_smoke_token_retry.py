from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ["smoke-test-mcp.sh", "smoke-test-code-skill.sh"]

STUB = """#!/usr/bin/env python3
import pathlib, sys
counter = pathlib.Path(sys.argv[0]).with_name("calls")
calls = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(calls))
failures = int(pathlib.Path(sys.argv[0]).with_name("failures").read_text())
if calls <= failures:
    sys.stdout.write('{"error":"unknown_error"}\\n500')
else:
    sys.stdout.write('{"access_token":"stub-token"}\\n200')
"""


def _functions(script: str) -> str:
    source = (ROOT / script).read_text()
    names = ("log", "keycloak_token", "get_token", "get_admin_token")
    blocks = []
    for name in names:
        match = re.search(rf"^{name}\(\) \{{.*?^\}}$|^{name}\(\) \{{[^\n]*\}}$", source, re.M | re.S)
        assert match, f"{script} must define {name}()"
        blocks.append(match.group(0))
    return "\n".join(blocks)


def _run(tmp_path: Path, script: str, function: str, failures: int) -> subprocess.CompletedProcess[str]:
    stub = tmp_path / "curl"
    stub.write_text(STUB)
    stub.chmod(0o755)
    (tmp_path / "failures").write_text(str(failures))
    program = "\n".join([
        "set -euo pipefail",
        'KEYCLOAK_URL=http://keycloak.invalid REALM=aurum-ai CLIENT_ID=business-ui ADMIN_CLIENT_ID=admin-ui',
        'USERNAME=admin PASSWORD=admin ADMIN_USERNAME=admin ADMIN_PASSWORD=admin TOKEN_RETRY_DELAY=0',
        _functions(script),
        function,
    ])
    return subprocess.run(
        ["bash", "-c", program],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
        capture_output=True, text=True, timeout=30,
    )


@pytest.mark.parametrize("script", SCRIPTS)
@pytest.mark.parametrize("function", ["get_token", "get_admin_token"])
def test_token_request_retries_transient_keycloak_errors(tmp_path: Path, script: str, function: str) -> None:
    result = _run(tmp_path, script, function, failures=2)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "stub-token"
    assert (tmp_path / "calls").read_text() == "3"
    assert "HTTP 500" in result.stderr


@pytest.mark.parametrize("script", SCRIPTS)
def test_token_request_fails_clearly_after_bounded_retries(tmp_path: Path, script: str) -> None:
    result = _run(tmp_path, script, "get_token", failures=99)
    assert result.returncode != 0
    assert result.stdout.strip() == ""
    assert int((tmp_path / "calls").read_text()) == 5
    assert "could not obtain a Keycloak token" in result.stderr
    assert "JSONDecodeError" not in result.stderr
