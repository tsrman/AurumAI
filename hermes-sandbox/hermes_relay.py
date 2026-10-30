#!/usr/bin/env python3
"""The only interface skill-executor uses to talk to Hermes in its sandbox.

It runs through `docker exec`, so the sandbox opens no port to the platform.
It speaks to Hermes's API server on 127.0.0.1 and prints one JSON object per
line. Everything Hermes says is data for the caller to check, not trusted:
the agent can stop the server and answer in its place.

  health           exit 0 once the API server answers
  run FILE         FILE (placed by skill-executor, then removed) holds
                   {"input", "session_id", "key", "conversation_history"?};
                   starts the run once per key, then streams it
  events RUN_ID    replays and follows a run's events
  stop RUN_ID      asks Hermes to stop a run
  clarify RUN FILE answers a run's pending question; FILE (placed by
                   skill-executor, then removed) holds {"request_id",
                   "execution_generation", "answer"}
  active           the recent runs Hermes still reports as queued or running
  memory-write FILE  writes the user's memory files; FILE (placed by
                   skill-executor, then removed) holds {"memory", "user"}
  memory-read      the memory files as Hermes left them: regular files only,
                   bounded, valid UTF-8, else null
  skills-read      the skills Hermes holds beyond the image's bundled ones and
                   unchanged copies of the platform's, one line each: bounded
                   text files only, links never followed
  skills-hydrate DIR replaces the platform skills with those in DIR (placed by
                   skill-executor under .aurum/hydrate, then removed): an
                   empty DIR removes them all
  inputs-list      the files in the workspace's inputs folder, the user's
                   uploads, with their SHA-256: regular files with one link
  inputs-write DIR moves the uploads in DIR (placed by skill-executor under
                   .aurum/inputs, then removed) into the inputs folder,
                   read-only, replacing files of the same name
  file-read PATH   a file the agent attaches to its answer, in base64 chunks
                   and a closing line with its size and SHA-256: a regular
                   file in the workspace, reached through no link, outside
                   what snapshots leave out, at most FILE_MAX_BYTES
"""

import base64
import hashlib
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.request

HERMES_HOME = os.environ.get("HERMES_HOME", "/workspace/.hermes")
STATE = os.environ.get("HERMES_RELAY_STATE", "/workspace/.aurum")
API = os.environ.get("HERMES_RELAY_API", "http://127.0.0.1:8642")
MAX_LINE = 256 * 1024
TERMINAL = {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}
ACTIVE = {"queued", "running", "waiting_for_approval"}
# A run not yet settled: still active, or asked to stop.
UNSETTLED = ACTIVE | {"stopping"}
WORKSPACE = os.environ.get("HERMES_RELAY_WORKSPACE", os.path.dirname(HERMES_HOME))
# As skill-executor's hermes_snapshots.EXCLUDED: secrets and per-user state.
EXCLUDED = (
    ".aurum",
    ".hermes/config.yaml",
    ".hermes/backups",
    ".hermes/.env",
    ".hermes/auth.json",
    ".hermes/logs",
    ".hermes/memories",
    ".hermes/skills",
    # Package caches (pip, npm): a second copy of what is installed.
    ".cache",
)
FILE_MAX_BYTES = 20 * 1024 * 1024
# Base64 of a chunk, in its JSON line, stays well below MAX_LINE.
FILE_CHUNK = 96 * 1024
POLL_SECONDS = float(os.environ.get("HERMES_RELAY_POLL_SECONDS", "2"))
# Only runs started within the longest turn deadline, newest first, are asked about.
RECENT_SECONDS = 86400
RECENT_RUNS = 32


def emit(kind: str, **fields: object) -> None:
    sys.stdout.write(json.dumps({"type": kind, **fields}, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def api_key() -> str:
    with open(os.path.join(HERMES_HOME, "config.yaml")) as config:
        return str(json.load(config)["platforms"]["api_server"]["extra"]["key"])


def request(method: str, path: str, body: object | None = None, timeout: float = 30):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}
    return urllib.request.urlopen(urllib.request.Request(API + path, data=data, headers=headers, method=method), timeout=timeout)


def write_atomic(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    temporary = f"{path}.{os.getpid()}.tmp"
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as out:
        out.write(text)
    os.replace(temporary, path)


def run_status(run_id: str) -> dict:
    with request("GET", f"/v1/runs/{run_id}") as response:
        return json.load(response)


def stream_events(run_id: str) -> None:
    """Hermes hands a run's events to one subscriber, once, and drops the queue
    when that subscriber leaves: a reattach finds no events (404), or a queue
    an earlier subscriber still reads, so only keepalives arrive. Either way
    the run's status decides when it is over."""
    try:
        stream = request("GET", f"/v1/runs/{run_id}/events", timeout=None)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return
        raise
    with stream:
        for raw in stream:
            line = raw[:MAX_LINE].decode("utf-8", "replace").rstrip("\n")
            if line == ": keepalive" and run_status(run_id).get("status") not in UNSETTLED:
                return
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:])
            except ValueError:
                continue
            if isinstance(event, dict):
                emit("event", event=event)
                if event.get("event") in TERMINAL:
                    return


