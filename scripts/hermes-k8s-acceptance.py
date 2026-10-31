"""Acceptance of KubernetesHermesSandboxes against a live cluster.

Driven by ../smoke-test-hermes-k8s.sh from the skill-executor directory, against
the chart's Hermes namespace and the real hermes-sandbox image: it needs no
database or object store (snapshots go to memory) and prints one PASS or FAIL
line per check; the exit code is the number of failures, capped at 1.
"""
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from src.config import Settings
from src.hermes_kubernetes import KubernetesHermesSandboxes
from src.hermes_runtime import HermesSandboxError
from src.hermes_snapshots import Snapshot, SnapshotError

SESSION = "b" * 32
ORG, CONV = "org-1", "conv-1"
TOKEN = "hermes-session-token-0123456789"
NAMESPACE = os.environ.get("SKILL_EXECUTOR_HERMES_ACCEPTANCE_NAMESPACE", "hermes-agents")
RELEASE = os.environ.get("SKILL_EXECUTOR_HERMES_ACCEPTANCE_RELEASE", "rel")
# The cluster API's Service address (k3s's default), which a sandbox must not reach.
API_ADDRESS = os.environ.get("SKILL_EXECUTOR_HERMES_ACCEPTANCE_API", "10.43.0.1")
FAILED = 0


class MemoryStore:
    """SnapshotStore's interface over a dict."""

    enabled = True

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, bytes]] = {}
        self.latest_sha: dict[tuple[str, str], str] = {}

    async def put(self, org: str, conversation: str, path: Path, snapshot: Snapshot, session_id: str) -> None:
        self.objects.setdefault((org, conversation), {})[snapshot.sha256] = path.read_bytes()
        self.latest_sha[(org, conversation)] = snapshot.sha256

    async def latest(self, org: str, conversation: str) -> str | None:
        return self.latest_sha.get((org, conversation))

    async def fetch(self, org: str, conversation: str, sha256: str, path: Path, limit: int) -> None:
        data = self.objects.get((org, conversation), {}).get(sha256)
        if data is None:
            raise SnapshotError("hermes_snapshot_not_found")
        if hashlib.sha256(data).hexdigest() != sha256:
            raise SnapshotError("hermes_snapshot_invalid")
        path.write_bytes(data)

    async def sweep(self, cutoff: float) -> int:
        return 0

    async def delete_conversation(self, org: str, conversation: str) -> int:
        return len(self.objects.pop((org, conversation), {}))


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    print(("PASS  " if ok else "FAIL  ") + label + (f"  [{detail}]" if detail and not ok else ""), flush=True)
    if not ok:
        FAILED += 1


