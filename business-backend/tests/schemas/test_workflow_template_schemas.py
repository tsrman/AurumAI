from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from src.core.config import settings
from src.schemas.workflow_template import (
    TemplateCaptureRequest,
    TemplateInstantiateRequest,
    TemplateReplaceRequest,
    TemplateSnapshot,
    TemplateVersionRequest,
)


def valid_snapshot() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "supervisorAgentKey": "a0",
        "maxParallelism": 2,
        "agents": [
            {
                "key": "a0",
                "name": "Analyst",
                "description": None,
                "modelId": "provider/model-v1",
                "systemPrompt": "Answer",
                "temperature": 0.3,
                "skillIds": [],
                "knowledgeBaseIds": [],
            }
        ],
        "nodes": [
            {
                "nodeKey": "research",
                "displayName": "Research",
                "agentKey": "a0",
                "instruction": "Find evidence",
                "positionX": 12.5,
                "positionY": 30.0,
                "requiresConfirmation": True,
            }
        ],
        "edges": [],
    }


@pytest.mark.parametrize("version", [None, 2, True, "1"])
def test_snapshot_requires_exact_integer_schema_version(version: object) -> None:
    payload = valid_snapshot()
    if version is None:
        payload.pop("schemaVersion")
    else:
        payload["schemaVersion"] = version

    with pytest.raises(ValidationError):
        TemplateSnapshot.model_validate(payload)


@pytest.mark.parametrize(
    ("location", "field"),
    [
        ("root", "permissions"),
        ("root", "sessionToken"),
        ("agent", "permissions"),
        ("agent", "clientSecret"),
        ("node", "agentDefinitionId"),
    ],
)
def test_snapshot_rejects_permissions_secrets_and_unknown_fields(
    location: str, field: str
) -> None:
    payload = valid_snapshot()
    if location == "root":
        payload[field] = "must-not-be-stored"
    else:
        nested = payload[f"{location}s"]
        assert isinstance(nested, list)
        nested[0][field] = "must-not-be-stored"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TemplateSnapshot.model_validate(payload)


@pytest.mark.parametrize("case", ["missing", "extra", "duplicate"])
def test_snapshot_agent_keys_exactly_match_graph_references(case: str) -> None:
    payload = valid_snapshot()
    agents = payload["agents"]
    assert isinstance(agents, list)
    extra_agent = deepcopy(agents[0])
    extra_agent["key"] = "a1"
    if case == "missing":
        agents.clear()
    elif case == "extra":
        agents.append(extra_agent)
    else:
        agents.append(deepcopy(agents[0]))

    with pytest.raises(ValidationError):
        TemplateSnapshot.model_validate(payload)


def test_snapshot_rejects_invalid_agent_key() -> None:
    payload = valid_snapshot()
    payload["supervisorAgentKey"] = "A 0"

    with pytest.raises(ValidationError):
        TemplateSnapshot.model_validate(payload)


@pytest.mark.parametrize(
    ("location", "field"),
    [("agents", "name"), ("nodes", "displayName"), ("nodes", "instruction")],
)
def test_snapshot_rejects_blank_required_text(location: str, field: str) -> None:
    payload = valid_snapshot()
    collection = payload[location]
    assert isinstance(collection, list)
    collection[0][field] = "   "

    with pytest.raises(ValidationError):
        TemplateSnapshot.model_validate(payload)


@pytest.mark.parametrize("node_key", ["supervisor_prepare", "supervisor_synthesize"])
def test_snapshot_rejects_reserved_node_keys(node_key: str) -> None:
    payload = valid_snapshot()
    nodes = payload["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["nodeKey"] = node_key

    with pytest.raises(ValidationError):
        TemplateSnapshot.model_validate(payload)


def test_snapshot_rejects_cycles() -> None:
    payload = valid_snapshot()
    nodes = payload["nodes"]
    assert isinstance(nodes, list)
    second = deepcopy(nodes[0])
    second["nodeKey"] = "review"
    nodes.append(second)
    payload["edges"] = [
        {"sourceNodeKey": "research", "targetNodeKey": "review"},
        {"sourceNodeKey": "review", "targetNodeKey": "research"},
    ]

    with pytest.raises(ValidationError, match="cycle"):
        TemplateSnapshot.model_validate(payload)


def test_saved_snapshot_remains_valid_after_operator_limits_reduced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "WORKFLOW_MAX_NODES", 1)
    monkeypatch.setattr(settings, "WORKFLOW_MAX_PARALLELISM", 1)

    parallel = valid_snapshot()
    parallel["maxParallelism"] = 2
    assert TemplateSnapshot.model_validate(parallel).max_parallelism == 2

    too_many_nodes = valid_snapshot()
    too_many_nodes["maxParallelism"] = 1
    nodes = too_many_nodes["nodes"]
    assert isinstance(nodes, list)
    second = deepcopy(nodes[0])
    second["nodeKey"] = "review"
    nodes.append(second)
    assert len(TemplateSnapshot.model_validate(too_many_nodes).nodes) == 2


