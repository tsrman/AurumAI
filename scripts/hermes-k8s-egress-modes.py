"""Egress modes of the Hermes guard with real internet traffic, on a throwaway k3d cluster.

Driven by ../smoke-test-hermes-k8s-egress.sh. The objects are the chart's own
(helm template of the hermes-* templates); the backend is a stand-in that decides a
session's mode from its token and can change it while a tunnel is open, and the
internal package mirror is a Pod in another namespace. It needs the internet: the
package registries and a few public hosts are reached for real. One PASS or FAIL
line per check; the exit code is 1 if any failed.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLUSTER = os.environ.get("HERMES_K8S_EGRESS_CLUSTER", "aurum-ai-hermes-egress-smoke")
WORK = Path(tempfile.mkdtemp(prefix="egress-modes-"))
ENV = {**os.environ, "KUBECONFIG": str(WORK / "kubeconfig")}
HN, PN, MN = "hermes-agents", "platform", "mirrors"
SECRET = "change-me-hermes-egress-secret"
FAILED = 0
MIRROR_HOST = "mirror.mirrors.svc.cluster.local"


def sh(cmd, check=True, inp=None, timeout=300):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), env=ENV, text=True, input=inp,
                       capture_output=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"{cmd}\n{r.stdout}\n{r.stderr}")
    return r


def kc(*args, **kw):
    return sh(["kubectl", *args], **kw)


def check(label, ok, detail=""):
    global FAILED
    print(("PASS  " if ok else "FAIL  ") + label + (f"   [{detail}]" if not ok and detail else ""), flush=True)
    FAILED += 0 if ok else 1


def info(label, detail):
    print(f"INFO  {label}: {detail}", flush=True)


# ---- cluster ------------------------------------------------------------------------
def setup():
    existing = sh(["k3d", "cluster", "list", "-o", "json"]).stdout
    if f'"name":"{CLUSTER}"' in existing.replace(" ", ""):
        raise SystemExit(f"a cluster named {CLUSTER} already exists; delete it or set HERMES_K8S_EGRESS_CLUSTER")
    sh(["k3d", "cluster", "create", CLUSTER, "--servers", "1", "--agents", "0", "--no-lb", "--wait",
        "--k3s-arg", "--disable=traefik@server:0", "--kubeconfig-update-default=false"])
    (WORK / "kubeconfig").write_text(sh(["k3d", "kubeconfig", "get", CLUSTER]).stdout)
    kc("wait", "--for=condition=Ready", "node", "--all", "--timeout=120s")
    sh(["k3d", "image", "import", "aurum-ai-web-egress:latest", "aurum-ai-hermes-sandbox:latest", "-c", CLUSTER])
    kc("create", "ns", PN)
    kc("create", "ns", MN)
    backend = r'''
import http.server, json, threading
MODES = {}
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0); body = self.rfile.read(n)
        if self.path.startswith("/_set"):
            q = dict(p.split("=") for p in self.path.split("?")[1].split("&"))
            MODES[q["token"]] = q["mode"]; return self._send(200, {"ok": True})
        if self.path != "/v1/internal/hermes/egress/authorize":
            return self._send(200, {"backend": "ok"})
        if self.headers.get("authorization") != "Bearer change-me-hermes-egress-secret":
            return self._send(401, {})
        token = json.loads(body)["token"]
        mode = MODES.get(token) or token.split("-")[1]
        if mode == "revoked":
            return self._send(200, {"allow": False, "reason": "hermes_session_revoked", "maxAgeSeconds": 2})
        self._send(200, {"allow": True, "mode": mode, "sessionId": "s-" + token, "turnId": "t1", "maxAgeSeconds": 2})
    do_GET = do_POST
http.server.ThreadingHTTPServer(("0.0.0.0", 8000), H).serve_forever()
'''
    mirror = r'''
import http.server, time
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == "/hold":
            self.send_response(200); self.send_header("content-type", "text/plain"); self.end_headers()
            for _ in range(600):
                self.wfile.write(b"x\n"); self.wfile.flush(); time.sleep(1)
            return
        b = b"mirror-ok"
        self.send_response(200); self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)
http.server.ThreadingHTTPServer(("0.0.0.0", 8443), H).serve_forever()
'''
    (WORK / "backend.py").write_text(backend)
    (WORK / "mirror.py").write_text(mirror)
    kc("-n", PN, "create", "configmap", "standin", f"--from-file=backend.py={WORK/'backend.py'}")
    kc("-n", MN, "create", "configmap", "standin", f"--from-file=mirror.py={WORK/'mirror.py'}")

    def pod(ns, name, labels, script, port, service=None):
        return f"""
