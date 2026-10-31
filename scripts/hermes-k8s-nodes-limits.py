"""Several nodes, eviction and a PID limit for Hermes sandboxes, on a throwaway k3d cluster.

Driven by ../smoke-test-hermes-k8s-nodes.sh from the skill-executor directory, with
KUBECONFIG already exported (the Kubernetes client reads it when it is imported).
The cluster has three nodes, a local registry and a node with a PID limit; the chart's
own Hermes objects run on it and skill-executor's KubernetesHermesSandboxes drives the
sandboxes. One PASS or FAIL line per check (INFO lines give the measurements); the exit
code is 1 if any failed. About fifteen minutes, most of them the lost node.
"""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from src.config import Settings
from src.hermes_kubernetes import KubernetesHermesSandboxes
from src.hermes_runtime import HermesSandboxError
from src.hermes_snapshots import Snapshot, SnapshotError

ROOT = Path(__file__).resolve().parents[1]
SP = Path(os.environ["KUBECONFIG"]).parent
CL = os.environ.get("HERMES_K8S_NODES_CLUSTER", "aurum-ai-hermes-nodes-smoke")
REG = "aurum-ai-hermes-registry.localhost"
PORT = int(os.environ.get("HERMES_K8S_NODES_REGISTRY_PORT", "5111"))
REGH = f"k3d-{REG}:{PORT}"
HN, PN = "hermes-agents", "platform"
ORG = "org-1"
TOKEN = "hermes-session-token-0123456789"
ENV = dict(os.environ)
KUBECONFIG = Path(os.environ["KUBECONFIG"])
FAILED = 0
NODES = [f"k3d-{CL}-server-0", f"k3d-{CL}-agent-0", f"k3d-{CL}-agent-1"]


def sh(cmd, check=True, timeout=600, inp=None):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), env=ENV, text=True, capture_output=True, timeout=timeout, input=inp)
    if check and r.returncode:
        raise RuntimeError(f"{cmd}\n{r.stdout}\n{r.stderr}")
    return r


def kc(*a, **kw):
    return sh(["kubectl", *a], **kw)


def check(label, ok, detail=""):
    global FAILED
    print(("PASS  " if ok else "FAIL  ") + label + (f"   [{detail}]" if not ok and detail else ""), flush=True)
    FAILED += 0 if ok else 1


def info(label, detail):
    print(f"INFO  {label}: {detail}", flush=True)


class MemoryStore:
    enabled = True

    def __init__(self):
        self.objects, self.latest_sha = {}, {}

    async def put(self, org, conversation, path, snapshot, session_id):
        self.objects.setdefault((org, conversation), {})[snapshot.sha256] = path.read_bytes()
        self.latest_sha[(org, conversation)] = snapshot.sha256

    async def latest(self, org, conversation):
        return self.latest_sha.get((org, conversation))

    async def fetch(self, org, conversation, sha256, path, limit):
        data = self.objects.get((org, conversation), {}).get(sha256)
        if data is None:
            raise SnapshotError("hermes_snapshot_not_found")
        path.write_bytes(data)

    async def sweep(self, cutoff):
        return 0

    async def delete_conversation(self, org, conversation):
        return 0


STORE = MemoryStore()


def sid(n):
    return f"{n:032x}"


def make(**over):
    base = dict(
        hermes_enabled=True, runtime="kubernetes", hermes_namespace=HN, kubernetes_release_name="rel",
        hermes_sandbox_image=f"{REGH}/aurum-ai-hermes-sandbox:latest",
        hermes_pod_start_timeout_seconds=300, hermes_start_timeout_seconds=120,
        hermes_exec_timeout_seconds=120, hermes_snapshot_max_bytes=256 * 1024 * 1024,
    )
    base.update(over)
    return KubernetesHermesSandboxes(Settings(**base), store=STORE)  # type: ignore[arg-type]


async def create(ex, n, org=ORG, **kw):
    return await ex.create(session_id=sid(n), org_id=org, conversation_id=f"conv-{n}", token=TOKEN, model="m", **kw)


def pod_node(n):
    return kc("-n", HN, "get", "pod", f"aurum-ai-hermes-{sid(n)}", "-o", "jsonpath={.spec.nodeName}", check=False).stdout.strip()


def pod_names():
    return kc("-n", HN, "get", "pod", "-l", "aurum-ai.component=hermes-sandbox", "-o", "jsonpath={.items[*].metadata.name}").stdout.split()


def cordon(*nodes):
    for n in NODES:
        kc(("cordon" if n in nodes else "uncordon"), n)


