from pathlib import Path
import shlex

import yaml

ROOT = Path(__file__).resolve().parents[2]


def workflow(name):
    path = ROOT / ".github/workflows" / name
    assert path.is_file(), "Public release CI is not implemented"
    return yaml.safe_load(path.read_text())


def test_public_default_branch_and_fork_prs_have_read_only_ci():
    for name in ("ci.yml", "docs-mermaid.yml", "oss-release-checks.yml"):
        data = workflow(name)
        events = data.get("on", data.get(True))
        assert "main" in events["push"]["branches"]
        assert "pull_request" in events
        assert "pull_request_target" not in events
        assert data["permissions"] == {"contents": "read"}
        for job in data["jobs"].values():
            assert not job.get("permissions", {}).get("contents") == "write"


def test_oss_check_uses_an_exported_tree_and_redacts_independent_secret_scan():
    data = workflow("oss-release-checks.yml")
    steps = data["jobs"]["source-release"]["steps"]
    commands = "\n".join(s.get("run", "") for s in steps)
    assert "prepare-oss-release.py export" in commands
    assert "prepare-oss-release.py verify" in commands
    invocation = shlex.split(next(s["run"] for s in steps if s.get("name") == "Scan public candidate"))
    assert invocation[0].endswith("/gitleaks")
    assert invocation[1] == "dir" and "--redact" in invocation
    assert "secrets." not in str(data)
