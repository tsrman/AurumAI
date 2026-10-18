from pathlib import Path
from typing import Any

import yaml


def _compose() -> dict[str, Any]:
    path = Path(__file__).parents[2] / "docker-compose.yml"
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _networks(service: dict[str, Any]) -> set[str]:
    configured = service.get("networks", [])
    if isinstance(configured, dict):
        return set(configured)
    assert isinstance(configured, list)
    return set(configured)


def test_unauthenticated_rag_api_is_not_reachable_from_default_network() -> None:
    compose = _compose()
    services = compose["services"]

    assert _networks(services["rag-api"]) == {"rag-control-net", "rag-net"}
    assert "rag-control-net" in _networks(services["backend"])
    assert "rag-control-net" in _networks(services["postgres"])
    assert "rag-control-net" in _networks(services["temporal"])
    assert "rag-control-net" not in _networks(services["tool-worker"])
    assert compose["networks"]["rag-control-net"]["internal"] is True


def test_tool_worker_has_no_direct_rag_api_endpoint() -> None:
    environment = _compose()["services"]["tool-worker"]["environment"]
    assert "TOOL_WORKER_RAG_API_URL" not in environment

    root = Path(__file__).parents[2]
    config_source = (root / "tool-worker/src/config.py").read_text()
    helm_template = (
        root / "charts/aurum-ai/templates/tool-worker-deployment.yaml"
    ).read_text()
    assert "rag_api_url" not in config_source
    assert "TOOL_WORKER_RAG_API_URL" not in helm_template
