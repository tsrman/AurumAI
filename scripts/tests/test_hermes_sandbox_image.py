"""The hermes-sandbox image and its relay."""

from __future__ import annotations

import http.server
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
IMAGE = ROOT / "hermes-sandbox"


def test_every_dependency_is_pinned_by_hash() -> None:
    requirements = (IMAGE / "requirements.txt").read_text()
    pins = re.findall(r"^([A-Za-z0-9_.-]+(?:\[[^\]]+\])?)==([^ \\\n]+)", requirements, re.M)
    blocks = [block for block in re.split(r"\n(?=[A-Za-z0-9_.-]+==)", requirements) if "==" in block]

    assert ("mcp", "2.3.0") in pins and ("aiohttp", "3.14.5") in pins
    assert len(pins) == len(blocks) > 50
    assert all("--hash=sha256:" in block for block in blocks)
    assert "git+" not in requirements
    assert not re.search(r"^hermes-agent\b", requirements, re.M)


def test_data_libraries_are_pinned_by_hash_without_repeating_hermes_pins() -> None:
    data = (IMAGE / "requirements-data.txt").read_text()
    base = (IMAGE / "requirements.txt").read_text()
    pattern = r"^([A-Za-z0-9_.-]+)=="
    names = re.findall(pattern, data, re.M)
    blocks = [block for block in re.split(r"\n(?=[A-Za-z0-9_.-]+==)", data) if "==" in block]

    assert {"matplotlib", "pandas", "openpyxl", "numpy", "python-pptx", "python-docx", "pypdf", "playwright"} <= set(names)
    assert len(names) == len(blocks) and all("--hash=sha256:" in block for block in blocks)
    assert not set(names) & {name.lower() for name in re.findall(pattern, base, re.M)}
    assert "git+" not in data
    dockerfile = (IMAGE / "Dockerfile").read_text()
    assert "-r /tmp/requirements.txt -r /tmp/requirements-data.txt" in dockerfile
    assert "MPLBACKEND=Agg" in dockerfile and "MPLCONFIGDIR=/tmp/matplotlib" in dockerfile


def test_node_is_the_official_release_verified_per_architecture() -> None:
    dockerfile = (IMAGE / "Dockerfile").read_text()

    assert re.search(r"^ARG NODE_VERSION=24\.\d+\.\d+$", dockerfile, re.M)
    for arch in ("AMD64", "ARM64"):
        assert re.search(rf"^ARG NODE_SHA256_{arch}=[0-9a-f]{{64}}$", dockerfile, re.M)
    assert '"https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${arch}.tar.xz"' in dockerfile
    assert 'echo "${sha256}  /tmp/node.tar.xz" | sha256sum -c -' in dockerfile


def test_web_tooling_keeps_caches_and_browsers_out_of_snapshots_and_the_network() -> None:
    dockerfile = (IMAGE / "Dockerfile").read_text()
    start = (IMAGE / "start.sh").read_text()

    assert "chromium-headless-shell" in dockerfile
    assert "PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium-headless-shell" in dockerfile
    # npm packages never download a browser the egress proxy would refuse.
    assert "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1" in dockerfile and "PUPPETEER_SKIP_DOWNLOAD=true" in dockerfile
    # Caches sit in .cache, which snapshots leave out; global installs in the workspace.
    for setting in ("NPM_CONFIG_CACHE=/workspace/.cache/npm", "PIP_CACHE_DIR=/workspace/.cache/pip", "NPM_CONFIG_PREFIX=/workspace/.local"):
        assert setting in dockerfile
    # The proxy, with its token, reaches npm through the environment, never a file.
    assert 'NPM_CONFIG_HTTPS_PROXY="$proxy"' in start and ".npmrc" not in start


def test_image_installs_by_hash_and_runs_unprivileged_without_compilers() -> None:
    dockerfile = (IMAGE / "Dockerfile").read_text()

    assert "FROM python:3.14-slim" in dockerfile
    assert "pip install --no-cache-dir --require-hashes --no-deps --only-binary=:all:" in dockerfile
    # Hermes's own source tree, from its tag archive, verified by hash.
    assert re.search(r"^ARG HERMES_SHA256=[0-9a-f]{64}$", dockerfile, re.M)
    assert 'sha256sum -c -' in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert not re.search(r"build-essential|\bgcc\b|\bg\+\+|\bmake\b", dockerfile)
    assert 'ENTRYPOINT ["/usr/local/bin/hermes-sandbox-start"]' in dockerfile


