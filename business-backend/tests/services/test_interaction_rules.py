from datetime import UTC, datetime, timedelta

import pytest


@pytest.mark.parametrize(
    "status,active,offset,expected",
    [
        ("pending", True, -1, True),
        ("pending", True, 0, False),
        ("pending", True, 1, False),
        ("pending", False, -1, False),
        ("answered", True, -1, False),
        ("expired", True, -1, False),
        ("cancelled", True, -1, False),
    ],
)
def test_answer_boundary(status, active, offset, expected):
    from src.services.interaction_rules import answer_allowed

    deadline = datetime(2026, 9, 6, 12, tzinfo=UTC)
    assert (
        answer_allowed(status, active, deadline + timedelta(microseconds=offset), deadline)
        is expected
    )


def test_request_fingerprint_changes_with_answer_choices():
    from src.schemas.interaction import QuestionRequest
    from src.services.interaction_rules import question_arguments_hash

    one = QuestionRequest(text="Where?", options=[{"id": "a", "label": "North"}])
    two = QuestionRequest(text="Where?", options=[{"id": "a", "label": "South"}])
    assert question_arguments_hash(one) != question_arguments_hash(two)
    assert question_arguments_hash(one) == question_arguments_hash(
        QuestionRequest.model_validate(one.model_dump(by_alias=True))
    )