def follow(run_id: str) -> int:
    stream_events(run_id)
    status = run_status(run_id)
    while status.get("status") in UNSETTLED:
        time.sleep(POLL_SECONDS)
        status = run_status(run_id)
    emit("result", run_id=run_id, status=status.get("status"), output=status.get("output"), usage=status.get("usage"))
    return 0


def run(path: str) -> int:
    with open(path) as request_file:
        body = json.load(request_file)
    os.unlink(path)
    marker = os.path.join(STATE, "runs", hashlib.sha256(str(body["key"]).encode()).hexdigest())
    try:
        with open(marker) as existing:
            run_id = existing.read().strip()
    except FileNotFoundError:
        payload = {"input": body["input"], "session_id": body["session_id"]}
        if body.get("conversation_history"):
            payload["conversation_history"] = body["conversation_history"]
        with request("POST", "/v1/runs", payload) as response:
            run_id = str(json.load(response)["run_id"])
        # Hermes 0.19 does not replay Idempotency-Key; a retried activity
        # finds this marker and attaches to the run it already started.
        write_atomic(marker, run_id)
    emit("started", run_id=run_id)
    return follow(run_id)


def clarify(run_id: str, path: str) -> int:
    with open(path) as answer_file:
        body = json.load(answer_file)
    os.unlink(path)
    try:
        with request("POST", f"/v1/runs/{run_id}/clarify", body) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        # 409: the question was settled meanwhile (answered, timed out, run ended).
        status = exc.code
    emit("clarify", status=status)
    return 0 if status < 300 else 3


MEMORY_FILES = {"memory": "MEMORY.md", "user": "USER.md"}
# Well above Hermes's character limits; the platform cuts to them.
MEMORY_MAX_BYTES = 32 * 1024


def memory_write(path: str) -> int:
    with open(path) as request_file:
        body = json.load(request_file)
    os.unlink(path)
    directory = os.path.join(HERMES_HOME, "memories")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    for kind, name in MEMORY_FILES.items():
        # os.replace swaps the name itself, never a file a link points to.
        write_atomic(os.path.join(directory, name), str(body.get(kind) or ""))
    emit("memory-written")
    return 0


def _read_memory(path: str) -> str | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as memory_file:
        if not stat.S_ISREG(os.fstat(memory_file.fileno()).st_mode):
            return None
        data = memory_file.read(MEMORY_MAX_BYTES + 1)
    if len(data) > MEMORY_MAX_BYTES:
        return None
    try:
        return data.decode("utf-8").replace("\x00", "")
    except UnicodeDecodeError:
        return None


def memory_read() -> int:
    directory = os.path.join(HERMES_HOME, "memories")
    emit("memory", **{kind: _read_memory(os.path.join(directory, name)) for kind, name in MEMORY_FILES.items()})
    return 0


# business-backend's hermes_skill_drafts keeps to the same bounds.
SKILLS_MAX = 16
SKILL_MAX_FILES = 32
SKILL_FILE_MAX_BYTES = 64 * 1024
SKILL_MAX_BYTES = 96 * 1024
SKILL_MAX_DEPTH = 4
# Skill directories looked at, bundled ones included (Hermes 0.22.5 ships 58).
SKILL_DIRS_MAX = 512
BUNDLED_SKILLS = os.environ.get("HERMES_RELAY_BUNDLED_SKILLS", "/opt/hermes/skills")
# One of Hermes's skills.external_dirs; skill-executor creates it with the config.
PLATFORM_SKILLS = os.path.join(STATE, "platform-skills")
_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Caches and environments, which Hermes's skill scanners skip too.
_IGNORED = {"__pycache__", "node_modules", "venv", "site-packages"}
_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class Unusable(Exception):
    """A skill that is not a bounded set of text files."""