def test_start_script_keeps_secrets_out_of_the_container_environment() -> None:
    start = (IMAGE / "start.sh").read_text()

    assert 'NO_PROXY="hermes-egress,localhost,127.0.0.1"' in start
    assert "HTTP_PROXY=" not in start
    assert "exec hermes gateway run" in start


def test_image_is_built_and_published_with_the_others() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    publish = (ROOT / ".github" / "workflows" / "publish-images.yml").read_text()
    manifest = json.loads((ROOT / "release" / "oss-manifest.json").read_text())

    assert " hermes-sandbox " in ci
    assert "{ name: aurum-ai-hermes-sandbox, context: hermes-sandbox }" in publish
    assert "hermes-sandbox/**" in manifest["include"]


class FakeHermes(http.server.BaseHTTPRequestHandler):
    runs: list[dict] = []
    statuses: dict[str, str] = {}
    # "gone": the run's event queue was consumed by an earlier subscriber;
    # "stalled": another subscriber holds it, so only keepalives arrive.
    events_mode = "normal"
    polls: list[str] = []
    clarified: list[tuple[str, dict]] = []

    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.headers.get("Authorization") != "Bearer relay-key":
            return self._send(401, b"{}")
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.endswith("/clarify"):
            FakeHermes.clarified.append((self.path, body))
            if body["request_id"] == "settled":
                return self._send(409, b'{"error": {"code": "stale_generation"}}')
            return self._send(200, b'{"status": "resolved"}')
        FakeHermes.runs.append(body)
        self._send(202, json.dumps({"run_id": f"run_{len(FakeHermes.runs)}", "status": "started"}).encode())

    def do_GET(self) -> None:
        if self.path.endswith("/events"):
            if FakeHermes.events_mode == "gone":
                return self._send(404, b"{}")
            if FakeHermes.events_mode == "stalled":
                return self._send(200, b": keepalive\n\n: keepalive\n\n", "text/event-stream")
            events = [
                ": opened",
                'data: {"event": "tool.started", "tool": "terminal"}',
                "data: not json",
                'data: {"event": "run.completed", "output": "done"}',
                'data: {"event": "after the end"}',
            ]
            return self._send(200, ("\n".join(events) + "\n").encode(), "text/event-stream")
        run_id = self.path.rsplit("/", 1)[-1]
        status = FakeHermes.polls.pop(0) if FakeHermes.polls else FakeHermes.statuses.get(run_id, "completed")
        if status == "missing":
            return self._send(404, b"{}")
        self._send(200, json.dumps({"status": status, "output": "done", "usage": {"total_tokens": 3}}).encode())


@pytest.fixture
def relay(tmp_path: Path):  # noqa: ANN201
    FakeHermes.runs, FakeHermes.statuses, FakeHermes.events_mode, FakeHermes.polls = [], {}, "normal", []
    FakeHermes.clarified = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHermes)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(json.dumps({"platforms": {"api_server": {"extra": {"key": "relay-key"}}}}))
    env = {
        **os.environ,
        "HERMES_HOME": str(home),
        "HERMES_RELAY_STATE": str(tmp_path / ".aurum"),
        "HERMES_RELAY_API": f"http://127.0.0.1:{server.server_address[1]}",
        "HERMES_RELAY_POLL_SECONDS": "0.05",
        "HERMES_RELAY_BUNDLED_SKILLS": str(tmp_path / "bundled"),
    }

    def run(*args: str) -> list[dict]:
        result = subprocess.run(
            [sys.executable, str(IMAGE / "hermes_relay.py"), *args], env=env, text=True, capture_output=True, timeout=30
        )
        return [json.loads(line) for line in result.stdout.splitlines()]

    yield run, tmp_path
    server.shutdown()


def _request(tmp_path: Path, key: str) -> str:
    path = tmp_path / f"request-{key}.json"
    path.write_text(json.dumps({"key": key, "input": "hi", "session_id": "conv"}))
    return str(path)


