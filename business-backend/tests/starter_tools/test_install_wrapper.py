"""Run the real installer wrapper with an inert Compose recorder."""

import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def compose_recorder(tmp_path):
    executable = tmp_path / "docker"
    executable.write_text(
        "#!/usr/bin/env python3\nimport json,os,sys\n"
        "with open(os.environ['STARTER_TEST_LOG'], 'a') as stream:\n"
        " stream.write(json.dumps({'args':sys.argv[1:], 'secret':os.environ.get('APP_SECRET_KEY'),"
        " 'profiles':os.environ.get('COMPOSE_PROFILES')})+'\\n')\n"
    )
    executable.chmod(0o755)
    log = tmp_path / "commands.jsonl"
    environment = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
                   "STARTER_TEST_LOG": str(log), "APP_SECRET_KEY": "synthetic-retained-secret",
                   "COMPOSE_PROFILES": "mcp,archive", "COMPOSE_FILE": "untrusted-overlay.yml"}
    return environment, log


def invoke(compose_recorder, *args):
    environment, log = compose_recorder
    result = subprocess.run(
        ["bash", str(ROOT / "install-platform.sh"), *args],
        cwd="/tmp", env=environment, text=True, capture_output=True, timeout=10,
    )
    records = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, records


def test_wrapper_dry_run_has_no_external_operations(compose_recorder):
    result, records = invoke(compose_recorder)
    assert result.returncode == 0, result.stderr
    assert records == []
    assert "dry run" in result.stdout.lower()
    assert "mcp-everything" in result.stdout
    assert "mcp-fetch" not in result.stdout


def test_apply_starts_only_bounded_optional_services_and_keeps_secret(compose_recorder):
    org_id = "5e8dd172-c21b-4e68-ac93-aef646d1ac42"
    result, records = invoke(compose_recorder, "--apply", "--org-id", org_id)
    assert result.returncode == 0, result.stderr
    assert len(records) == 4
    args = [record["args"] for record in records]
    assert all(item[:3] == ["compose", "-f", str(ROOT / "docker-compose.yml")] for item in args)
    assert "skill-sandbox" in args[0] and "build" in args[0]
    assert args[1][-4:] == ["-d", "--build", "--wait", "--wait-timeout=300"]
    assert args[2][-2:] == ["mcp-everything", "skill-executor"]
    assert args[3][-6:] == ["-m", "src.cli.install_starter_tools", "--apply", "--org-id", org_id, "--mcp-endpoint=http://mcp-everything:3001/sse"]
    assert not any("mcp-fetch" in item or "web.access" in item for item in args)
    assert all(record["secret"] == "synthetic-retained-secret" for record in records)
    assert all(record["profiles"] == "" for record in records)


def test_wrapper_rejects_unknown_options_without_mutating(compose_recorder):
    result, records = invoke(compose_recorder, "--all")
    assert result.returncode != 0
    assert records == []