def _ignored(name: str) -> bool:
    return name.startswith(".") or name in _IGNORED


def _is_dir(path: str) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _text_file(path: str) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise Unusable() from None
    with os.fdopen(descriptor, "rb") as skill_file:
        if not stat.S_ISREG(os.fstat(skill_file.fileno()).st_mode):
            raise Unusable()
        data = skill_file.read(SKILL_FILE_MAX_BYTES + 1)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Unusable() from None
    if len(data) > SKILL_FILE_MAX_BYTES or _TEXT.search(text):
        raise Unusable()
    return text


def _skill_files(directory: str) -> dict[str, str]:
    files: dict[str, str] = {}
    size = 0

    def walk(path: str, prefix: str, depth: int) -> None:
        nonlocal size
        for entry in sorted(os.scandir(path), key=lambda item: item.name):
            if _ignored(entry.name):
                continue
            relative = f"{prefix}{entry.name}"
            if not _PART.match(entry.name) or depth > SKILL_MAX_DEPTH:
                raise Unusable()
            if entry.is_dir(follow_symlinks=False):
                walk(entry.path, f"{relative}/", depth + 1)
                continue
            files[relative] = _text_file(entry.path)
            size += len(files[relative].encode())
            if len(files) > SKILL_MAX_FILES or size > SKILL_MAX_BYTES:
                raise Unusable()

    walk(directory, "", 1)
    return files


def _skill_dirs(root: str):  # noqa: ANN202
    """Directories holding a SKILL.md; a skill's own subdirectories belong to it."""

    def walk(path: str, prefix: str, depth: int):  # noqa: ANN202
        for entry in sorted(os.scandir(path), key=lambda item: item.name):
            if _ignored(entry.name) or not _PART.match(entry.name) or not entry.is_dir(follow_symlinks=False):
                continue
            relative = f"{prefix}{entry.name}"
            if os.path.lexists(os.path.join(entry.path, "SKILL.md")):
                yield relative, entry.path
            elif depth < SKILL_MAX_DEPTH:
                yield from walk(entry.path, f"{relative}/", depth + 1)

    yield from walk(root, "", 1)


def _bundled(relative: str) -> dict[str, str] | None:
    directory = os.path.join(BUNDLED_SKILLS, relative)
    if not _is_dir(directory):
        return None
    try:
        return _skill_files(directory)
    except Unusable:
        return None


def _newest(directory: str, depth: int = 1) -> float:
    """The latest change in a skill's tree, links not followed."""
    try:
        newest = os.lstat(directory).st_mtime
        for entry in os.scandir(directory):
            if _ignored(entry.name):
                continue
            newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
            if depth < SKILL_MAX_DEPTH and entry.is_dir(follow_symlinks=False):
                newest = max(newest, _newest(entry.path, depth + 1))
    except OSError:
        # Changed while read; the skill is read, or skipped, after the others.
        return 0.0
    return newest


def _platform_copies() -> list[dict[str, str]]:
    if not _is_dir(PLATFORM_SKILLS):
        return []
    copies = []
    for _, directory in _skill_dirs(PLATFORM_SKILLS):
        try:
            copies.append(_skill_files(directory))
        except Unusable:
            continue
    return copies


def skills_read() -> int:
    root = os.path.join(HERMES_HOME, "skills")
    count = skipped = 0
    platform = _platform_copies()
    if _is_dir(root):
        found = [(relative, directory) for _, (relative, directory) in zip(range(SKILL_DIRS_MAX), _skill_dirs(root))]
        # Most recently changed first: skills drafted on earlier turns, read
        # again every turn, do not crowd out a new one.
        found.sort(key=lambda item: _newest(item[1]), reverse=True)
        for relative, directory in found:
            try:
                files = _skill_files(directory)
            except Unusable:
                skipped += 1
                continue
            # Copied from the image or the platform unchanged: not the agent's.
            if files == _bundled(relative) or files in platform:
                continue
            if count == SKILLS_MAX:
                skipped += 1
                continue
            count += 1
            line = {"type": "skill", "path": relative, "files": [{"path": name, "content": text} for name, text in files.items()]}
            sys.stdout.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
    emit("skills", count=count, skipped=skipped)
    return 0


