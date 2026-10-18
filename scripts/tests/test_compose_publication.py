from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "docker-compose.yml"
PROXY = ROOT / "docker-compose.proxy.yml"
TRAEFIK_DYNAMIC = ROOT / "infra/traefik/dynamic/aurumai.yml"
DOMAIN = "example.test"

# Browser-facing services may follow PUBLIC_BIND_ADDRESS; infrastructure never does.
PUBLIC_SERVICES = {
    "keycloak", "temporal-ui", "backend", "backend-admin", "frontend", "admin-ui",
    "langfuse-server",
}
# Reference MCP servers (mcp-fetch is an unrestricted fetcher) never widen.
LOOPBACK_FIXTURES = {"mcp-everything", "mcp-fetch"}
REQUIRED = {
    "APP_SECRET_KEY": "compose-publication-secret",
    "KEYCLOAK_ADMIN_PASSWORD": "compose-publication-keycloak",
}
PROXY_REQUIRED = {
    "PUBLIC_DOMAIN": DOMAIN,
    "ACME_EMAIL": "operator@example.test",
    "TEMPORAL_UI_BASIC_AUTH_PASSWORD": "compose-publication-temporal",
}


def render(tmp_path: Path, values: dict[str, str], *files: Path) -> subprocess.CompletedProcess[str]:
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    inherited = {
        key: value for key, value in os.environ.items()
        if key not in {*REQUIRED, *PROXY_REQUIRED, "PUBLIC_BIND_ADDRESS", "PUBLIC_HOST"}
    }
    command = ["docker", "compose", "--env-file", str(env_file)]
    for path in files or (BASE,):
        command += ["-f", str(path)]
    command += ["--profile", "*", "config", "--format", "json"]
    return subprocess.run(command, env=inherited, capture_output=True, text=True, timeout=60)


def config(tmp_path: Path, values: dict[str, str], *files: Path) -> dict:
    result = render(tmp_path, values, *files)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def published(services: dict) -> dict[str, list[tuple[str, str]]]:
    return {
        name: [(port.get("host_ip", "0.0.0.0"), str(port["published"])) for port in service.get("ports", [])]
        for name, service in services.items() if service.get("ports")
    }


def test_every_published_port_is_loopback_by_default(tmp_path: Path):
    ports = published(config(tmp_path, REQUIRED)["services"])
    assert PUBLIC_SERVICES | LOOPBACK_FIXTURES <= set(ports)
    exposed = {name: binding for name, bindings in ports.items() for binding in bindings if binding[0] != "127.0.0.1"}
    assert exposed == {}


def test_public_bind_address_opens_only_browser_facing_services(tmp_path: Path):
    ports = published(config(tmp_path, {**REQUIRED, "PUBLIC_BIND_ADDRESS": "0.0.0.0"})["services"])
    for name, bindings in ports.items():
        expected = "0.0.0.0" if name in PUBLIC_SERVICES else "127.0.0.1"
        assert {host for host, _ in bindings} == {expected}, name


def test_keycloak_admin_password_is_required_and_never_a_literal_default(tmp_path: Path):
    missing = render(tmp_path, {"APP_SECRET_KEY": "compose-publication-secret"})
    assert missing.returncode != 0
    assert "KEYCLOAK_ADMIN_PASSWORD" in missing.stderr
    keycloak = config(tmp_path, REQUIRED)["services"]["keycloak"]["environment"]
    assert keycloak["KC_BOOTSTRAP_ADMIN_PASSWORD"] == REQUIRED["KEYCLOAK_ADMIN_PASSWORD"]


def test_env_example_declares_publication_settings_without_values():
    example = (ROOT / ".env.example").read_text()
    for key in ("KEYCLOAK_ADMIN_PASSWORD", "TEMPORAL_UI_BASIC_AUTH_PASSWORD", "PUBLIC_DOMAIN", "ACME_EMAIL"):
        assert re.search(rf"^{key}=$", example, re.MULTILINE), key
    assert re.search(r"^PUBLIC_BIND_ADDRESS=127\.0\.0\.1$", example, re.MULTILINE)


def proxy_config(tmp_path: Path) -> dict:
    return config(tmp_path, {**REQUIRED, **PROXY_REQUIRED}, BASE, PROXY)


@pytest.mark.parametrize("missing", sorted(PROXY_REQUIRED))
def test_proxy_overlay_requires_domain_acme_contact_and_dashboard_password(tmp_path: Path, missing: str):
    values = {**REQUIRED, **PROXY_REQUIRED}
    del values[missing]
    result = render(tmp_path, values, BASE, PROXY)
    assert result.returncode != 0
    assert missing in result.stderr


