"""Render the web-egress guard's generated nftables files without a namespace.

The entrypoint's embedded generator runs in a child interpreter with fixed
fakes for `ip`, `ss`, `nft`, name resolution and resolv.conf, so a test can
compare exactly what each mode would install.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "infra" / "web-egress" / "entrypoint.sh"

_CHILD = r'''
import json, os, pathlib, socket, subprocess, sys, types
spec = json.loads(os.environ["FAKE_SPEC"])
real_read = pathlib.Path.read_text
def read_text(self, *a, **k):
    if str(self) == "/etc/resolv.conf":
        return "nameserver 127.0.0.11\n"
    return real_read(self, *a, **k)
pathlib.Path.read_text = read_text
def getaddrinfo(host, *a, **k):
    if host not in spec["hosts"]:
        raise socket.gaierror(host)
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in spec["hosts"][host]]
socket.getaddrinfo = getaddrinfo
class Done:
    def __init__(self, out=""):
        self.stdout, self.returncode = out, 0
def run(cmd, **k):
    if cmd[:3] == ["ip", "-json", "route"]:
        return Done(json.dumps([{"gateway": "172.30.0.1"}]))
    if cmd[:3] == ["ip", "-json", "address"]:
        return Done(json.dumps([{"ifname": "eth0", "addr_info": [{"local": "172.30.0.5", "prefixlen": 24}]}]))
    if cmd[:2] == ["ss", "-H"]:
        return Done("udp UNCONN 0 0 127.0.0.11:53001 0.0.0.0:*\ntcp LISTEN 0 4096 127.0.0.11:40001 0.0.0.0:*\n")
    if cmd[:3] == ["nft", "list", "table"]:
        d = Done(); d.returncode = 1; return d
    return Done()
subprocess.run = run
code = sys.stdin.read()
sys.argv = ["generator", *spec["argv"]]
exec(compile(code, "web-egress-generator", "exec"), {"__name__": "__main__"})
'''


def generator_source() -> str:
    text = ENTRYPOINT.read_text()
    start = text.index("<<'PY'\n") + len("<<'PY'\n")
    return text[start : text.index("\nPY\n", start)]


def render(env: dict[str, str], hosts: dict[str, list[str]]) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as directory:
        paths = [
            str(ROOT / "infra" / "web-egress" / "bootstrap.nft"),
            # The shell wrapper picks the template by mode, as in the image.
            str(
                ROOT
                / "infra"
                / "web-egress"
                / ("hermes-rules.nft" if env.get("WEB_EGRESS_MODE") == "hermes" else "rules.nft")
            ),
            f"{directory}/bootstrap.nft",
            f"{directory}/final.nft",
        ]
        spec = {"hosts": hosts, "argv": paths}
        subprocess.run(
            [sys.executable, "-c", _CHILD],
            input=generator_source(),
            text=True,
            check=True,
            env={"PATH": os.environ.get("PATH", ""), "FAKE_SPEC": json.dumps(spec), **env},
        )
        source = str(ROOT / "infra" / "web-egress")
        return {
            kind: Path(path).read_text().replace(source, "/etc/web-egress")
            for kind, path in (("bootstrap", paths[2]), ("final", paths[3]))
        }


WEB_TOOLS_ENV = {
    "WEB_EGRESS_MODE": "web-tools",
    "WEB_EGRESS_LOCAL_PORT": "8120",
    "WEB_EGRESS_READY_PORT": "8190",
    "WEB_EGRESS_INGRESS_HOST": "web-tools-backend",
    "WEB_EGRESS_INGRESS_PORT": "8120",
    "WEB_EGRESS_INTERNAL_HOST": "searxng",
    "WEB_EGRESS_INTERNAL_PORT": "8080",
    "WEB_EGRESS_FORBIDDEN_CIDRS": "203.0.114.0/24",
}
SEARXNG_ENV = {
    "WEB_EGRESS_MODE": "searxng",
    "WEB_EGRESS_LOCAL_PORT": "8080",
    "WEB_EGRESS_READY_PORT": "8191",
    "WEB_EGRESS_INGRESS_HOST": "web-tools",
    "WEB_EGRESS_INGRESS_PORT": "8080",
}
HOSTS = {"web-tools-backend": ["172.30.0.10"], "searxng": ["172.30.0.11"], "web-tools": ["172.30.0.12"]}


if __name__ == "__main__":
    out = Path(sys.argv[1])
    for name, env in (("web-tools", WEB_TOOLS_ENV), ("searxng", SEARXNG_ENV)):
        for kind, text in render(env, HOSTS).items():
            (out / f"{name}.{kind}.nft").write_text(text)
