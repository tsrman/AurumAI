"""The shared web-egress guard: the Hermes mode, and unchanged web modes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import web_egress_rules as rules  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "web-egress"
HERMES_RULES = rules.ROOT / "infra" / "web-egress" / "hermes-rules.nft"
HERMES_ENV = {
    "WEB_EGRESS_MODE": "hermes",
    "WEB_EGRESS_READY_PORT": "8192",
    "HERMES_EGRESS_PLATFORM_PORT": "8080",
    "HERMES_EGRESS_PROXY_PORT": "3128",
    "HERMES_EGRESS_SANDBOX_CIDRS": "172.31.250.0/24",
    "HERMES_EGRESS_BACKEND_HOST": "backend",
    "HERMES_EGRESS_BACKEND_PORT": "8000",
    "HERMES_EGRESS_MAX_MODE": "packages",
    "WEB_EGRESS_FORBIDDEN_CIDRS": "203.0.114.0/24",
}
HOSTS = {**rules.HOSTS, "backend": ["172.30.0.20"], "mirror.internal": ["10.9.0.4"]}


@pytest.mark.parametrize(
    ("name", "env"), [("web-tools", rules.WEB_TOOLS_ENV), ("searxng", rules.SEARXNG_ENV)]
)
def test_web_modes_generate_exactly_the_recorded_rules(name: str, env: dict[str, str]) -> None:
    rendered = rules.render(env, rules.HOSTS)

    for kind in ("bootstrap", "final"):
        assert rendered[kind] == (FIXTURES / f"{name}.{kind}.nft").read_text()


def _hermes(**overrides: str) -> list[str]:
    final = rules.render({**HERMES_ENV, **overrides}, HOSTS)["final"]
    return final.splitlines()


def test_hermes_mode_opens_only_the_proxy_ports_to_sandboxes() -> None:
    lines = _hermes()

    assert lines[1] == 'include "/etc/web-egress/hermes-rules.nft"'
    assert "add element inet web_egress sandbox_v4 { 172.31.250.0/24 }" in lines
    assert "add element inet web_egress sandbox_tcp_ports { 8080, 3128 }" in lines
    assert "add element inet web_egress local_tcp_ports { 8192 }" in lines
    assert "add element inet web_egress internal_tcp_v4 { 172.30.0.20 . 8000 }" in lines
    assert "add element inet web_egress operator_forbidden_v4 { 203.0.114.0/24 }" in lines


@pytest.mark.parametrize(
    ("maximum", "ports"), [("none", None), ("packages", "{ 443 }"), ("public", "{ 80, 443 }")]
)
def test_public_ports_follow_the_operator_maximum(maximum: str, ports: str | None) -> None:
    lines = [line for line in _hermes(HERMES_EGRESS_MAX_MODE=maximum) if "public_tcp_ports" in line]

    assert lines == ([] if ports is None else [f"add element inet web_egress public_tcp_ports {ports}"])


def test_internal_mirrors_are_exact_exceptions() -> None:
    lines = _hermes(HERMES_EGRESS_INTERNAL_PACKAGE_HOSTS="mirror.internal:8443")

    assert (
        "add element inet web_egress internal_tcp_v4 { 172.30.0.20 . 8000, 10.9.0.4 . 8443 }" in lines
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"HERMES_EGRESS_SANDBOX_CIDRS": ""},
        {"HERMES_EGRESS_SANDBOX_CIDRS": "172.31.250.1/24"},
        {"HERMES_EGRESS_MAX_MODE": "everything"},
        {"HERMES_EGRESS_INTERNAL_PACKAGE_HOSTS": "mirror.internal"},
    ],
)
def test_hermes_mode_refuses_incomplete_configuration(overrides: dict[str, str]) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        rules.render({**HERMES_ENV, **overrides}, HOSTS)


def test_hermes_template_differs_from_the_web_template_only_in_its_sets() -> None:
    web = (rules.ROOT / "infra" / "web-egress" / "rules.nft").read_text()
    hermes = HERMES_RULES.read_text()

    assert "tcp dport { 80, 443 } accept" in web
    assert "tcp dport { 80, 443 } accept" not in hermes
    assert "tcp dport @public_tcp_ports accept" in hermes
    assert "ingress_tcp" not in hermes
    for reject in ("@forbidden_v4 reject", "@forbidden_v6 reject", "@operator_forbidden_v4 reject"):
        assert reject in hermes


def test_image_ships_the_hermes_proxy_and_template() -> None:
    dockerfile = (rules.ROOT / "infra" / "web-egress" / "Dockerfile").read_text()

    assert "COPY hermes_proxy.py /usr/local/bin/hermes-egress-proxy" in dockerfile
    assert "COPY hermes-rules.nft /etc/web-egress/hermes-rules.nft" in dockerfile