def wipe_pods():
    kc("-n", HN, "delete", "pod", "-l", "aurum-ai.component=hermes-sandbox", "--wait=true", "--grace-period=0", "--force", check=False)


# ---- setup (part 1) -----------------------------------------------------------------------
def setup():
    sh(["k3d", "registry", "create", REG, "--port", str(PORT)])
    sh(["k3d", "cluster", "create", CL, "--servers", "1", "--agents", "2", "--no-lb", "--wait",
        "--registry-use", f"{REGH}",
        "--k3s-arg", "--disable=traefik@server:0",
        "--k3s-arg", "--kubelet-arg=pod-max-pids=64@agent:0",
        "--k3s-node-label", "aurum.test/pids=limited@agent:0",
        "--kubeconfig-update-default=false"], timeout=900)
    KUBECONFIG.write_text(sh(["k3d", "kubeconfig", "get", CL]).stdout)
    kc("wait", "--for=condition=Ready", "node", "--all", "--timeout=180s")
    for image in ("aurum-ai-hermes-sandbox", "aurum-ai-web-egress"):
        sh(["docker", "tag", f"{image}:latest", f"localhost:{PORT}/{image}:latest"])
        sh(["docker", "push", "-q", f"localhost:{PORT}/{image}:latest"], timeout=900)
    kc("create", "ns", PN)
    args = ["helm", "template", "rel", str(ROOT / "charts/aurum-ai"), "-n", PN,
            "--set", "hermes.enabled=true", "--set", f"hermes.namespace={HN}",
            "--set", f"image.registry={REGH}", "--set", "image.tag=latest"]
    for t in ["hermes-namespace", "hermes-secret", "hermes-egress-deployment", "hermes-egress-service",
              "hermes-network-policies", "hermes-rbac"]:
        args += ["-s", f"templates/{t}.yaml"]
    (SP / "hermes.yaml").write_text(sh(args).stdout)
    kc("apply", "-f", str(SP / "hermes.yaml"))
    kc("-n", HN, "rollout", "status", "deploy/rel-hermes-egress", "--timeout=300s")
    kc("apply", "-f", "-", inp="""
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata: {name: limited}
handler: runc
scheduling:
  nodeSelector: {aurum.test/pids: limited}
""")
    time.sleep(10)


# ---- part 1 sections -------------------------------------------------------------------------
async def cold_pull():
    print("--- A: a cold image pull on a node that has never run the image", flush=True)
    cordon(NODES[0], NODES[1])  # only agent-1 is schedulable (the server is tainted anyway)
    node = NODES[2]
    ex = make()
    t0 = time.monotonic()
    sandbox, outcome = await create(ex, 1)
    cold = time.monotonic() - t0
    check(f"a sandbox starts on a node that must pull the 2.4 GB image first ({cold:.0f}s)", pod_node(1) == node and outcome == "created", pod_node(1))
    info("cold start", f"{cold:.0f}s against hermes.sandbox.podStartTimeoutSeconds=300 (default)")
    check("the default pod start timeout leaves room for the cold pull", cold < 300 * 0.5, f"{cold:.0f}s")
    await ex.close(sid(1))
    t0 = time.monotonic()
    await create(ex, 1)
    warm = time.monotonic() - t0
    info("warm start", f"{warm:.0f}s")
    await ex.close(sid(1))
    # drop the image from the node, then give the Pod too little time to pull it
    sh(["docker", "exec", node, "crictl", "rmi", f"{REGH}/aurum-ai-hermes-sandbox:latest"], check=False)
    short = make(hermes_pod_start_timeout_seconds=3)
    try:
        await create(short, 2)
        check("a pull that outlasts the pod start timeout is reported", False, "no error")
    except HermesSandboxError as exc:
        check("a pull that outlasts the pod start timeout is reported (hermes_start_timeout) and the Pod is removed",
              exc.code == "hermes_start_timeout" and not pod_names(), f"{exc.code} {pod_names()}")
    wipe_pods()
    await ex.shutdown(); await short.shutdown()
    cordon()