@pytest.mark.parametrize("field,limit", [("maxParallelism", 4), ("nodes", 20), ("agents", 21)])
def test_snapshot_keeps_fixed_format_ceilings_when_operator_limits_increase(
    monkeypatch: pytest.MonkeyPatch, field: str, limit: int,
) -> None:
    monkeypatch.setattr(settings, "WORKFLOW_MAX_NODES", 100)
    monkeypatch.setattr(settings, "WORKFLOW_MAX_PARALLELISM", 100)
    payload = valid_snapshot()
    if field == "maxParallelism":
        payload[field] = limit + 1
    else:
        values = payload[field]
        assert isinstance(values, list)
        payload[field] = [deepcopy(values[0]) for _ in range(limit + 1)]
    with pytest.raises(ValidationError, match="at most|less than or equal"):
        TemplateSnapshot.model_validate(payload)


def test_snapshot_serializes_only_camel_case_configuration(template_snapshot) -> None:
    assert template_snapshot.model_dump(by_alias=True) == {
        "schemaVersion": 1,
        "supervisorAgentKey": "a0",
        "maxParallelism": 2,
        "agents": [
            {
                "key": "a0",
                "name": "Analyst",
                "description": None,
                "modelId": "provider/model-v1",
                "systemPrompt": "Answer",
                "temperature": 0.3,
                "skillIds": [],
                "knowledgeBaseIds": [],
            }
        ],
        "nodes": [
            {
                "nodeKey": "research",
                "displayName": "Research",
                "agentKey": "a0",
                "instruction": "Find evidence",
                "positionX": None,
                "positionY": None,
                "requiresConfirmation": True,
            }
        ],
        "edges": [],
    }


@pytest.mark.parametrize("version", [0, True, "1"])
def test_request_versions_are_strict_positive_integers(version: object) -> None:
    with pytest.raises(ValidationError):
        TemplateVersionRequest.model_validate({"version": version})

    with pytest.raises(ValidationError):
        TemplateCaptureRequest.model_validate(
            {
                "sourceWorkflowId": uuid4(),
                "sourceWorkflowVersion": version,
                "name": "Template",
            }
        )


def test_request_names_are_trimmed_bounded_and_strict() -> None:
    capture = TemplateCaptureRequest.model_validate(
        {
            "sourceWorkflowId": uuid4(),
            "sourceWorkflowVersion": 1,
            "name": "  Template  ",
            "description": "  description  ",
        }
    )
    instantiate = TemplateInstantiateRequest.model_validate(
        {"version": 1, "name": "  " + "x" * 255 + "  "}
    )

    assert capture.name == "Template"
    assert capture.description == "description"
    assert instantiate.name == "x" * 255

    for request in (
        {
            "sourceWorkflowId": uuid4(),
            "sourceWorkflowVersion": 1,
            "name": "   ",
        },
        {"version": 1, "name": "x" * 256},
        {"version": 1, "name": "Copy", "unexpected": True},
    ):
        model = (
            TemplateCaptureRequest
            if "sourceWorkflowId" in request
            else TemplateInstantiateRequest
        )
        with pytest.raises(ValidationError):
            model.model_validate(request)


@pytest.mark.parametrize("name", [None, 7, True, [], {}])
@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            TemplateCaptureRequest,
            {"sourceWorkflowId": uuid4(), "sourceWorkflowVersion": 1},
        ),
        (
            TemplateReplaceRequest,
            {
                "sourceWorkflowId": uuid4(),
                "sourceWorkflowVersion": 1,
                "version": 1,
            },
        ),
        (TemplateInstantiateRequest, {"version": 1}),
    ],
)
def test_request_names_reject_non_strings_with_validation_error(
    model: type[BaseModel], payload: dict[str, object], name: object
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "name": name})
