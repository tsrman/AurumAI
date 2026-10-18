"""Deterministic URL/IP policy applied before every outbound connection."""

from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_network,
)
import re
from urllib.parse import SplitResult, urlsplit, urlunsplit

import idna


class URLBlocked(ValueError):
    pass


# Frozen special-use ranges. Do not delegate this boundary solely to
# ipaddress.is_global: Python releases have changed that classification.
_FORBIDDEN_NETWORKS = tuple(
    ip_network(value)
    for value in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/23",
        "2001:db8::/32",
        # 6to4 encapsulates arbitrary IPv4 (including private space) behind a
        # relay, and 3fff::/20 is reserved documentation space that
        # is_global still reports as global. Neither may rely on is_global.
        "2002::/16",
        "3fff::/20",
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
    )
)
_AMBIGUOUS_NUMERIC_HOST = re.compile(r"(?:0[xX][0-9A-Fa-f]+|[0-9.]+)")


def _canonical_host(host: str) -> tuple[str, IPv4Address | IPv6Address | None]:
    if not host or "%" in host or "\\" in host:
        raise URLBlocked("web_url_blocked")
    if host.endswith("."):
        host = host[:-1]
    if not host:
        raise URLBlocked("web_url_blocked")
    try:
        literal = ip_address(host)
    except ValueError:
        if _AMBIGUOUS_NUMERIC_HOST.fullmatch(host):
            raise URLBlocked("web_url_blocked")
        try:
            canonical = idna.encode(host, uts46=False, std3_rules=True).decode("ascii").lower()
        except idna.IDNAError as exc:
            raise URLBlocked("web_url_blocked") from exc
        return canonical, None
    if isinstance(literal, IPv6Address) and literal.ipv4_mapped is not None:
        raise URLBlocked("web_url_blocked")
    return literal.compressed, literal


def _canonical_url(url: str) -> tuple[str, IPv4Address | IPv6Address | None]:
    if (
        not url
        or url != url.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in url)
        or "\\" in url
    ):
        raise URLBlocked("web_url_blocked")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise URLBlocked("web_url_blocked") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None:
        raise URLBlocked("web_url_blocked")
    if not parsed.hostname or "%" in parsed.netloc:
        raise URLBlocked("web_url_blocked")
    expected_port = 80 if scheme == "http" else 443
    if port is not None and port != expected_port:
        raise URLBlocked("web_url_blocked")
    host, literal = _canonical_host(parsed.hostname)
    netloc = f"[{host}]" if ":" in host else host
    path = parsed.path or "/"
    canonical = urlunsplit(SplitResult(scheme, netloc, path, parsed.query, ""))
    return canonical, literal


def canonicalize_url(url: str) -> str:
    """Reject invalid URL syntax before any resolver or network I/O."""

    canonical, _literal = _canonical_url(url)
    return canonical


def _blocked_address(
    address: IPv4Address | IPv6Address,
    operator_networks: tuple[IPv4Network | IPv6Network, ...],
) -> bool:
    if not address.is_global or any(address in network for network in _FORBIDDEN_NETWORKS):
        return True
    return any(address.version == network.version and address in network for network in operator_networks)


def validate_target(
    url: str,
    addresses: tuple[str, ...],
    forbidden_cidrs: tuple[str, ...] = (),
) -> str:
    canonical, literal = _canonical_url(url)
    if not addresses:
        raise URLBlocked("web_url_blocked")
    try:
        resolved = tuple(ip_address(value) for value in addresses)
        operator_networks = tuple(ip_network(value, strict=True) for value in forbidden_cidrs)
    except ValueError as exc:
        raise URLBlocked("web_url_blocked") from exc
    if literal is not None and any(address != literal for address in resolved):
        raise URLBlocked("web_url_blocked")
    if any(
        (isinstance(address, IPv6Address) and address.ipv4_mapped is not None)
        or _blocked_address(address, operator_networks)
        for address in resolved
    ):
        raise URLBlocked("web_url_blocked")
    return canonical