def test_relay_streams_events_and_the_final_status(relay) -> None:
    run, tmp_path = relay

    lines = run("run", _request(tmp_path, "turn-1"))

    assert [line["type"] for line in lines] == ["started", "event", "event", "result"]
    assert lines[-1] == {"type": "result", "run_id": "run_1", "status": "completed", "output": "done", "usage": {"total_tokens": 3}}
    assert FakeHermes.runs == [{"input": "hi", "session_id": "conv"}]


def test_a_retried_run_attaches_instead_of_starting_again(relay) -> None:
    run, tmp_path = relay

    run("run", _request(tmp_path, "turn-1"))
    again = run("run", _request(tmp_path, "turn-1"))
    other = run("run", _request(tmp_path, "turn-2"))

    assert again[0] == {"type": "started", "run_id": "run_1"}
    assert len(FakeHermes.runs) == 2
    assert other[-1]["status"] == "completed"
    assert not (tmp_path / "request-turn-1.json").exists()


def test_a_reattach_after_the_events_were_consumed_polls_for_the_result(relay) -> None:
    run, tmp_path = relay
    run("run", _request(tmp_path, "turn-1"))
    FakeHermes.events_mode = "gone"
    FakeHermes.polls = ["running", "running", "completed"]

    lines = run("run", _request(tmp_path, "turn-1"))

    assert [line["type"] for line in lines] == ["started", "result"]
    assert lines[-1]["status"] == "completed" and lines[-1]["output"] == "done"
    assert len(FakeHermes.runs) == 1


def test_a_stream_held_by_another_subscriber_ends_once_the_run_has(relay) -> None:
    run, tmp_path = relay
    FakeHermes.events_mode = "stalled"
    FakeHermes.polls = ["running", "completed"]

    lines = run("run", _request(tmp_path, "turn-1"))

    assert [line["type"] for line in lines] == ["started", "result"]
    assert lines[-1]["status"] == "completed"


def test_clarify_answers_a_pending_question_from_a_file_it_removes(relay, tmp_path: Path) -> None:
    run, _ = relay
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps({"request_id": "p1", "execution_generation": 1, "answer": "Rome"}))
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps({"request_id": "settled", "execution_generation": 1, "answer": "x"}))

    assert run("clarify", "run_1", str(answer)) == [{"type": "clarify", "status": 200}]
    assert run("clarify", "run_1", str(stale)) == [{"type": "clarify", "status": 409}]
    assert FakeHermes.clarified[0] == (
        "/v1/runs/run_1/clarify",
        {"request_id": "p1", "execution_generation": 1, "answer": "Rome"},
    )
    assert not answer.exists() and not stale.exists()


def test_memory_is_written_and_read_back_only_from_regular_files(relay, tmp_path: Path) -> None:
    run, _ = relay
    memories = tmp_path / ".hermes" / "memories"
    request = tmp_path / "memory.json"
    request.write_text(json.dumps({"memory": "repo uses uv", "user": "likes tea"}))

    assert run("memory-write", str(request)) == [{"type": "memory-written"}]
    assert not request.exists()
    assert (memories / "MEMORY.md").read_text() == "repo uses uv"
    assert oct((memories / "USER.md").stat().st_mode & 0o777) == "0o600"
    assert run("memory-read") == [{"type": "memory", "memory": "repo uses uv", "user": "likes tea"}]

    # The terminal can replace the files: a link, a FIFO or invalid text is not memory.
    (memories / "USER.md").unlink()
    (memories / "USER.md").symlink_to(tmp_path / ".hermes" / "config.yaml")
    (memories / "MEMORY.md").write_bytes(b"\xff\xfe not text")
    assert run("memory-read") == [{"type": "memory", "memory": None, "user": None}]
    (memories / "USER.md").unlink()
    os.mkfifo(memories / "USER.md")
    (memories / "MEMORY.md").write_text("x" * 100_000)
    [read] = run("memory-read")
    assert read["user"] is None and read["memory"] is None


def _skill(directory: Path, body: str = "Steps.", **extra: str) -> None:
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(f"---\nname: {directory.name}\ndescription: d\n---\n{body}\n")
    for name, text in extra.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(text)


