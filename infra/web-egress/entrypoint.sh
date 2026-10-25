#!/bin/sh
set -eu

readonly bootstrap_rules=/etc/web-egress/bootstrap.nft
if [ "${WEB_EGRESS_MODE:-}" = hermes ]; then
    readonly final_rules=/etc/web-egress/hermes-rules.nft
else
    readonly final_rules=/etc/web-egress/rules.nft
fi
readonly bootstrap_generated=/run/web-egress-bootstrap.nft
readonly final_generated=/run/web-egress-final.nft

apply_only=false
if [ "$#" -gt 0 ]; then
    if [ "$#" -ne 1 ] || [ "$1" != "--apply-only" ]; then
        echo "usage: web-egress-entrypoint [--apply-only]" >&2
        exit 2
    fi
    apply_only=true
fi

python3 - "$bootstrap_rules" "$final_rules" "$bootstrap_generated" "$final_generated" <<'PY'
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time


BOOTSTRAP_RULES = Path(sys.argv[1])
FINAL_RULES = Path(sys.argv[2])
BOOTSTRAP_GENERATED = Path(sys.argv[3])
FINAL_GENERATED = Path(sys.argv[4])
MODE = os.environ.get("WEB_EGRESS_MODE", "")
if MODE not in {"web-tools", "searxng", "hermes"}:
    raise SystemExit("WEB_EGRESS_MODE must be web-tools, searxng or hermes")


def port(name: str) -> int:
    try:
        value = int(os.environ[name])
    except (KeyError, ValueError) as exc:
        raise SystemExit(f"{name} must be an integer port") from exc
    if value < 1 or value > 65535:
        raise SystemExit(f"{name} must be between 1 and 65535")
    return value


def addresses(host: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    if not host or any(character.isspace() for character in host):
        raise SystemExit("peer hostname must be non-empty and contain no whitespace")
    deadline = time.monotonic() + 60
    while True:
        try:
            rows = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            values = tuple(dict.fromkeys(ipaddress.ip_address(row[4][0]) for row in rows))
            if values:
                return values
        except socket.gaierror:
            pass
        if time.monotonic() >= deadline:
            raise SystemExit(f"could not resolve required peer {host!r}")
        time.sleep(1)


def resolvers() -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    values: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for line in Path("/etc/resolv.conf").read_text().splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == "nameserver":
            try:
                values.append(ipaddress.ip_address(fields[1]))
            except ValueError as exc:
                raise SystemExit("resolv.conf contains a non-IP nameserver") from exc
    if not values:
        raise SystemExit("resolv.conf contains no exact DNS resolver")
    return tuple(dict.fromkeys(values))


def gateways() -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    result = subprocess.run(
        ["ip", "-json", "route", "show", "table", "all", "default"],
        check=True,
        text=True,
        capture_output=True,
    )
    values = []
    for route in json.loads(result.stdout):
        gateway = route.get("gateway")
        if gateway:
            values.append(ipaddress.ip_address(gateway))
    links = subprocess.run(
        ["ip", "-json", "address", "show"],
        check=True,
        text=True,
        capture_output=True,
    )
    for interface in json.loads(links.stdout):
        if interface.get("ifname") == "lo":
            continue
        for address in interface.get("addr_info", []):
            local = address.get("local")
            prefixlen = address.get("prefixlen")
            if local is None or prefixlen is None:
                continue
            network = ipaddress.ip_network(f"{local}/{prefixlen}", strict=False)
            candidate = network.network_address + 1
            if candidate != ipaddress.ip_address(local):
                values.append(candidate)
    return tuple(dict.fromkeys(values))


def dns_listener_sockets(
    dns: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...],
) -> dict[str, tuple[str, ...]]:
    result = subprocess.run(
        ["ss", "-H", "-lnut"], check=True, text=True, capture_output=True
    )
    found: dict[str, list[str]] = {
        "dns_listener_udp_v4": [],
        "dns_listener_tcp_v4": [],
        "dns_listener_udp_v6": [],
        "dns_listener_tcp_v6": [],
    }
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0] not in {"udp", "tcp"}:
            continue
        raw_address, raw_port = fields[-2].rsplit(":", 1)
        raw_address = raw_address.strip("[]")
        try:
            address = ipaddress.ip_address(raw_address)
            socket_port = int(raw_port)
        except ValueError:
            continue
        if address not in dns:
            continue
        family = "v4" if address.version == 4 else "v6"
        found[f"dns_listener_{fields[0]}_{family}"].append(f"{address} . {socket_port}")
    return {name: tuple(values) for name, values in found.items()}


