"""The maintenance network namespace only permits configured peers."""

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def module():
    spec = importlib.util.spec_from_file_location(
        "maintenance_guard", ROOT / "infra/maintenance-egress/guard.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_exact_peer_rules_cover_both_families_without_general_tcp_egress():
    rules = module().rules_for(["127.0.0.11"], [("192.0.2.1", 5432), ("2001:db8::2", 7233)])
    assert "policy drop" in rules
    assert "192.0.2.1 . 5432" in rules
    assert "2001:db8::2 . 7233" in rules
    assert "ip daddr . tcp dport @peers4 accept" in rules
    assert "ip6 daddr . tcp dport @peers6 accept" in rules
    assert "tcp dport { 80, 443 } accept" not in rules
    assert "elements = {  }" not in rules


@pytest.mark.parametrize("value", ["ftp://example.com", "http://", "https://example.com:0"])
def test_invalid_gateway_configuration_is_rejected(value):
    with pytest.raises(ValueError):
        module().gateway_peer(value)


def test_compose_worker_cannot_bypass_guard():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    worker = services["identity-worker"]
    assert worker["network_mode"] == "service:identity-egress"
    assert worker["cap_drop"] == ["ALL"]
    assert worker["depends_on"]["identity-egress"]["condition"] == "service_healthy"
    guard = services["identity-egress"]
    assert guard["environment"]["LLM_GATEWAY_URL"] == worker["environment"]["APP_LLM_GATEWAY_URL"]
    assert guard["cap_drop"] == ["ALL"]
    assert guard["cap_add"] == ["NET_ADMIN"]
    assert not guard.get("ports") and not guard.get("volumes")


def test_data_endpoints_do_not_implicitly_allow_identity_gateway():
    targets = module().configured_targets({
        "MAINTENANCE_TCP_ENDPOINTS": '[["postgres",5432],["temporal",7233]]',
        "MAINTENANCE_HTTP_ENDPOINTS": '["http://archive-minio:9000","http://rag-archive:8111"]',
        "LLM_GATEWAY_URL": "https://unused-provider.example",
    })
    assert targets == [("postgres", 5432), ("temporal", 7233), ("archive-minio", 9000), ("rag-archive", 8111)]


def test_identity_default_targets_remain_exact():
    assert module().configured_targets({"LLM_GATEWAY_URL": "https://provider.example:8443"}) == [
        ("postgres", 5432), ("temporal", 7233), ("provider.example", 8443),
    ]


@pytest.mark.parametrize("raw", ['[]', '{}', '[["postgres",0]]', '[["",5432]]', '[["postgres",true]]', '[["postgres","5432"]]', '[["postgres",5432,1]]'])
def test_invalid_explicit_targets_fail_closed_without_fallback(raw):
    with pytest.raises(ValueError):
        module().configured_targets({"MAINTENANCE_TCP_ENDPOINTS": raw, "LLM_GATEWAY_URL": "https://provider.example"})