async def capacity():
    print("--- B: capacity eviction under load", flush=True)
    wipe_pods()
    ex = make(hermes_max_running=2, hermes_max_running_per_org=4)
    for n in (11, 12, 13):
        t0 = time.monotonic()
        await create(ex, n)
        info(f"create {n}", f"{time.monotonic() - t0:.0f}s, {len(pod_names())} Pods")
        await asyncio.sleep(1)
    names = set(pod_names())
    check("at the operator cap the least recently used idle sandbox is retired", f"aurum-ai-hermes-{sid(11)}" not in names and len(names) == 2, str(names))
    check("the retired sandbox left a snapshot", (ORG, "conv-11") in STORE.latest_sha)
    _, outcome = await create(ex, 11)
    check("it comes back restored from that snapshot (retiring the next idle one)", outcome == "restored" and len(pod_names()) == 2, outcome)
    wipe_pods()
    per_org = make(hermes_max_running=10, hermes_max_running_per_org=1)
    await create(per_org, 21, org="org-a")
    await create(per_org, 22, org="org-b")
    await create(per_org, 23, org="org-a")
    names = set(pod_names())
    check("the per-organization cap retires only that organization's sandbox",
          names == {f"aurum-ai-hermes-{sid(22)}", f"aurum-ai-hermes-{sid(23)}"}, str(names))
    wipe_pods()
    await ex.shutdown(); await per_org.shutdown()


async def workspace_limits():
    print("--- C: the workspace limit, soft and the kubelet's backstop", flush=True)
    wipe_pods()
    ex = make(hermes_workspace_max_bytes=64 * 1024 * 1024, hermes_orphan_grace_seconds=5,
              hermes_disk_check_interval_seconds=0, hermes_snapshot_retry_seconds=1)
    sandbox, _ = await create(ex, 31)
    await ex.snapshot(sid(31))
    await ex._exec(sandbox, ["sh", "-c", "dd if=/dev/zero of=/workspace/big bs=1M count=80 2>/dev/null"])
    ex._disk_checked_at = 0
    await ex._check_disk()
    check("over the soft limit the sandbox is flagged and refuses new runs", ex.get(sid(31)).workspace_full)
    try:
        async for _ in ex.run(sid(31), {"key": "k"}):
            pass
        check("a run is refused while the workspace is full", False, "no error")
    except HermesSandboxError as exc:
        check("a run is refused while the workspace is full", exc.code == "hermes_workspace_full", exc.code)
    await ex._exec(sandbox, ["rm", "/workspace/big"])
    ex._disk_checked_at = 0
    await ex._check_disk()
    check("back under the limit it takes runs again", not ex.get(sid(31)).workspace_full)

    # past the backstop (1.5 x the soft limit = 96 MiB) the kubelet evicts the Pod
    name = f"aurum-ai-hermes-{sid(31)}"
    await ex._exec(sandbox, ["sh", "-c", "dd if=/dev/zero of=/workspace/huge bs=1M count=200 2>/dev/null; sync"])
    reason, t0 = "", time.monotonic()
    while time.monotonic() - t0 < 150:
        r = kc("-n", HN, "get", "pod", name, "-o", "jsonpath={.status.phase}/{.status.reason}", check=False)
        if r.returncode != 0 or r.stdout.startswith("Failed"):
            reason = r.stdout or "gone"
            break
        await asyncio.sleep(5)
    info("kubelet", f"{reason} after {time.monotonic() - t0:.0f}s")
    check("past the backstop the kubelet evicts the Pod", reason.startswith("Failed/Evicted") or reason == "gone", reason)
    await asyncio.sleep(6)
    await ex.reap()
    try:
        ex.get(sid(31))
        check("the executor forgets the evicted sandbox and removes its Pod", False, "still known")
    except HermesSandboxError:
        check("the executor forgets the evicted sandbox and removes its Pod", name not in pod_names(), str(pod_names()))
    sandbox, outcome = await create(ex, 31)
    check("the session starts again from its last snapshot", outcome == "restored", outcome)
    wipe_pods()
    await ex.shutdown()


FORK = ("import os,signal,time\n"
        "pids=[]\n"
        "try:\n"
        "    for _ in range(400):\n"
        "        p=os.fork()\n"
        "        if p==0:\n"
        "            time.sleep(60); os._exit(0)\n"
        "        pids.append(p)\n"
        "except OSError as e:\n"
        "    print('LIMIT', len(pids), e.errno)\n"
        "else:\n"
        "    print('FREE', len(pids))\n"
        "for p in pids:\n"
        "    os.kill(p, signal.SIGKILL)\n")