async def main() -> None:
    settings = Settings(
        hermes_enabled=True,
        runtime="kubernetes",
        hermes_namespace=NAMESPACE,
        kubernetes_release_name=RELEASE,
        hermes_sandbox_image="aurum-ai-hermes-sandbox:latest",
        hermes_pod_start_timeout_seconds=240,
        hermes_start_timeout_seconds=120,
        hermes_exec_timeout_seconds=60,
        hermes_snapshot_max_bytes=64 * 1024 * 1024,
    )
    store = MemoryStore()
    sandboxes = KubernetesHermesSandboxes(settings, store=store)  # type: ignore[arg-type]
    await sandboxes.ready()
    check("ready: the guard Service is found", True)

    t0 = time.monotonic()
    sandbox, outcome = await sandboxes.create(
        session_id=SESSION, org_id=ORG, conversation_id=CONV, token=TOKEN, model="m"
    )
    check(f"create: a Pod runs and the relay is healthy ({time.monotonic() - t0:.0f}s)", outcome == "created")

    # Files in through tar over exec, then read back through the relay.
    await sandboxes.write_input(SESSION, "notes.txt", b"hello from the platform")
    inputs = await sandboxes.list_inputs(SESSION)
    check("an upload reaches /workspace/inputs", "notes.txt" in inputs, str(inputs))
    data = await sandboxes.read_file(SESSION, "/workspace/inputs/notes.txt")
    check("read_file streams the file back, hash-checked", data == b"hello from the platform")

    # The platform's files are in the workspace, owned by the sandbox user.
    code, out = await sandboxes._exec(sandbox, ["sh", "-c", "ls -ln /workspace/.aurum/proxy | awk '{print $3}'; test -w /workspace && echo writable"])
    check("the workspace is writable and files belong to uid 10001", code == 0 and b"10001" in out and b"writable" in out, out.decode())
    code, out = await sandboxes._exec(sandbox, ["sh", "-c", "grep -c hermes-session-token /proc/1/environ /proc/self/environ 2>/dev/null; id -u"])
    check("the token is not in the environment", b":0" in out or out.strip().endswith(b"10001"), out.decode())

    # A preview: a web app on loopback, reached through the exec pipe.
    await sandboxes._exec(
        sandbox,
        ["sh", "-c", "cd /tmp && nohup python3 -m http.server 8899 --bind 127.0.0.1 >/dev/null 2>&1 &"],
    )
    await asyncio.sleep(2)
    stream = await sandboxes.connect(SESSION, 8899)
    await stream.write_in(b"GET / HTTP/1.0\r\n\r\n")
    got = b""
    async with asyncio.timeout(20):
        while (message := await stream.read_out()) is not None:
            got += message.data
            if len(got) > 100:
                break
    await stream.close()
    check("connect: bytes go both ways through the pipe", got.startswith(b"HTTP/1."), got[:60].decode(errors="replace"))

    # The network, from inside the sandbox.
    probe = (
        "import socket,sys\n"
        "def can(host, port):\n"
        "    try:\n"
        "        socket.create_connection((host, port), 4).close(); return True\n"
        "    except OSError: return False\n"
        "def resolves(name):\n"
        "    try: socket.getaddrinfo(name, 80); return True\n"
        "    except OSError: return False\n"
        "print(json.dumps({'guard': can('hermes-egress', 8080), 'proxy': can('hermes-egress', 3128),"
        " 'internet': can('1.1.1.1', 443), 'dns_cluster': resolves('kubernetes.default.svc.cluster.local'),"
        " 'dns_public': resolves('pypi.org'), 'api': can(" + repr(API_ADDRESS) + ", 443)}))\n"
    )
    code, out = await sandboxes._exec(sandbox, ["python3", "-c", "import json\n" + probe])
    seen = json.loads(out.decode().strip().splitlines()[-1])
    check("network: the guard is reachable by name through hostAliases", seen["guard"] and seen["proxy"], str(seen))
    check("network: no internet, no DNS, no cluster API", not (seen["internet"] or seen["dns_cluster"] or seen["dns_public"] or seen["api"]), str(seen))

    # Snapshot from the running Pod, then retire and restore into a new Pod.
    snapshot = await sandboxes.snapshot(SESSION)
    check("snapshot: tar of the live workspace is stored", store.latest_sha.get((ORG, CONV)) == snapshot.sha256)
    await sandboxes.close(SESSION, snapshot=True)
    try:
        sandboxes.get(SESSION)
        check("close(snapshot): the sandbox is gone", False)
    except HermesSandboxError:
        check("close(snapshot): the sandbox is gone", True)
    sandbox, outcome = await sandboxes.create(
        session_id=SESSION, org_id=ORG, conversation_id=CONV, token=TOKEN, model="m"
    )
    check("create restores the snapshot into a new Pod", outcome == "restored")
    inputs = await sandboxes.list_inputs(SESSION)
    check("the restored workspace has the upload", "notes.txt" in inputs, str(inputs))

    # A restarted executor re-adopts the live Pod.
    fresh = KubernetesHermesSandboxes(settings, store=store)  # type: ignore[arg-type]
    adopted = await fresh.adopt()
    check("adopt: a new executor finds the live sandbox", adopted == 1 and fresh.get(SESSION).org_id == ORG, str(adopted))
    await fresh.shutdown()

    # Volume usage is measured in the Pod.
    usage = await sandboxes._volume_usage()
    check("workspace usage is measured with du", usage.get(sandbox.volume, -1) > 0, str(usage))

    await sandboxes.close(SESSION)
    check("close removes the Pod", True)
    await sandboxes.shutdown()


try:
    asyncio.run(main())
except Exception as exc:  # noqa: BLE001
    import traceback

    traceback.print_exc()
    print(f"FAIL  driver crashed: {type(exc).__name__}: {exc}")
    FAILED += 1
print("DRIVER-DONE failures=%d" % FAILED)
sys.exit(1 if FAILED else 0)