apiVersion: v1
kind: Pod
metadata: {{name: {name}, labels: {labels}}}
spec:
  containers:
  - name: c
    image: aurum-ai-web-egress:latest
    imagePullPolicy: IfNotPresent
    command: [python3, /s/{script}]
    volumeMounts: [{{name: s, mountPath: /s}}]
  volumes: [{{name: s, configMap: {{name: standin}}}}]
---
apiVersion: v1
kind: Service
metadata: {{name: {service or ('rel-backend' if name == 'backend' else 'mirror')}}}
spec: {{selector: {labels}, ports: [{{port: {port}, targetPort: {port}}}]}}
"""
    kc("-n", PN, "apply", "-f", "-", inp=pod(PN, "backend", "{app.kubernetes.io/name: backend, app.kubernetes.io/instance: rel}", "backend.py", 8000))
    kc("-n", MN, "apply", "-f", "-", inp=pod(MN, "mirror", "{app: mirror}", "mirror.py", 8443))
    # A second server in the same namespace on the same port, with other labels:
    # a selector for the mirror must not open it.
    kc("-n", MN, "apply", "-f", "-", inp=pod(MN, "decoy", "{app: decoy}", "mirror.py", 8443, service="decoy"))
    kc("-n", PN, "apply", "-f", "-", inp="""
apiVersion: v1
kind: Pod
metadata: {name: outsider}
spec:
  containers:
  - name: c
    image: aurum-ai-web-egress:latest
    imagePullPolicy: IfNotPresent
    command: [python3, -m, http.server, "8080"]
""")
    kc("-n", PN, "wait", "--for=condition=Ready", "pod/backend", "pod/outsider", "--timeout=120s")
    kc("-n", MN, "wait", "--for=condition=Ready", "pod/mirror", "pod/decoy", "--timeout=120s")


def cidr_destination(cidr):
    return {"cidr": cidr, "ports": [{"protocol": "TCP", "port": 8443}]}


def apply_round(max_mode, destination):
    values = WORK / "values.yaml"
    values.write_text(json.dumps({"hermes": {"egress": {"internalDestinations": [destination]}}}))
    args = ["helm", "template", "rel", str(ROOT / "charts/aurum-ai"), "-n", PN, "-f", str(values),
            "--set", "hermes.enabled=true", "--set", f"hermes.namespace={HN}",
            "--set", f"hermes.egress.maxMode={max_mode}", "--set", "hermes.egress.recheckSeconds=4",
            "--set", f"hermes.egress.internalPackageHosts={{{MIRROR_HOST}:8443}}"]
    for t in ["hermes-namespace", "hermes-secret", "hermes-egress-deployment", "hermes-egress-service",
              "hermes-network-policies", "hermes-rbac"]:
        args += ["-s", f"templates/{t}.yaml"]
    (WORK / "r.yaml").write_text(sh(args).stdout)
    kc("apply", "-f", str(WORK / "r.yaml"))
    kc("-n", HN, "rollout", "status", "deploy/rel-hermes-egress", "--timeout=180s")
    # An old guard may still be terminating: wait for exactly one Pod.
    for _ in range(60):
        pods = kc("-n", HN, "get", "pod", "-l", "app.kubernetes.io/name=hermes-egress", "-o", "json").stdout
        items = json.loads(pods)["items"]
        if len(items) == 1 and not items[0]["metadata"].get("deletionTimestamp"):
            break
        time.sleep(1)
    time.sleep(8)  # the policy controller programs the rules
    return items[0]["metadata"]["name"]


PROBE = r'''
import base64, json, socket, ssl
def tunnel(host, port, token, tls=False, sni=None):
    try:
        s = socket.create_connection(("hermes-egress", 3128), 5)
    except OSError as e:
        return {"error": "guard_unreachable"}
    s.settimeout(20)
    auth = base64.b64encode(f"hermes:{token}".encode()).decode()
    s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\nProxy-Authorization: Basic {auth}\r\n\r\n".encode())
    buf = b""
    try:
        while b"\r\n\r\n" not in buf:
            d = s.recv(4096)
            if not d: break
            buf += d
    except OSError as e:
        return {"error": type(e).__name__}
    head = buf.split(b"\r\n", 1)[0].decode(errors="replace")
    status = int(head.split()[1]) if head.startswith("HTTP/") else 0
    res = {"status": status}
    if status != 200:
        res["reason"] = buf.partition(b"\r\n\r\n")[2].decode(errors="replace").strip()[:80]
        return res
    try:
        if tls:
            s = ssl.create_default_context().wrap_socket(s, server_hostname=sni or host)
            s.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\nUser-Agent: probe\r\n\r\n".encode())
        else:
            s.sendall(f"GET / HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
        data = b""
        while len(data) < 2048:
            d = s.recv(2048)
            if not d: break
            data += d
        res["http"] = data.split(b"\r\n", 1)[0].decode(errors="replace")
        res["body"] = data.decode(errors="replace")[-40:]
    except (OSError, ssl.SSLError) as e:
        res["closed"] = type(e).__name__
    return res
CASES = json.loads(%s)
print(json.dumps([tunnel(**c) for c in CASES]))
'''


def probe_pod(name, image, command):
    guard_ip = kc("-n", HN, "get", "svc", "hermes-egress", "-o", "jsonpath={.spec.clusterIP}").stdout
    kc("-n", HN, "apply", "-f", "-", inp=f"""