def test_skills_read_returns_only_what_differs_from_the_bundled_skills(relay, tmp_path: Path) -> None:
    run, _ = relay
    skills, bundled = tmp_path / ".hermes" / "skills", tmp_path / "bundled"
    assert run("skills-read") == [{"type": "skills", "count": 0, "skipped": 0}]

    for root in (skills, bundled):
        _skill(root / "research" / "arxiv")
        _skill(root / "notes" / "standup", "Bundled steps.")
    (skills / ".bundled_manifest").write_text("arxiv:abc")
    (skills / ".hub").mkdir()
    (skills / "notes" / "standup" / "SKILL.md").write_text("---\nname: standup\ndescription: d\n---\nChanged.\n")
    _skill(skills / "mine", "Mine.", **{"scripts/run.py": "print(1)\n"})
    (skills / "mine" / "__pycache__").mkdir()
    (skills / "mine" / "__pycache__" / "run.pyc").write_bytes(b"\x00\x01")

    *found, summary = run("skills-read")

    assert summary == {"type": "skills", "count": 2, "skipped": 0}
    assert {item["path"]: {f["path"]: f["content"] for f in item["files"]} for item in found} == {
        "mine": {"SKILL.md": "---\nname: mine\ndescription: d\n---\nMine.\n", "scripts/run.py": "print(1)\n"},
        "notes/standup": {"SKILL.md": "---\nname: standup\ndescription: d\n---\nChanged.\n"},
    }


def test_skills_read_skips_links_binaries_and_oversized_skills(relay, tmp_path: Path) -> None:
    run, _ = relay
    skills = tmp_path / ".hermes" / "skills"
    _skill(skills / "linked", **{"config.yaml": "x"})
    (skills / "linked" / "config.yaml").unlink()
    (skills / "linked" / "config.yaml").symlink_to(tmp_path / ".hermes" / "config.yaml")
    _skill(skills / "binary")
    (skills / "binary" / "blob.bin").write_bytes(b"\x7fELF\x00")
    _skill(skills / "big", **{"a.md": "x" * 60_000, "b.md": "y" * 60_000})
    _skill(skills / "elsewhere")
    (skills / "outside").symlink_to(skills / "elsewhere", target_is_directory=True)

    *found, summary = run("skills-read")

    assert [item["path"] for item in found] == ["elsewhere"]
    assert summary == {"type": "skills", "count": 1, "skipped": 3}


def test_skills_read_reads_the_most_recently_changed_skills_first(relay, tmp_path: Path) -> None:
    run, _ = relay
    skills = tmp_path / ".hermes" / "skills"
    for index in range(17):
        _skill(skills / f"s{index:02d}")
        os.utime(skills / f"s{index:02d}" / "SKILL.md", (1_000 + index, 1_000 + index))
        os.utime(skills / f"s{index:02d}", (1_000 + index, 1_000 + index))
    # The alphabetically first skill changed last.
    (skills / "s00" / "notes.md").write_text("new")

    *found, summary = run("skills-read")

    assert found[0]["path"] == "s00" and "s01" not in [item["path"] for item in found]
    assert summary == {"type": "skills", "count": 16, "skipped": 1}


def _staged(tmp_path: Path, name: str = "batch") -> Path:
    staging = tmp_path / ".aurum" / "hydrate" / name
    staging.mkdir(parents=True)
    return staging


def test_skills_hydrate_replaces_the_platform_skills_read_only(relay, tmp_path: Path) -> None:
    run, _ = relay
    platform = tmp_path / ".aurum" / "platform-skills"
    staging = _staged(tmp_path)
    _skill(staging / "standup-1a2b3c4d", **{"scripts/run.sh": "echo hi\n"})
    (staging / "standup-1a2b3c4d" / "scripts" / "run.sh").chmod(0o755)

    assert run("skills-hydrate", str(staging)) == [{"type": "skills-hydrated", "count": 1}]
    hydrated = platform / "standup-1a2b3c4d"
    assert not staging.exists()
    assert oct(hydrated.stat().st_mode & 0o777) == "0o555"
    assert oct((hydrated / "SKILL.md").stat().st_mode & 0o777) == "0o444"
    assert oct((hydrated / "scripts" / "run.sh").stat().st_mode & 0o777) == "0o555"

    # A revoked skill is gone on the next hydration, as is what the agent added.
    (platform / "added").mkdir()
    assert run("skills-hydrate", str(_staged(tmp_path, "next"))) == [{"type": "skills-hydrated", "count": 0}]
    assert list(platform.iterdir()) == []