def test_proxy_is_the_only_publicly_published_service(tmp_path: Path):
    services = proxy_config(tmp_path)["services"]
    ports = published(services)
    assert sorted(ports["traefik"]) == [("0.0.0.0", "443"), ("0.0.0.0", "80")]
    others = {name: bindings for name, bindings in ports.items() if name != "traefik"}
    assert all(host == "127.0.0.1" for bindings in others.values() for host, _ in bindings)
    # File provider only: the edge proxy never receives Docker API authority.
    mounts = [volume.get("source", "") for volume in services["traefik"].get("volumes", [])]
    assert not any("docker.sock" in source for source in mounts)


def test_proxy_overlay_switches_browser_urls_identity_and_cors_to_https_hosts(tmp_path: Path):
    services = proxy_config(tmp_path)["services"]
    frontend = services["frontend"]["environment"]
    assert frontend["RUNTIME_API_URL"] == f"https://api.{DOMAIN}/v1"
    assert frontend["RUNTIME_KEYCLOAK_URL"] == f"https://auth.{DOMAIN}"
    assert frontend["RUNTIME_ADMIN_URL"] == f"https://admin.{DOMAIN}"
    admin_ui = services["admin-ui"]["environment"]
    assert admin_ui["RUNTIME_API_URL"] == f"https://admin-api.{DOMAIN}/v1"
    assert admin_ui["RUNTIME_KEYCLOAK_URL"] == f"https://auth.{DOMAIN}"
    assert admin_ui["RUNTIME_APP_URL"] == f"https://chat.{DOMAIN}"
    keycloak = services["keycloak"]["environment"]
    assert keycloak["KC_HOSTNAME"] == f"https://auth.{DOMAIN}"
    assert keycloak["KC_PROXY_HEADERS"] == "xforwarded"
    assert keycloak["AURUMAI_CHAT_ORIGIN"] == f"https://chat.{DOMAIN}"
    assert keycloak["AURUMAI_ADMIN_ORIGIN"] == f"https://admin.{DOMAIN}"
    for name, origin in (("backend", "chat"), ("backend-admin", "admin")):
        environment = services[name]["environment"]
        assert environment["APP_KEYCLOAK_ISSUER"] == f"https://auth.{DOMAIN}/realms/aurum-ai"
        assert environment["APP_FRONTEND_URL"] == f"https://{origin}.{DOMAIN}"
    for name in ("langfuse-server", "langfuse-worker"):
        assert services[name]["environment"]["NEXTAUTH_URL"] == f"https://langfuse.{DOMAIN}"


def traefik_routes() -> dict:
    text = TRAEFIK_DYNAMIC.read_text()
    # Traefik renders the file provider as a Go template; emulate only `env`.
    rendered = re.sub(r'\{\{\s*env\s+"PUBLIC_DOMAIN"\s*\}\}', DOMAIN, text)
    assert "{{" not in rendered
    return yaml.safe_load(rendered)["http"]


def test_traefik_routes_each_subdomain_to_its_service_over_tls():
    http = traefik_routes()
    expected = {
        "chat": "http://frontend:3000",
        "admin": "http://admin-ui:3000",
        "api": "http://backend:8000",
        "admin-api": "http://backend-admin:8000",
        "auth": "http://keycloak:8080",
        "temporal": "http://temporal-ui:8080",
        "langfuse": "http://langfuse-server:3000",
    }
    actual = {}
    for router in http["routers"].values():
        host = re.fullmatch(r"Host\(`([a-z-]+)\." + re.escape(DOMAIN) + r"`\)", router["rule"])
        assert host, router["rule"]
        assert router["entryPoints"] == ["websecure"]
        assert router["tls"] == {"certResolver": "letsencrypt"}
        servers = http["services"][router["service"]]["loadBalancer"]["servers"]
        actual[host.group(1)] = [server["url"] for server in servers]
    assert actual == {name: [url] for name, url in expected.items()}


def test_temporal_ui_requires_basic_auth_and_other_routers_do_not_share_it():
    http = traefik_routes()
    by_host = {re.search(r"`([a-z-]+)\.", router["rule"]).group(1): router for router in http["routers"].values()}
    basic_auth = {name for name, middleware in http["middlewares"].items() if "basicAuth" in middleware}
    temporal_auth = basic_auth & set(by_host["temporal"]["middlewares"])
    assert len(temporal_auth) == 1
    assert http["middlewares"][temporal_auth.pop()]["basicAuth"]["usersFile"].startswith("/etc/traefik/auth/")
    assert all(not basic_auth & set(router.get("middlewares", []))
               for host, router in by_host.items() if host != "temporal")


def test_realm_allows_configured_proxy_origins_with_local_defaults():
    realm = json.loads((ROOT / "keycloak/realm-export.json").read_text())
    clients = {client["clientId"]: client for client in realm["clients"]}
    for client, variable, default in (
        ("business-ui", "AURUMAI_CHAT_ORIGIN", "http://localhost:3000"),
        ("admin-ui", "AURUMAI_ADMIN_ORIGIN", "http://localhost:3100"),
    ):
        origin = "${%s:%s}" % (variable, default)
        assert origin + "/*" in clients[client]["redirectUris"]
        assert origin in clients[client]["webOrigins"]
