"""Contextual retrieval is on only where a gateway it can call is configured.

Without one the worker's call fails for every chunk; with the setting on, every document would
then be reported as indexed without context. The base stack has no such gateway, the gateway
overlay does.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SETTING = "RAG_WORKER_CONTEXTUAL_RETRIEVAL_ENABLED"


def _worker_environment(name: str) -> dict[str, str]:
    compose = yaml.safe_load((ROOT / name).read_text())
    return compose["services"]["rag-worker"]["environment"]


def test_the_base_stack_does_not_expect_contextual_retrieval() -> None:
    assert _worker_environment("docker-compose.yml")[SETTING] == "${RAG_CONTEXTUAL_RETRIEVAL_ENABLED:-false}"


def test_the_gateway_overlay_turns_contextual_retrieval_on() -> None:
    assert (
        _worker_environment("docker-compose.llm-gateway.yml")[SETTING]
        == "${RAG_CONTEXTUAL_RETRIEVAL_ENABLED:-true}"
    )


def test_the_contextual_model_can_be_chosen_without_editing_compose() -> None:
    environment = _worker_environment("docker-compose.yml")
    assert environment["RAG_WORKER_CONTEXTUAL_MODEL"] == "${RAG_CONTEXTUAL_MODEL:-gpt-4o-mini}"
