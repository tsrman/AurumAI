from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_independent_scanner_keeps_token_detection_with_exact_fixture_exceptions(tmp_path: Path):
    scanner = os.environ.get("GITLEAKS_BIN")
    if not scanner:
        pytest.skip("Pinned independent scanner must be explicitly installed")
    config = ROOT / ".gitleaks.toml"
    assert config.is_file()
    source = tmp_path / "source"
    source.mkdir()
    (source / "fixtures.txt").write_text('STORAGE_KEY, "conversation-1"\n'
        'langfuseSecretKey: "sk-lf-550e8400-e29b-41d4-a716-446655440001"\n'
        'access_revoked", non_retryable=True\nkeys, unique/idempotency \n')
    command = [scanner, "dir", str(source), "--config", str(config), "--redact", "--no-banner"]
    assert subprocess.run(command, capture_output=True).returncode == 0
    # A credential on the very same fixture path must still be detected.
    (source / "fixtures.txt").write_text('api_key = "' + "ghp_" + "aB2cD3eF4gH5iJ6kL7mN8oP9qR0sT1uV2wX3" + '"\n')
    assert subprocess.run(command, capture_output=True).returncode == 1