apiVersion: v1
kind: Pod
metadata:
  name: {name}
  labels: {{app.kubernetes.io/instance: rel, aurum-ai.component: hermes-sandbox}}
spec:
  automountServiceAccountToken: false
  dnsPolicy: None
  dnsConfig: {{nameservers: ["192.0.2.1"], options: [{{name: timeout, value: "1"}}, {{name: attempts, value: "1"}}]}}
  hostAliases: [{{ip: "{guard_ip}", hostnames: [hermes-egress]}}]
  securityContext: {{runAsNonRoot: true, runAsUser: 10001, seccompProfile: {{type: RuntimeDefault}}}}
  containers:
  - name: c
    image: {image}
    imagePullPolicy: IfNotPresent
    command: {command}
    securityContext: {{allowPrivilegeEscalation: false, capabilities: {{drop: [ALL]}}}}
""")


def run_cases(cases):
    script = PROBE % json.dumps(json.dumps(cases))
    r = kc("-n", HN, "exec", "-i", "sb", "--", "python3", "-", inp=script, timeout=400)
    return json.loads(r.stdout.strip().splitlines()[-1])


def expect(label, case, want):
    got = run_cases([case])[0]
    ok = all((k in got) if want_v == "" else str(want_v) in str(got.get(k, "")) for k, want_v in want.items())
    check(label, ok, json.dumps(got))
    return got


def direct(label, pod, host, port, want_ok):
    code = f"import socket; socket.create_connection(('{host}',{port}),5)"
    r = kc("-n", HN, "exec", pod, "--", "python3", "-c", code, check=False, timeout=60)
    check(label, (r.returncode == 0) == want_ok, (r.stderr or "")[-100:])


def set_mode(token, mode):
    kc("-n", PN, "exec", "backend", "--", "python3", "-c",
       f"import urllib.request; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/_set?token={token}&mode={mode}', data=b'x'))")


def main():
    setup()
    mirror_cluster_ip = kc("-n", MN, "get", "svc", "mirror", "-o", "jsonpath={.spec.clusterIP}").stdout
    mirror_pod_ip = kc("-n", MN, "get", "pod", "mirror", "-o", "jsonpath={.status.podIP}").stdout
    neighbour = kc("-n", PN, "get", "pod", "outsider", "-o", "jsonpath={.status.podIP}").stdout
    info("mirror", f"service {mirror_cluster_ip}, pod {mirror_pod_ip}")

    # ---- round 1: the in-cluster mirror allowed by its Service address -------------
    print("--- round 1: packages, mirror allowed by ClusterIP/32", flush=True)
    guard = apply_round("packages", cidr_destination(f"{mirror_cluster_ip}/32"))
    probe_pod("sb", "aurum-ai-web-egress:latest", "[sleep, '7200']")
    kc("-n", HN, "wait", "--for=condition=Ready", "pod/sb", "--timeout=120s")
    got = run_cases([{"host": MIRROR_HOST, "port": 8443, "token": "m-packages-a"}])[0]
    info("internal mirror, ipBlock = Service address", json.dumps(got))
    CLUSTER_IP_WORKS = "mirror-ok" in str(got.get("body", ""))

    # ---- round 2: packages -----------------------------------------------------------
    print("--- round 2: max mode packages, mirror allowed by Pod IP/32", flush=True)
    guard = apply_round("packages", cidr_destination(f"{mirror_pod_ip}/32"))
    kc("-n", HN, "wait", "--for=condition=Ready", "pod/sb", "--timeout=120s")
    expect("packages: pypi.org:443 through the tunnel (real TLS)", {"host": "pypi.org", "port": 443, "token": "m-packages-a", "tls": True}, {"status": 200, "http": "HTTP/1.1 200"})
    expect("packages: files.pythonhosted.org:443", {"host": "files.pythonhosted.org", "port": 443, "token": "m-packages-a", "tls": True}, {"status": 200, "http": "HTTP/1.1"})
    expect("packages: registry.npmjs.org:443", {"host": "registry.npmjs.org", "port": 443, "token": "m-packages-a", "tls": True}, {"status": 200, "http": "HTTP/1.1"})
    expect("packages: github.com:443 is refused (host_not_allowed)", {"host": "github.com", "port": 443, "token": "m-packages-a", "tls": True}, {"status": 403, "reason": "host_not_allowed"})
    expect("packages: pypi.org:80 is refused", {"host": "pypi.org", "port": 80, "token": "m-packages-a"}, {"status": 403, "reason": "host_not_allowed"})
    expect("packages: SNI different from the CONNECT host closes the tunnel", {"host": "pypi.org", "port": 443, "token": "m-packages-a", "tls": True, "sni": "github.com"}, {"status": 200, "closed": ""})
    expect("a token whose organization allows public is clamped to packages", {"host": "github.com", "port": 443, "token": "m-public-a", "tls": True}, {"status": 403, "reason": "host_not_allowed"})
    expect("a token whose organization allows none gets nothing", {"host": "pypi.org", "port": 443, "token": "m-none-a"}, {"status": 403, "reason": "egress_disabled"})
    expect("a revoked session gets nothing", {"host": "pypi.org", "port": 443, "token": "m-revoked-a"}, {"status": 403, "reason": "hermes_session_revoked"})
    expect("no credentials: refused", {"host": "pypi.org", "port": 443, "token": ""}, {"status": 40})
    expect("internal mirror reachable by its exact host:port", {"host": MIRROR_HOST, "port": 8443, "token": "m-packages-a"}, {"status": 200, "body": "mirror-ok"})
    expect("the mirror's other port is not an exception", {"host": MIRROR_HOST, "port": 8444, "token": "m-packages-a"}, {"status": 403})
    if CLUSTER_IP_WORKS:
        info("finding", "an ipBlock with the Service address allowed the mirror here")
    else:
        info("finding", "an ipBlock with the Service address did NOT allow the mirror; the Pod address did (policy sees the Pod after Service translation)")
    direct("policy layer, packages: the guard reaches a public host on 443", guard, "1.1.1.1", 443, True)
    direct("policy layer, packages: the guard cannot reach a public host on 80", guard, "1.1.1.1", 80, False)
    direct("policy layer: the guard cannot reach a private Pod", guard, neighbour, 8080, False)
    direct("policy layer: the guard cannot reach the cluster API", guard, kc("-n", "default", "get", "svc", "kubernetes", "-o", "jsonpath={.spec.clusterIP}").stdout, 443, False)

    # the same mirror described by selectors: no address to look up
    print("--- round 2b: packages, mirror allowed by namespace and Pod selectors", flush=True)
    decoy_ip = kc("-n", MN, "get", "pod", "decoy", "-o", "jsonpath={.status.podIP}").stdout
    guard = apply_round("packages", {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": MN}},
        "podSelector": {"matchLabels": {"app": "mirror"}},
        "ports": [{"protocol": "TCP", "port": 8443}],
    })
    kc("-n", HN, "wait", "--for=condition=Ready", "pod/sb", "--timeout=120s")
    expect("selectors: the mirror is reachable by its host:port through the Service", {"host": MIRROR_HOST, "port": 8443, "token": "m-packages-a"}, {"status": 200, "body": "mirror-ok"})
    direct("selectors: the guard reaches the selected mirror Pod", guard, mirror_pod_ip, 8443, True)
    direct("selectors: the guard cannot reach another Pod of the same namespace and port", guard, decoy_ip, 8443, False)
    direct("selectors: the guard cannot reach the mirror on another port", guard, mirror_pod_ip, 8080, False)
    direct("selectors: the guard cannot reach a Pod of another namespace", guard, neighbour, 8080, False)
    guard = apply_round("packages", cidr_destination(f"{mirror_pod_ip}/32"))
    kc("-n", HN, "wait", "--for=condition=Ready", "pod/sb", "--timeout=120s")

    # real package managers with the real sandbox image
    probe_pod("sbx", "aurum-ai-hermes-sandbox:latest", "[sleep, '7200']")
    kc("-n", HN, "wait", "--for=condition=Ready", "pod/sbx", "--timeout=180s")
    proxy = "http://hermes:m-packages-b@hermes-egress:3128"
    r = kc("-n", HN, "exec", "sbx", "--", "sh", "-c",
           f"cd /tmp && pip download --no-deps -q --proxy {proxy} -d /tmp/pk requests 2>&1 | tail -2; ls /tmp/pk", check=False, timeout=240)
    check("real pip download of requests through the guard (packages)", ".whl" in r.stdout, r.stdout[-200:] + r.stderr[-100:])
    r = kc("-n", HN, "exec", "sbx", "--", "sh", "-c",
           f"HOME=/tmp npm view left-pad version --registry https://registry.npmjs.org --https-proxy {proxy} --no-update-notifier 2>&1 | tail -2", check=False, timeout=240)
    check("real npm view through the guard (packages)", r.stdout.strip().split()[-1:] and r.stdout.strip().split()[-1][0].isdigit(), r.stdout[-200:])
    r = kc("-n", HN, "exec", "sbx", "--", "sh", "-c", f"curl -sS -m 15 -o /dev/null -w '%{{http_code}}' --proxy {proxy} https://github.com 2>&1", check=False, timeout=60)
    check("curl to github.com through the guard is refused (packages)", "200" not in r.stdout and r.returncode != 0, r.stdout[-200:])
    r = kc("-n", HN, "exec", "sbx", "--", "sh", "-c", f"pip download --no-deps --no-cache-dir --retries 0 --timeout 8 --proxy http://hermes:m-none-b@hermes-egress:3128 -d /tmp/pk2 requests 2>&1 | tail -3; ls /tmp/pk2 2>/dev/null", check=False, timeout=120)
    check("real pip (no cache) with an organization at none fails", ".whl" not in r.stdout, r.stdout[-200:])

    # ---- round 3: public ------------------------------------------------------------------
    print("--- round 3: max mode public", flush=True)
    guard = apply_round("public", cidr_destination(f"{mirror_pod_ip}/32"))
    for p in ("sb",):
        kc("-n", HN, "wait", "--for=condition=Ready", f"pod/{p}", "--timeout=120s")
    expect("public: github.com:443 through the tunnel", {"host": "github.com", "port": 443, "token": "m-public-a", "tls": True}, {"status": 200, "http": "HTTP/1.1"})
    expect("public: example.com:80 plain HTTP", {"host": "example.com", "port": 80, "token": "m-public-a"}, {"status": 200, "http": "HTTP/1."})
    expect("public: another port is refused (port_not_allowed)", {"host": "github.com", "port": 8443, "token": "m-public-a"}, {"status": 403, "reason": "port_not_allowed"})
    expect("public: an organization at packages is held to packages", {"host": "github.com", "port": 443, "token": "m-packages-a", "tls": True}, {"status": 403, "reason": "host_not_allowed"})
    for label, host, port in [
        ("the metadata address", "169.254.169.254", 80),
        ("the cluster API address", kc("-n", "default", "get", "svc", "kubernetes", "-o", "jsonpath={.spec.clusterIP}").stdout, 443),
        ("a private Pod", neighbour, 80),
        ("loopback", "127.0.0.1", 80),
        ("a name that resolves to loopback (localtest.me)", "localtest.me", 80),
        ("a name that resolves to a private address (a Service name)", MIRROR_HOST, 80),
        ("IPv6 loopback", "[::1]", 80),
    ]:
        expect(f"public: {label} is refused by address", {"host": host, "port": port, "token": "m-public-a"}, {"status": 403})
    direct("policy layer, public: the guard reaches a public host on 80", guard, "1.1.1.1", 80, True)
    direct("policy layer, public: the guard cannot reach the metadata address", guard, "169.254.169.254", 80, False)

    # the periodic re-check ends an open tunnel when the mode is lowered
    def hold(token, out):
        script = PROBE.split("CASES =")[0] + (
            "import time\n"
            f"s = socket.create_connection(('hermes-egress', 3128), 5)\n"
            f"auth = base64.b64encode(b'hermes:{token}').decode()\n"
            f"s.sendall(('CONNECT {MIRROR_HOST}:8443 HTTP/1.1\\r\\nHost: x\\r\\nProxy-Authorization: Basic ' + auth + '\\r\\n\\r\\n').encode())\n"
            "buf = s.recv(4096); assert b' 200 ' in buf.split(b'\\r\\n')[0], buf\n"
            "s.sendall(b'GET /hold HTTP/1.1\\r\\nHost: m\\r\\n\\r\\n')\n"
            "t0 = time.time(); got = 0\n"
            "s.settimeout(60)\n"
            "try:\n"
            "    while True:\n"
            "        d = s.recv(4096)\n"
            "        if not d: break\n"
            "        got += len(d)\n"
            "except OSError: pass\n"
            "print(json.dumps({'open_seconds': round(time.time() - t0, 1), 'bytes': got}))\n"
        )
        out.append(sh(["kubectl", "-n", HN, "exec", "-i", "sb", "--", "python3", "-"], inp=script, check=False, timeout=120).stdout)

    for how, newmode in (("the organization lowers the mode to none", "none"), ("the session is revoked", "revoked")):
        token = f"m-public-hold-{newmode}"
        out: list = []
        t = threading.Thread(target=hold, args=(token, out))
        t.start()
        time.sleep(8)
        set_mode(token, newmode)
        t.join(70)
        try:
            res = json.loads(out[0].strip().splitlines()[-1])
            check(f"an open tunnel ends within the re-check when {how}", 8 <= res["open_seconds"] <= 40 and res["bytes"] > 0, json.dumps(res))
        except Exception:
            check(f"an open tunnel ends within the re-check when {how}", False, str(out)[-200:])

    # ---- round 4: none --------------------------------------------------------------------------------
    print("--- round 4: max mode none", flush=True)
    guard = apply_round("none", cidr_destination(f"{mirror_pod_ip}/32"))
    kc("-n", HN, "wait", "--for=condition=Ready", "pod/sb", "--timeout=120s")
    expect("none: even a public organization gets nothing (egress_disabled)", {"host": "pypi.org", "port": 443, "token": "m-public-a"}, {"status": 403, "reason": "egress_disabled"})
    expect("none: the internal mirror is refused too", {"host": MIRROR_HOST, "port": 8443, "token": "m-public-a"}, {"status": 403, "reason": "egress_disabled"})
    direct("policy layer, none: the guard cannot reach any public host", guard, "1.1.1.1", 443, False)
    direct("policy layer, none: the guard still reaches the backend", guard, "rel-backend.platform.svc", 8000, True)
    r = kc("-n", HN, "get", "networkpolicy", "rel-hermes-egress-egress", "-o", "json").stdout
    rules = json.loads(r)["spec"]["egress"]
    check("none: the chart renders no internet or mirror rule", not [x for x in rules if "ipBlock" in x["to"][0]], str(rules)[:200])
    print(f"{FAILED} check(s) failed" if FAILED else "all checks passed", flush=True)


try:
    main()
except Exception as exc:
    import traceback

    traceback.print_exc()
    print(f"FAIL  crashed: {type(exc).__name__}: {exc}", flush=True)
    FAILED += 1
finally:
    if os.environ.get("KEEP_HERMES_K8S_CLUSTER") != "1":
        sh(["k3d", "cluster", "delete", CLUSTER], check=False)
sys.exit(1 if FAILED else 0)
