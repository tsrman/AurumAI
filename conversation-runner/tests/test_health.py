from fastapi.testclient import TestClient

from src.config import settings
from src.health import get_bind_event, health_app


def test_health_endpoint_ok():
    client = TestClient(health_app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_pool_status_reflects_binding(monkeypatch):
    monkeypatch.setattr(settings, "conversation_id", None)
    client = TestClient(health_app)
    assert client.get("/pool-status").json() == {"bound": False}

    monkeypatch.setattr(settings, "conversation_id", "conv-1")
    assert client.get("/pool-status").json() == {"bound": True}


def test_bind_sets_settings_and_event(monkeypatch):
    monkeypatch.setattr(settings, "conversation_id", None)
    monkeypatch.setattr(settings, "user_id", None)
    monkeypatch.setattr(settings, "org_id", None)
    monkeypatch.setattr(settings, "session_token", None)
    monkeypatch.setattr(settings, "task_queue", None)
    # Force a fresh event bound to this test's event loop.
    monkeypatch.setattr("src.health._bind_event", None)

    client = TestClient(health_app)
    response = client.post(
        "/bind",
        json={
            "conversation_id": "conv-42",
            "user_id": "user-1",
            "org_id": "org-1",
            "session_token": "tok-abc",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "bound"}
    assert settings.conversation_id == "conv-42"
    assert settings.user_id == "user-1"
    assert settings.org_id == "org-1"
    assert settings.session_token == "tok-abc"
    assert settings.task_queue == "session-conv-42"
    assert get_bind_event().is_set()


def test_bind_rejects_already_bound_container(monkeypatch):
    monkeypatch.setattr(settings, "conversation_id", "already-bound")

    client = TestClient(health_app)
    response = client.post(
        "/bind",
        json={
            "conversation_id": "conv-99",
            "user_id": "user-1",
            "session_token": "tok",
        },
    )

    assert response.status_code == 409


def test_bind_is_idempotent_for_same_identity_and_refreshes_token(monkeypatch):
    monkeypatch.setattr(settings, "conversation_id", "conv-42")
    monkeypatch.setattr(settings, "user_id", "user-1")
    monkeypatch.setattr(settings, "org_id", "org-1")
    monkeypatch.setattr(settings, "session_token", "old-token")
    monkeypatch.setattr(settings, "task_queue", "session-conv-42")

    client = TestClient(health_app)
    response = client.post(
        "/bind",
        json={
            "conversation_id": "conv-42",
            "user_id": "user-1",
            "org_id": "org-1",
            "session_token": "new-token",
            "task_queue": "session-conv-42",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "bound"}
    assert settings.session_token == "new-token"
