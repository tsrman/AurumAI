from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "bootstrap-dev-env.sh"


def run_bootstrap(path: Path, **environment):
    assert SCRIPT.is_file(), "Persistent bootstrap is not implemented"
    inherited = {k: v for k, v in os.environ.items() if k != "APP_SECRET_KEY"}
    return subprocess.run(
        ["bash", str(SCRIPT), "--env-file", str(path)],
        env={**inherited, **environment}, capture_output=True, text=True, timeout=15,
    )


def test_new_env_has_private_permissions_and_is_stable_on_repeat(tmp_path: Path):
    path = tmp_path / ".env"
    first = run_bootstrap(path)
    assert first.returncode == 0, first.stderr
    content = path.read_text()
    assert re.search(r"^APP_SECRET_KEY=[a-f0-9]{64}$", content, re.MULTILINE)
    assert path.stat().st_mode & 0o777 == 0o600
    second = run_bootstrap(path)
    assert second.returncode == 0, second.stderr
    assert path.read_text() == content
    key = re.search(r"^APP_SECRET_KEY=(.+)$", content, re.MULTILINE).group(1)
    assert key not in first.stdout + first.stderr + second.stdout + second.stderr


def test_configured_values_comments_and_other_settings_are_preserved(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text("# Keep this comment\nAPP_SECRET_KEY=operator-supplied-secret\nAPP_DEBUG=false\nOTHER_SETTING=custom\n")
    result = run_bootstrap(path)
    assert result.returncode == 0, result.stderr
    content = path.read_text()
    assert "APP_SECRET_KEY=operator-supplied-secret\n" in content
    assert "APP_DEBUG=false\n" in content
    assert "# Keep this comment\n" in content
    assert "OTHER_SETTING=custom\n" in content
    assert "operator-supplied-secret" not in result.stdout + result.stderr


def test_duplicate_secret_assignment_fails_without_modifying_env(tmp_path: Path):
    path = tmp_path / ".env"
    initial = "APP_SECRET_KEY=first\nAPP_SECRET_KEY=second\n"
    path.write_text(initial)
    result = run_bootstrap(path)
    assert result.returncode != 0
    assert path.read_text() == initial
    assert not list(tmp_path.glob(".env.tmp.*"))


def test_symlink_target_is_never_followed(tmp_path: Path):
    original = tmp_path / "private"
    original.write_text("APP_SECRET_KEY=protected\n")
    path = tmp_path / ".env"
    path.symlink_to(original)
    result = run_bootstrap(path)
    assert result.returncode != 0
    assert original.read_text() == "APP_SECRET_KEY=protected\n"


def test_failure_during_secret_generation_keeps_previous_env(tmp_path: Path):
    path = tmp_path / ".env"
    initial = "APP_DEBUG=true\n"
    path.write_text(initial)
    tools = tmp_path / "tools"
    tools.mkdir()
    fake = tools / "openssl"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    result = run_bootstrap(path, PATH=str(tools) + os.pathsep + os.environ["PATH"])
    assert result.returncode != 0
    assert path.read_text() == initial
    assert not list(tmp_path.glob(".env.tmp.*"))


def test_env_is_never_executed_as_shell_code(tmp_path: Path):
    path = tmp_path / ".env"
    sentinel = tmp_path / "must-not-exist"
    path.write_text(f"APP_SECRET_KEY=$(touch {sentinel})\n")
    result = run_bootstrap(path)
    assert result.returncode == 0, result.stderr
    assert not sentinel.exists()


def test_explicit_environment_secret_is_persisted_on_first_bootstrap(tmp_path: Path):
    path = tmp_path / ".env"
    result = run_bootstrap(path, APP_SECRET_KEY="explicit-operator-supplied-secret")
    assert result.returncode == 0, result.stderr
    assert "APP_SECRET_KEY=explicit-operator-supplied-secret\n" in path.read_text()
    assert "explicit-operator-supplied-secret" not in result.stdout + result.stderr


def test_environment_secret_with_dotenv_metacharacters_round_trips(tmp_path: Path):
    from dotenv import dotenv_values
    path = tmp_path / ".env"
    sentinel = "value $with space # and 'quotes' and \\slashes"
    result = run_bootstrap(path, APP_SECRET_KEY=sentinel)
    assert result.returncode == 0, result.stderr
    assert dotenv_values(path)["APP_SECRET_KEY"] == sentinel
    assert sentinel not in result.stdout + result.stderr


def test_whitespace_and_export_assignments_preserve_effective_configured_secrets(tmp_path: Path):
    from dotenv import dotenv_values
    path = tmp_path / ".env"
    initial = "  APP_SECRET_KEY = 'operator-signing-secret' # keep\n\texport APP_DATABASE_ROLE_PASSWORD = \"database-secret\"\n"
    path.write_text(initial)
    result = run_bootstrap(path)
    assert result.returncode == 0, result.stderr
    assert path.read_text().startswith(initial)
    parsed = dotenv_values(path)
    assert parsed['APP_SECRET_KEY'] == 'operator-signing-secret'
    assert parsed['APP_DATABASE_ROLE_PASSWORD'] == 'database-secret'
    assert 'operator-signing-secret' not in result.stdout + result.stderr


def test_whitespace_normalized_duplicates_fail_without_replacing_file(tmp_path: Path):
    path = tmp_path / ".env"
    initial = "APP_SECRET_KEY=first\n export APP_SECRET_KEY = second\n"
    path.write_text(initial)
    result = run_bootstrap(path)
    assert result.returncode != 0
    assert path.read_text() == initial
    assert not list(tmp_path.glob('.env.tmp.*'))


def test_quoted_empty_values_with_inline_comments_are_populated(tmp_path: Path):
    from dotenv import dotenv_values
    path = tmp_path / ".env"
    path.write_text('APP_SECRET_KEY = "" # populate safely\nREDIS_PASSWORD=\'\' # cache secret\n')
    result = run_bootstrap(path)
    assert result.returncode == 0, result.stderr
    values = dotenv_values(path)
    assert re.fullmatch('[a-f0-9]{64}', values['APP_SECRET_KEY'])
    assert re.fullmatch('[a-f0-9]{64}', values['REDIS_PASSWORD'])
    assert '# populate safely' in path.read_text()
    assert '# cache secret' in path.read_text()


def test_unsupported_multiline_dotenv_fails_before_replacement(tmp_path: Path):
    path = tmp_path / '.env'
    initial = 'OTHER_SETTING="line1\nAPP_SECRET_KEY=not-an-assignment\nline3"\n'
    path.write_text(initial)
    result = run_bootstrap(path)
    assert result.returncode != 0
    assert path.read_text() == initial
    assert not list(tmp_path.glob('.env.tmp.*'))


def test_quoted_keys_escaped_values_and_crlf_preserve_operator_configuration(tmp_path: Path):
    from dotenv import dotenv_values
    path = tmp_path / '.env'
    initial = "  'APP_SECRET_KEY' = 'operator\\\'s key' # retain\r\n"
    path.write_bytes(initial.encode())
    result = run_bootstrap(path)
    assert result.returncode == 0, result.stderr
    assert path.read_bytes().startswith(initial.encode())
    assert dotenv_values(path)['APP_SECRET_KEY'] == "operator's key"


def test_bare_empty_required_key_preserves_its_inline_comment(tmp_path: Path):
    from dotenv import dotenv_values
    path = tmp_path / '.env'
    path.write_text('APP_SECRET_KEY # generate on first start\n')
    result = run_bootstrap(path)
    assert result.returncode == 0, result.stderr
    assert re.fullmatch('[a-f0-9]{64}', dotenv_values(path)['APP_SECRET_KEY'])
    assert '# generate on first start' in path.read_text()


def test_identity_and_dashboard_passwords_are_generated(tmp_path: Path):
    from dotenv import dotenv_values
    path = tmp_path / ".env"
    result = run_bootstrap(path)
    assert result.returncode == 0, result.stderr
    values = dotenv_values(path)
    for key in ("KEYCLOAK_ADMIN_PASSWORD", "TEMPORAL_UI_BASIC_AUTH_PASSWORD"):
        assert re.fullmatch("[a-f0-9]{64}", values[key]), key