def operator_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    raw = os.environ.get("WEB_EGRESS_FORBIDDEN_CIDRS", "").strip()
    if not raw:
        return ()
    values = []
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            raise SystemExit("WEB_EGRESS_FORBIDDEN_CIDRS contains an empty item")
        try:
            values.append(ipaddress.ip_network(candidate, strict=True))
        except ValueError as exc:
            raise SystemExit(
                "WEB_EGRESS_FORBIDDEN_CIDRS must contain canonical IPv4/IPv6 CIDRs"
            ) from exc
    return tuple(values)


def emit_addresses(lines: list[str], set_name: str, values: tuple[object, ...]) -> None:
    if values:
        lines.append(
            f"add element inet web_egress {set_name} {{ {', '.join(map(str, values))} }}"
        )


def emit_peers(
    lines: list[str],
    set_v4: str,
    set_v6: str,
    values: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...],
    peer_port: int,
) -> None:
    emit_addresses(
        lines,
        set_v4,
        tuple(f"{value} . {peer_port}" for value in values if value.version == 4),
    )
    emit_addresses(
        lines,
        set_v6,
        tuple(f"{value} . {peer_port}" for value in values if value.version == 6),
    )


def install(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")
    subprocess.run(["nft", "--check", "--file", str(path)], check=True)
    subprocess.run(["nft", "--file", str(path)], check=True)


def install_bootstrap(dns: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]) -> None:
    # A fresh namespace first receives a minimal DNS-only default-drop table.
    # It remains in force while required peers resolve and if final
    # validation fails.
    if subprocess.run(
        ["nft", "list", "table", "inet", "web_egress"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode != 0:
        bootstrap = [f'include "{BOOTSTRAP_RULES}"']
        emit_addresses(bootstrap, "dns_v4", tuple(value for value in dns if value.version == 4))
        emit_addresses(bootstrap, "dns_v6", tuple(value for value in dns if value.version == 6))
        for set_name, values in dns_listener_sockets(dns).items():
            emit_addresses(bootstrap, set_name, values)
        install(BOOTSTRAP_GENERATED, bootstrap)


def canonical_networks(name: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    values = []
    for item in os.environ.get(name, "").split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            values.append(ipaddress.ip_network(candidate, strict=True))
        except ValueError as exc:
            raise SystemExit(f"{name} must contain canonical IPv4/IPv6 CIDRs") from exc
    return tuple(values)


def hermes() -> None:
    """hermes-egress: sandbox CIDRs reach only the proxy ports; out go DNS for
    the proxy's own resolution, the backend, explicit internal package mirrors
    and, up to the operator maximum, public 443 (packages) or 80 and 443."""
    ready_port = port("WEB_EGRESS_READY_PORT")
    sandbox_ports = (port("HERMES_EGRESS_PLATFORM_PORT"), port("HERMES_EGRESS_PROXY_PORT"))
    sandboxes = canonical_networks("HERMES_EGRESS_SANDBOX_CIDRS")
    if not sandboxes:
        raise SystemExit("HERMES_EGRESS_SANDBOX_CIDRS must name the sandbox networks")
    max_mode = os.environ.get("HERMES_EGRESS_MAX_MODE", "")
    public_ports = {"none": (), "packages": (443,), "public": (80, 443)}.get(max_mode)
    if public_ports is None:
        raise SystemExit("HERMES_EGRESS_MAX_MODE must be none, packages or public")
    peers = [(os.environ.get("HERMES_EGRESS_BACKEND_HOST", "").strip(), port("HERMES_EGRESS_BACKEND_PORT"))]
    for item in os.environ.get("HERMES_EGRESS_INTERNAL_PACKAGE_HOSTS", "").split(","):
        if item.strip():
            host, sep, raw_port = item.strip().rpartition(":")
            if not sep or not raw_port.isdigit() or not 0 < int(raw_port) < 65536:
                raise SystemExit("HERMES_EGRESS_INTERNAL_PACKAGE_HOSTS entries must be host:port")
            peers.append((host, int(raw_port)))
    operator = operator_networks()
    dns = resolvers()
    install_bootstrap(dns)
    internal = [(value, peer_port) for host, peer_port in peers for value in addresses(host)]
    host_gateways = gateways()
    final = ["delete table inet web_egress", f'include "{FINAL_RULES}"']
    emit_addresses(final, "local_tcp_ports", (ready_port,))
    emit_addresses(final, "sandbox_tcp_ports", sandbox_ports)
    emit_addresses(final, "public_tcp_ports", public_ports)
    emit_addresses(final, "sandbox_v4", tuple(value for value in sandboxes if value.version == 4))
    emit_addresses(final, "sandbox_v6", tuple(value for value in sandboxes if value.version == 6))
    emit_addresses(final, "dns_v4", tuple(value for value in dns if value.version == 4))
    emit_addresses(final, "dns_v6", tuple(value for value in dns if value.version == 6))
    for set_name, values in dns_listener_sockets(dns).items():
        emit_addresses(final, set_name, values)
    emit_addresses(final, "host_gateway_v4", tuple(v for v in host_gateways if v.version == 4))
    emit_addresses(final, "host_gateway_v6", tuple(v for v in host_gateways if v.version == 6))
    emit_addresses(final, "operator_forbidden_v4", tuple(v for v in operator if v.version == 4))
    emit_addresses(final, "operator_forbidden_v6", tuple(v for v in operator if v.version == 6))
    emit_addresses(
        final, "internal_tcp_v4", tuple(f"{v} . {p}" for v, p in internal if v.version == 4)
    )
    emit_addresses(
        final, "internal_tcp_v6", tuple(f"{v} . {p}" for v, p in internal if v.version == 6)
    )
    install(FINAL_GENERATED, final)


if MODE == "hermes":
    hermes()
    raise SystemExit(0)

local_port = port("WEB_EGRESS_LOCAL_PORT")
ready_port = port("WEB_EGRESS_READY_PORT")
ingress_port = port("WEB_EGRESS_INGRESS_PORT")
operator = operator_networks()
internal_host = os.environ.get("WEB_EGRESS_INTERNAL_HOST", "").strip()
internal_port_raw = os.environ.get("WEB_EGRESS_INTERNAL_PORT", "").strip()
if MODE == "web-tools":
    if not internal_host or not internal_port_raw:
        raise SystemExit("web-tools mode requires an exact internal host and port")
    internal_port = port("WEB_EGRESS_INTERNAL_PORT")
elif internal_host or internal_port_raw:
    raise SystemExit("searxng mode must not receive an internal outbound exception")

dns = resolvers()
dns_listeners = dns_listener_sockets(dns)

# A fresh namespace first receives a minimal DNS-only default-drop table. This
# remains in force while required peers resolve and if final validation fails.
if subprocess.run(
    ["nft", "list", "table", "inet", "web_egress"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
).returncode != 0:
    bootstrap = [f'include "{BOOTSTRAP_RULES}"']
    emit_addresses(bootstrap, "dns_v4", tuple(value for value in dns if value.version == 4))
    emit_addresses(bootstrap, "dns_v6", tuple(value for value in dns if value.version == 6))
    for set_name, values in dns_listeners.items():
        emit_addresses(bootstrap, set_name, values)
    install(BOOTSTRAP_GENERATED, bootstrap)

ingress = addresses(os.environ.get("WEB_EGRESS_INGRESS_HOST", ""))
internal = addresses(internal_host) if MODE == "web-tools" else ()
host_gateways = gateways()

# Delete and recreate the owned table in one nft transaction. Unlike `flush
# table`, this cannot retain resolver, peer, port, or operator set elements.
final = ["delete table inet web_egress", f'include "{FINAL_RULES}"']
emit_addresses(final, "local_tcp_ports", (local_port, ready_port))
emit_addresses(final, "dns_v4", tuple(value for value in dns if value.version == 4))
emit_addresses(final, "dns_v6", tuple(value for value in dns if value.version == 6))
for set_name, values in dns_listeners.items():
    emit_addresses(final, set_name, values)
emit_addresses(
    final,
    "host_gateway_v4",
    tuple(value for value in host_gateways if value.version == 4),
)
emit_addresses(
    final,
    "host_gateway_v6",
    tuple(value for value in host_gateways if value.version == 6),
)
emit_addresses(
    final,
    "operator_forbidden_v4",
    tuple(value for value in operator if value.version == 4),
)
emit_addresses(
    final,
    "operator_forbidden_v6",
    tuple(value for value in operator if value.version == 6),
)
emit_peers(final, "ingress_tcp_v4", "ingress_tcp_v6", ingress, ingress_port)
if MODE == "web-tools":
    emit_peers(final, "internal_tcp_v4", "internal_tcp_v6", internal, internal_port)
install(FINAL_GENERATED, final)
PY

if [ "$apply_only" = true ]; then
    exit 0
fi

exec python3 /usr/local/bin/web-egress-readiness \
    --serve --port "$WEB_EGRESS_READY_PORT"