def _remove(path: str) -> None:
    """Remove a path the agent may have replaced: a link or file is unlinked,
    never followed; a directory is made writable again, as its owner, and removed."""
    mode = os.lstat(path).st_mode
    if not stat.S_ISDIR(mode):
        os.unlink(path)
        return
    os.chmod(path, 0o700)
    for entry in os.scandir(path):
        _remove(entry.path)
    os.rmdir(path)


def _read_only(path: str) -> None:
    for entry in os.scandir(path):
        mode = entry.stat(follow_symlinks=False).st_mode
        if stat.S_ISDIR(mode):
            _read_only(entry.path)
        elif stat.S_ISREG(mode):
            os.chmod(entry.path, 0o555 if mode & 0o111 else 0o444)
        else:
            os.unlink(entry.path)
    os.chmod(path, 0o555)


def skills_hydrate(staging: str) -> int:
    """Hermes reads the platform skills as an external directory, whose real
    path it keeps: the directory stays, its entries are replaced."""
    # The agent can replace .aurum itself: nothing is removed through a link.
    hydrate = os.path.join(STATE, "hydrate")
    if not (_is_dir(STATE) and _is_dir(hydrate) and _is_dir(staging)) or os.path.dirname(os.path.abspath(staging)) != hydrate:
        emit("error", code="hermes_skills_staging_invalid")
        return 3
    # Staging left by an earlier hydration that failed would fill the workspace.
    for entry in os.scandir(hydrate):
        if entry.path != os.path.abspath(staging):
            _remove(entry.path)
    if os.path.lexists(PLATFORM_SKILLS) and not _is_dir(PLATFORM_SKILLS):
        os.unlink(PLATFORM_SKILLS)
    os.makedirs(PLATFORM_SKILLS, mode=0o755, exist_ok=True)
    for entry in os.scandir(PLATFORM_SKILLS):
        _remove(entry.path)
    names = [entry.name for entry in os.scandir(staging) if _PART.match(entry.name) and entry.is_dir(follow_symlinks=False)]
    for name in names:
        os.rename(os.path.join(staging, name), os.path.join(PLATFORM_SKILLS, name))
        _read_only(os.path.join(PLATFORM_SKILLS, name))
    _remove(staging)
    emit("skills-hydrated", count=len(names))
    return 0


INPUTS = os.path.join(WORKSPACE, "inputs")
INPUTS_MAX = 100


def _input_name(name: str) -> bool:
    return 0 < len(name.encode()) <= 255 and name not in (".", "..") and "/" not in name and "\x00" not in name


def inputs_list() -> int:
    files: dict[str, str] = {}
    if _is_dir(INPUTS):
        for entry in sorted(os.scandir(INPUTS), key=lambda item: item.name)[:INPUTS_MAX]:
            info = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > FILE_MAX_BYTES:
                continue
            try:
                descriptor = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError:
                continue
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "rb") as source:
                while chunk := source.read(FILE_CHUNK):
                    digest.update(chunk)
            files[entry.name] = digest.hexdigest()
    emit("inputs", files=files)
    return 0


def inputs_write(staging: str) -> int:
    """The inputs folder stays a real directory the platform fills; what the
    agent wrote there besides the uploads is left alone."""
    # The agent can replace .aurum itself: nothing is removed through a link.
    root = os.path.join(STATE, "inputs")
    if not (_is_dir(STATE) and _is_dir(root) and _is_dir(staging)) or os.path.dirname(os.path.abspath(staging)) != root:
        emit("error", code="hermes_inputs_staging_invalid")
        return 3
    # Staging left by an earlier write that failed would fill the workspace.
    for entry in os.scandir(root):
        if entry.path != os.path.abspath(staging):
            _remove(entry.path)
    if os.path.lexists(INPUTS) and not _is_dir(INPUTS):
        os.unlink(INPUTS)
    os.makedirs(INPUTS, mode=0o755, exist_ok=True)
    written = 0
    for entry in os.scandir(staging):
        if not (_input_name(entry.name) and stat.S_ISREG(entry.stat(follow_symlinks=False).st_mode)):
            continue
        target = os.path.join(INPUTS, entry.name)
        if os.path.lexists(target):
            _remove(target)
        os.rename(entry.path, target)
        os.chmod(target, 0o444)
        written += 1
    _remove(staging)
    emit("inputs-written", count=written)
    return 0