def test_skills_hydrate_follows_no_link_the_agent_placed(relay, tmp_path: Path) -> None:
    run, _ = relay
    project = tmp_path / "home" / "project"
    project.mkdir(parents=True)
    (project / "kept.txt").write_text("user data")
    platform = tmp_path / ".aurum" / "platform-skills"
    platform.parent.mkdir(parents=True)
    platform.symlink_to(project, target_is_directory=True)

    assert run("skills-hydrate", str(_staged(tmp_path))) == [{"type": "skills-hydrated", "count": 0}]
    assert (project / "kept.txt").read_text() == "user data"
    assert platform.is_dir() and not platform.is_symlink()

    (platform / "linked").symlink_to(project, target_is_directory=True)
    run("skills-hydrate", str(_staged(tmp_path, "again")))
    assert (project / "kept.txt").exists() and not (platform / "linked").exists()

    stale = _staged(tmp_path, "stale")
    (stale / "big.bin").write_bytes(b"x" * 1000)
    run("skills-hydrate", str(_staged(tmp_path, "latest")))
    assert list((tmp_path / ".aurum" / "hydrate").iterdir()) == []

    outside = tmp_path / "outside"
    outside.mkdir()
    assert run("skills-hydrate", str(outside))[0]["code"] == "hermes_skills_staging_invalid"


def test_skills_read_skips_unchanged_copies_of_platform_skills(relay, tmp_path: Path) -> None:
    run, _ = relay
    _skill(tmp_path / ".aurum" / "platform-skills" / "standup-1a2b3c4d")
    skills = tmp_path / ".hermes" / "skills"
    _skill(skills / "standup-1a2b3c4d")
    _skill(skills / "changed" / "standup-1a2b3c4d", "Changed.")

    *found, _ = run("skills-read")

    assert [item["path"] for item in found] == ["changed/standup-1a2b3c4d"]


def test_skills_read_takes_no_linked_skills_directory(relay, tmp_path: Path) -> None:
    run, _ = relay
    _skill(tmp_path / "real" / "mine")
    (tmp_path / ".hermes" / "skills").symlink_to(tmp_path / "real", target_is_directory=True)

    assert run("skills-read") == [{"type": "skills", "count": 0, "skipped": 0}]


def test_active_lists_the_recent_runs_hermes_still_runs(relay) -> None:
    run, tmp_path = relay
    assert run("active") == [{"type": "active", "run_ids": []}]
    for key in ("turn-1", "turn-2", "turn-3"):
        run("run", _request(tmp_path, key))
    FakeHermes.statuses = {"run_1": "running", "run_2": "missing", "run_3": "queued"}
    old = tmp_path / ".aurum" / "runs" / "stale"
    old.write_text("run_9")
    os.utime(old, (0, 0))
    FakeHermes.statuses["run_9"] = "running"

    [line] = run("active")

    assert line["type"] == "active" and sorted(line["run_ids"]) == ["run_1", "run_3"]


def _file_bytes(lines: list[dict]) -> bytes:
    import base64

    return b"".join(base64.b64decode(line["data"]) for line in lines if line["type"] == "chunk")


