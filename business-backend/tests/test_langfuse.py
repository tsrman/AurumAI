import hashlib

from src.core import langfuse_client
from src.core.config import settings


def test_sanitize_content_keeps_text_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "LANGFUSE_CAPTURE_FULL_CONTENT", True)
    assert langfuse_client.sanitize_content("hello") == "hello"


def test_sanitize_content_redacts_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "LANGFUSE_CAPTURE_FULL_CONTENT", False)
    text = "secret"
    expected_sha = hashlib.sha256(text.encode()).hexdigest()[:16]
    result = langfuse_client.sanitize_content(text)
    assert result.startswith("<redacted")
    assert expected_sha in result


def test_sanitize_messages(monkeypatch):
    monkeypatch.setattr(settings, "LANGFUSE_CAPTURE_FULL_CONTENT", False)
    messages = [{"role": "user", "content": "hello"}]
    result = langfuse_client.sanitize_messages(messages)
    assert result[0]["role"] == "user"
    assert result[0]["content"].startswith("<redacted")