def active() -> int:
    runs = os.path.join(STATE, "runs")
    try:
        markers = sorted(os.scandir(runs), key=lambda entry: entry.stat().st_mtime, reverse=True)
    except FileNotFoundError:
        markers = []
    found = []
    for entry in markers[:RECENT_RUNS]:
        if time.time() - entry.stat().st_mtime > RECENT_SECONDS:
            break
        with open(entry.path) as marker:
            run_id = marker.read().strip()
        try:
            with request("GET", f"/v1/runs/{run_id}", timeout=10) as response:
                status = json.load(response).get("status")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        if status in ACTIVE:
            found.append(run_id)
    emit("active", run_ids=found)
    return 0


class Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _workspace_parts(path: str) -> list[str]:
    """The path's components below the workspace; nothing that leaves it."""
    if not path or "\x00" in path or len(path) > 1024:
        raise Refused("file_not_allowed")
    if os.path.isabs(path):
        prefix = WORKSPACE.rstrip("/") + "/"
        if not path.startswith(prefix):
            raise Refused("file_not_allowed")
        path = path[len(prefix):]
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        raise Refused("file_not_allowed")
    relative = "/".join(parts)
    if any(relative == item or relative.startswith(item + "/") for item in EXCLUDED):
        raise Refused("file_not_allowed")
    return parts


def _open_in_workspace(parts: list[str]) -> int:
    """Each component opened relative to the last, links never followed."""
    directory = os.open(WORKSPACE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            except FileNotFoundError:
                raise Refused("file_not_found") from None
            except OSError:
                raise Refused("file_not_allowed") from None
            os.close(directory)
            directory = child
        try:
            return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            raise Refused("file_not_found") from None
        except OSError:
            raise Refused("file_not_allowed") from None
    finally:
        os.close(directory)


def file_read(path: str) -> int:
    try:
        descriptor = _open_in_workspace(_workspace_parts(path))
    except Refused as exc:
        emit("file-refused", code=exc.code)
        return 1
    info = os.fstat(descriptor)
    # A hard link could name an excluded file under another path.
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        emit("file-refused", code="file_not_allowed")
        return 1
    if info.st_size > FILE_MAX_BYTES:
        os.close(descriptor)
        emit("file-refused", code="file_too_large")
        return 1
    with os.fdopen(descriptor, "rb") as source:
        digest, size = hashlib.sha256(), 0
        while chunk := source.read(FILE_CHUNK):
            size += len(chunk)
            if size > FILE_MAX_BYTES:
                emit("file-refused", code="file_too_large")
                return 1
            digest.update(chunk)
            emit("chunk", data=base64.b64encode(chunk).decode())
    emit("file", size=size, sha256=digest.hexdigest())
    return 0


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    if command == "health":
        try:
            with urllib.request.urlopen(API + "/health", timeout=3) as response:
                return 0 if response.status == 200 else 1
        except (OSError, urllib.error.URLError):
            return 1
    if command == "run" and len(argv) == 2:
        return run(argv[1])
    if command == "events" and len(argv) == 2:
        return follow(argv[1])
    if command == "clarify" and len(argv) == 3:
        return clarify(argv[1], argv[2])
    if command == "memory-write" and len(argv) == 2:
        return memory_write(argv[1])
    if command == "memory-read" and len(argv) == 1:
        return memory_read()
    if command == "skills-hydrate" and len(argv) == 2:
        return skills_hydrate(argv[1])
    if command == "skills-read" and len(argv) == 1:
        return skills_read()
    if command == "active" and len(argv) == 1:
        return active()
    if command == "inputs-list" and len(argv) == 1:
        return inputs_list()
    if command == "inputs-write" and len(argv) == 2:
        return inputs_write(argv[1])
    if command == "file-read" and len(argv) == 2:
        return file_read(argv[1])
    if command == "stop" and len(argv) == 2:
        with request("POST", f"/v1/runs/{argv[1]}/stop", {}) as response:
            emit("stopping", status=json.load(response).get("status"))
        return 0
    print("usage: hermes-relay health|run FILE|events RUN|stop RUN|clarify RUN FILE|memory-write FILE|memory-read|skills-read|skills-hydrate DIR|inputs-list|inputs-write DIR|file-read PATH|active", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except urllib.error.HTTPError as exc:
        emit("error", status=exc.code)
        sys.exit(1)
    except (OSError, ValueError, KeyError) as exc:
        emit("error", reason=type(exc).__name__)
        sys.exit(1)
