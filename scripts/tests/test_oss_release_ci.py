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


def _publish_matrix():
    data = workflow("publish-images.yml")
    return data, data["jobs"]["publish"]["strategy"]["matrix"]["include"]


def test_image_publication_runs_only_for_release_tags_or_manual_dispatch():
    data, _ = _publish_matrix()
    events = data.get("on", data.get(True))
    assert set(events) == {"push", "workflow_dispatch"}
    assert events["push"] == {"tags": ["v*"]}
    assert data["permissions"] == {"contents": "read"}
    job = data["jobs"]["publish"]
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    text = (ROOT / ".github/workflows/publish-images.yml").read_text()
    assert "secrets.GITHUB_TOKEN" in text
    assert "secrets." not in text.replace("secrets.GITHUB_TOKEN", "")


def test_published_images_cover_every_chart_image_and_have_a_dockerfile():
    _, matrix = _publish_matrix()
    names = {entry["name"] for entry in matrix}
    assert len(names) == len(matrix)
    for entry in matrix:
        assert (ROOT / entry["context"] / "Dockerfile").is_file(), entry
        assert entry["name"].startswith("aurum-ai-")
    values = yaml.safe_load((ROOT / "charts/aurum-ai/values.yaml").read_text())
    chart_images = set()

    def collect(node):
        if isinstance(node, dict):
            repository = node.get("repository")
            if isinstance(repository, str) and repository.startswith("aurum-ai-"):
                chart_images.add(repository)
            for value in node.values():
                collect(value)

    collect(values)
    assert chart_images and chart_images <= names


def test_published_images_carry_source_and_license_labels():
    data, _ = _publish_matrix()
    build = next(
        s for s in data["jobs"]["publish"]["steps"]
        if str(s.get("uses", "")).startswith("docker/build-push-action")
    )
    assert build["with"]["push"] is True
    labels = build["with"]["labels"]
    assert "org.opencontainers.image.source=" in labels
    assert "org.opencontainers.image.licenses=AGPL-3.0-only" in labels
