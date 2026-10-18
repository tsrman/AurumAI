"""Exact endpoint egress for a separate, unprivileged maintenance process.

The worker joins this network namespace only after the atomic ruleset exists.
DNS changes require restarting guard and worker; stale addresses fail closed.
"""

import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit


def gateway_peer(value):
    url = urlsplit(value)
    if url.scheme not in {"http", "https"} or not url.hostname:
        raise ValueError("invalid gateway endpoint")
    port = url.port if url.port is not None else (443 if url.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise ValueError("invalid gateway port")
    return url.hostname, port


def configured_targets(environ):
    if "MAINTENANCE_TCP_ENDPOINTS" not in environ:
        return [
            (environ.get("MAINTENANCE_DATABASE_HOST", "postgres"), 5432),
            (environ.get("MAINTENANCE_TEMPORAL_HOST", "temporal"), 7233),
            gateway_peer(environ["LLM_GATEWAY_URL"]),
        ]
    try:
        raw = json.loads(environ["MAINTENANCE_TCP_ENDPOINTS"])
        urls = json.loads(environ.get("MAINTENANCE_HTTP_ENDPOINTS", "[]"))
        if not isinstance(raw, list) or not 1 <= len(raw) <= 32:
            raise ValueError
        if not isinstance(urls, list) or len(urls) > 32:
            raise ValueError
        targets = []
        for endpoint in raw:
            if not isinstance(endpoint, list) or len(endpoint) != 2:
                raise ValueError
            host, port = endpoint
            if not isinstance(host, str) or not host or len(host) > 255:
                raise ValueError
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError
            targets.append((host, port))
        for value in urls:
            if not isinstance(value, str) or len(value) > 2048:
                raise ValueError
            targets.append(gateway_peer(value))
        return targets
    except (ValueError, TypeError, KeyError):
        raise ValueError("invalid maintenance endpoints") from None


def rules_for(resolvers, peers):
    dns = [ipaddress.ip_address(value) for value in resolvers]
    validated = [(ipaddress.ip_address(address), int(port)) for address, port in peers]
    if not dns or not validated or any(not 1 <= port <= 65535 for _, port in validated):
        raise ValueError("missing or invalid allowed endpoints")
    lines = ["table inet maintenance {"]
    for version in (4, 6):
        addresses = ", ".join(str(value) for value in dns if value.version == version)
        endpoints = ", ".join(
            f"{address} . {port}" for address, port in validated if address.version == version
        )
        dns_elements = f"elements = {{ {addresses} }};" if addresses else ""
        peer_elements = f"elements = {{ {endpoints} }};" if endpoints else ""
        lines.append(f"set dns{version} {{ type ipv{version}_addr; {dns_elements} }}")
        lines.append(
            f"set peers{version} {{ type ipv{version}_addr . inet_service; {peer_elements} }}"
        )
    lines.extend(
        [
            "chain input { type filter hook input priority 0; policy drop;",
            'iifname "lo" accept',
            "ct state established,related accept",
            "}",
            "chain output { type filter hook output priority 0; policy drop;",
            'oifname "lo" accept',
            "ct state established,related accept",
            "ip daddr @dns4 udp dport 53 accept",
            "ip daddr @dns4 tcp dport 53 accept",
            "ip6 daddr @dns6 udp dport 53 accept",
            "ip6 daddr @dns6 tcp dport 53 accept",
            "ip daddr . tcp dport @peers4 accept",
            "ip6 daddr . tcp dport @peers6 accept",
            "}",
            "}",
        ]
    )
    return "\n".join(lines)


def main():
    if sys.argv[1:] == ["check"]:
        result = subprocess.run(
            ["nft", "list", "table", "inet", "maintenance"], capture_output=True
        )
        raise SystemExit(result.returncode)
    targets = configured_targets(os.environ)
    resolvers = [
        line.split()[1]
        for line in Path("/etc/resolv.conf").read_text().splitlines()
        if line.startswith("nameserver ")
    ]
    peers = set()
    for host, port in targets:
        for family, kind, protocol, canonical, address in socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        ):
            peers.add((address[0], port))
    rules = rules_for(resolvers, sorted(peers))
    exists = (
        subprocess.run(
            ["nft", "list", "table", "inet", "maintenance"], capture_output=True
        ).returncode
        == 0
    )
    if exists:
        rules = "delete table inet maintenance\n" + rules
    subprocess.run(["nft", "-f", "-"], input=rules, text=True, check=True)
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
