"""Executable contract for the manual-only, non-mutating auth launcher."""

import os
from pathlib import Path
import subprocess


WRAPPER = Path(__file__).resolve().parents[2] / "smoke-test-auth-lifecycle.sh"


def test_auth_wrapper_preserves_failure_and_forces_private_serial_run(tmp_path: Path) -> None:
    wrapper = tmp_path / WRAPPER.name
    wrapper.write_bytes(WRAPPER.read_bytes())
    executable = tmp_path / "business-ui/node_modules/.bin/playwright"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        '#!/bin/bash\n'
        'test "$E2E_AUTH_LIFECYCLE" = true || exit 91\n'
        'test "$*" = "test e2e/auth-lifecycle.real.spec.ts --workers 1 --retries 0 --trace off" || exit 92\n'
        'exit 37\n'
    )
    executable.chmod(0o700)
    result = subprocess.run(
        ["bash", str(wrapper)], cwd="/tmp", capture_output=True, check=False,
        env={**os.environ, "E2E_AUTH_LIFECYCLE": "false"},
    )
    assert result.returncode == 37


def test_auth_wrapper_rejects_arguments_before_launch(tmp_path: Path) -> None:
    wrapper = tmp_path / WRAPPER.name
    wrapper.write_bytes(WRAPPER.read_bytes())
    result = subprocess.run(
        ["bash", str(wrapper), "--trace", "on"], capture_output=True, check=False,
    )
    assert result.returncode == 2


def test_auth_wrapper_requires_installed_playwright(tmp_path: Path) -> None:
    wrapper = tmp_path / WRAPPER.name
    wrapper.write_bytes(WRAPPER.read_bytes())
    (tmp_path / "business-ui").mkdir()
    result = subprocess.run(["bash", str(wrapper)], capture_output=True, check=False)
    assert result.returncode == 1
    assert b"npm ci" in result.stderr
