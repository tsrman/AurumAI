from inspect import signature

from src.services.session_token import session_token_service


def test_issue_decode_round_trip() -> None:
    token = session_token_service.issue("conv-1", "user-1", "org-1")

    decoded = session_token_service.decode(token)

    assert decoded["sub"] == "user-1"
    assert decoded["conversation_id"] == "conv-1"
    assert decoded["org_id"] == "org-1"
    assert decoded["type"] == "session"
    assert "exp" in decoded


def test_issue_with_none_org_id() -> None:
    token = session_token_service.issue("conv-2", "user-2", None)

    decoded = session_token_service.decode(token)

    assert decoded["org_id"] is None


def test_issue_is_identity_only() -> None:
    token = session_token_service.issue("conv-3", "user-3", "org-1")

    decoded = session_token_service.decode(token)

    assert set(decoded) == {"sub", "conversation_id", "org_id", "exp", "type", "generation"}


def test_issue_api_does_not_accept_an_authorization_snapshot() -> None:
    assert "permissions" not in signature(session_token_service.issue).parameters


def test_session_token_preserves_lifecycle_generation():
    token = session_token_service.issue('conv', 'user', 'org', generation=7)
    assert session_token_service.decode(token)['generation'] == 7