def test_file_read_streams_a_workspace_file_in_chunks(relay, tmp_path: Path) -> None:
    import hashlib

    run, _ = relay
    data = os.urandom(300 * 1024)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "chart.png").write_bytes(data)

    for path in ("out/chart.png", f"{tmp_path}/out/chart.png", "./out//chart.png"):
        lines = run("file-read", path)
        assert len([line for line in lines if line["type"] == "chunk"]) == 4
        assert all(len(json.dumps(line)) < 256 * 1024 for line in lines)
        assert _file_bytes(lines) == data
        assert lines[-1] == {"type": "file", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def test_file_read_refuses_what_leaves_the_workspace_or_is_not_a_plain_file(relay, tmp_path: Path) -> None:
    run, _ = relay
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir()
    (outside / "secret").write_text("s")
    (tmp_path / ".aurum").mkdir(exist_ok=True)
    (tmp_path / ".aurum" / "proxy").write_text("token")
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    (tmp_path / "link.txt").symlink_to(outside / "secret")
    os.link(tmp_path / ".aurum" / "proxy", tmp_path / "hard.txt")
    os.mkfifo(tmp_path / "pipe")
    (tmp_path / "dir").mkdir()

    refused = {
        "../" + outside.name + "/secret": "file_not_allowed",
        str(outside / "secret"): "file_not_allowed",
        "linked/secret": "file_not_allowed",
        "link.txt": "file_not_allowed",
        "hard.txt": "file_not_allowed",
        "pipe": "file_not_allowed",
        "dir": "file_not_allowed",
        ".aurum/proxy": "file_not_allowed",
        ".hermes/config.yaml": "file_not_allowed",
        ".hermes/backups/config.yaml": "file_not_allowed",
        "missing.csv": "file_not_found",
        "": "file_not_allowed",
    }
    for path, code in refused.items():
        assert run("file-read", path) == [{"type": "file-refused", "code": code}], path


def test_file_read_refuses_a_file_above_the_limit(relay, tmp_path: Path) -> None:
    run, _ = relay
    with open(tmp_path / "big.bin", "wb") as big:
        big.truncate(20 * 1024 * 1024 + 1)
    assert run("file-read", "big.bin") == [{"type": "file-refused", "code": "file_too_large"}]


def test_file_read_leaves_out_what_snapshots_leave_out() -> None:
    def excluded(source: str) -> list[str]:
        start = source.index("EXCLUDED = (")
        return re.findall(r'^\s*"([^"]+)",', source[start : source.index("\n)", start)], re.M)

    snapshots = excluded((ROOT / "skill-executor" / "src" / "hermes_snapshots.py").read_text())
    assert ".aurum" in snapshots and ".hermes/config.yaml" in snapshots
    assert excluded((IMAGE / "hermes_relay.py").read_text()) == snapshots


def test_inputs_are_moved_in_read_only_and_listed_with_their_hash(relay, tmp_path: Path) -> None:
    import hashlib

    run, _ = relay
    staging = tmp_path / ".aurum" / "inputs" / "batch"
    staging.mkdir(parents=True)
    (staging / "sales q1.xlsx").write_bytes(b"PK workbook")
    (staging / "notes.md").write_text("# Notes\n")
    (staging / "nested").mkdir()
    stale = tmp_path / ".aurum" / "inputs" / "stale"
    stale.mkdir()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "notes.md").write_text("an older version")
    (inputs / "agent.txt").write_text("the agent's own")

    assert run("inputs-write", str(staging)) == [{"type": "inputs-written", "count": 2}]

    assert not staging.exists() and not stale.exists()
    assert (inputs / "notes.md").read_text() == "# Notes\n"
    assert oct((inputs / "sales q1.xlsx").stat().st_mode & 0o777) == "0o444"
    assert (inputs / "agent.txt").read_text() == "the agent's own"
    (inputs / "link.csv").symlink_to(tmp_path / ".hermes" / "config.yaml")
    os.link(inputs / "agent.txt", inputs / "hard.txt")
    assert run("inputs-list") == [
        {
            "type": "inputs",
            "files": {
                "notes.md": hashlib.sha256(b"# Notes\n").hexdigest(),
                "sales q1.xlsx": hashlib.sha256(b"PK workbook").hexdigest(),
            },
        }
    ]


def test_inputs_write_takes_only_its_staging_and_replaces_a_linked_folder(relay, tmp_path: Path) -> None:
    run, _ = relay
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert run("inputs-write", str(elsewhere)) == [{"type": "error", "code": "hermes_inputs_staging_invalid"}]

    staging = tmp_path / ".aurum" / "inputs" / "batch"
    staging.mkdir(parents=True)
    (staging / "data.csv").write_text("a,b\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "inputs").symlink_to(outside, target_is_directory=True)

    assert run("inputs-write", str(staging)) == [{"type": "inputs-written", "count": 1}]
    assert not (tmp_path / "inputs").is_symlink() and (tmp_path / "inputs" / "data.csv").exists()
    assert list(outside.iterdir()) == []
    assert run("inputs-list")[0]["files"].keys() == {"data.csv"}
