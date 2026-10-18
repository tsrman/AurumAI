"""Boundary validation: an LLM/browser cannot forge identity or ambiguous answers."""

import importlib
import importlib.util

import pytest
from pydantic import ValidationError


@pytest.fixture
def schemas():
    assert (
        importlib.util.find_spec("src.schemas.interaction") is not None
    ), "interaction request schemas are not implemented"
    return importlib.import_module("src.schemas.interaction")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"text": " "},
        {"optionId": ""},
        {"text": "yes", "optionId": "yes"},
        {"text": "x" * 16001},
        {"optionId": ["one", "two"]},
        {"text": "answer", "executionId": "forged"},
        {"text": "answer", "files": []},
    ],
)
def test_answer_rejects_ambiguous_empty_or_forged_payload(schemas, payload):
    with pytest.raises(ValidationError):
        schemas.AnswerRequest.model_validate(payload)


def test_answer_preserves_text_and_serializes_option_camel_case(schemas):
    answer = schemas.AnswerRequest(text="  Berlin\nground floor  ")
    assert answer.text == "  Berlin\nground floor  "
    choice = schemas.AnswerRequest(option_id="north")
    assert choice.model_dump(by_alias=True, exclude_none=True) == {"optionId": "north"}


@pytest.mark.parametrize(
    "payload",
    [
        {"text": " "},
        {"text": "x" * 4001},
        {"text": "City?", "options": [{"id": "a", "label": "A"}] * 2},
        {"text": "City?", "options": [{"id": str(i), "label": "A"} for i in range(9)]},
        {"text": "City?", "options": [{"id": "a", "label": " "}]},
        {"text": "City?", "options": [{"id": "a", "label": "x" * 201}]},
        {"text": "City?", "options": [{"id": "a b", "label": "A"}]},
        {"text": "City?", "options": [{"id": "a", "label": "A", "url": "file:/tmp"}]},
        {"text": "City?", "orgId": "forged"},
        {"text": "City?", "timeout": 999999},
    ],
)
def test_question_rejects_invalid_options_and_runtime_overrides(schemas, payload):
    with pytest.raises(ValidationError):
        schemas.QuestionRequest.model_validate(payload)


def test_question_supports_text_only_and_maximum_valid_options(schemas):
    assert schemas.QuestionRequest(text="City?").options == []
    question = schemas.QuestionRequest.model_validate(
        {
            "text": "x" * 4000,
            "options": [{"id": f"c-{i}", "label": "x" * 200} for i in range(8)],
        }
    )
    assert len(question.options) == 8
    assert question.options[7].id == "c-7"


@pytest.mark.parametrize(
    "payload",
    [
        {"questionTimeoutSeconds": 0},
        {"questionTotal": -1},
        {"questionOpen": 0},
        {"webSearchTotal": -1},
        {"webReadTotal": -1},
        {"questionTotal": True},
        {"questionTimeoutSeconds": 1.5},
        {"permissions": ["chat.send"]},
    ],
)
def test_turn_limits_reject_invalid_or_authorization_snapshot_fields(schemas, payload):
    with pytest.raises(ValidationError):
        schemas.TurnLimits.model_validate(payload)