async def pid_limit():
    print("--- D: a PID limit through a RuntimeClass", flush=True)
    wipe_pods()
    cordon()
    limited = make(hermes_runtime_class="limited")
    sandbox, _ = await create(limited, 41)
    check("a RuntimeClass with a nodeSelector puts the sandbox on the limited node", pod_node(41) == NODES[1], pod_node(41))
    _, out = await limited._exec(sandbox, ["python3", "-c", FORK])
    text = out.decode().strip().splitlines()[-1] if out.strip() else ""
    info("limited node", text)
    check("the limited node stops a fork loop well below the node's own", text.startswith("LIMIT") and int(text.split()[1]) < 100, text)
    await limited.close(sid(41))
    cordon(NODES[0], NODES[1])
    plain = make()
    sandbox, _ = await create(plain, 42)
    check("without the RuntimeClass the sandbox lands on the other node", pod_node(42) == NODES[2], pod_node(42))
    _, out = await plain._exec(sandbox, ["python3", "-c", FORK])
    text = out.decode().strip().splitlines()[-1] if out.strip() else ""
    info("unlimited node", text)
    check("the other node has no such limit for the same loop", text.startswith("FREE") or int(text.split()[1]) > 250, text)
    wipe_pods()
    cordon()
    await limited.shutdown(); await plain.shutdown()


# ---- part 2 -----------------------------------------------------------------------------------
async def node_loss():
    print("--- E: a node is lost under a running sandbox", flush=True)
    wipe_pods()
    cordon(NODES[0], NODES[1])  # agent-1 hosts it
    ex = make(hermes_orphan_grace_seconds=5, hermes_exec_timeout_seconds=15)
    sandbox, _ = await create(ex, 51)
    await ex._exec(sandbox, ["sh", "-c", "echo precious > /workspace/work.txt"])
    await ex.snapshot(sid(51))
    check("the sandbox is on the node about to be lost", pod_node(51) == NODES[2], pod_node(51))
    cordon()
    sh(["docker", "stop", NODES[2]])
    t0 = time.monotonic()
    name = f"aurum-ai-hermes-{sid(51)}"
    detected = None
    for _ in range(40):
        await asyncio.sleep(10)
        t = time.monotonic() - t0
        node = kc("get", "node", NODES[2], "-o", "jsonpath={.status.conditions[?(@.type=='Ready')].status}", check=False).stdout
        pod = kc("-n", HN, "get", "pod", name, "-o",
                 "jsonpath={.status.phase}/{.status.conditions[?(@.type=='Ready')].status}/{.metadata.deletionTimestamp}", check=False)
        info(f"t={t:.0f}s", f"node Ready={node or 'gone'}, pod phase/Ready/deleted = {pod.stdout or 'absent'}")
        parts = (pod.stdout or "").split("/")
        if pod.returncode != 0 or (len(parts) > 1 and parts[1] not in ("True", "")):
            detected = t
            break
    check("the cluster reports the Pod of the lost node as not Ready", detected is not None, "never")
    info("detection", f"{detected:.0f}s after the node stopped" if detected else "none")
    t1 = time.monotonic()
    sandbox, outcome = await create(ex, 51)
    took = time.monotonic() - t1
    code, out = await ex._exec(sandbox, ["cat", "/workspace/work.txt"])
    check(f"create() starts the session anew on a live node from its snapshot ({took:.0f}s)",
          outcome == "restored" and b"precious" in out and pod_node(51) != NODES[2], f"{outcome} {out!r} node={pod_node(51)}")
    stuck = kc("-n", HN, "get", "pod", "-o", "jsonpath={.items[*].metadata.name}").stdout.split()
    check("the dead node's Pod object does not linger under the same name", stuck.count(name) <= 1, str(stuck))
    sh(["docker", "start", NODES[2]])
    wipe_pods()
    await ex.shutdown()


async def main():
    existing = sh(["k3d", "cluster", "list", "-o", "json"]).stdout.replace(" ", "")
    if f'"name":"{CL}"' in existing:
        raise SystemExit(f"a cluster named {CL} already exists; delete it or set HERMES_K8S_NODES_CLUSTER")
    if f"k3d-{REG}" in sh(["k3d", "registry", "list", "-o", "json"]).stdout:
        raise SystemExit(f"the registry k3d-{REG} already exists; delete it first")
    setup()
    for section in (cold_pull, capacity, workspace_limits, pid_limit, node_loss):
        try:
            await section()
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            check(f"{section.__name__} ran to the end", False, f"{type(exc).__name__}: {exc}")


try:
    asyncio.run(main())
except SystemExit:
    raise
except Exception as exc:  # noqa: BLE001
    import traceback

    traceback.print_exc()
    check("the run did not crash", False, f"{type(exc).__name__}: {exc}")
finally:
    if os.environ.get("KEEP_HERMES_K8S_CLUSTER") != "1":
        sh(["k3d", "cluster", "delete", CL], check=False)
        sh(["k3d", "registry", "delete", f"k3d-{REG}"], check=False)
print(f"{FAILED} check(s) failed" if FAILED else "all checks passed", flush=True)
sys.exit(1 if FAILED else 0)
