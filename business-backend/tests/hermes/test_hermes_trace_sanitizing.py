"""Traces of Hermes requests never carry image bytes, and redact as configured."""

from src.core import langfuse_client
from src.core.langfuse_client import sanitize_native_messages
from src.services.completion_stream import CompletionOutput

IMAGE_URL = "data:image/png;base64," + "A" * 400


def _messages() -> list[dict[str, object]]:
    return [
        {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": IMAGE_URL}}]},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": '{"secret":1}'}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "tool output"},
    ]


def test_full_capture_keeps_text_but_never_image_bytes(monkeypatch) -> None:
    monkeypatch.setattr(langfuse_client.settings, "LANGFUSE_CAPTURE_FULL_CONTENT", True)

    traced = sanitize_native_messages(_messages())

    assert "AAAA" not in str(traced)
    assert traced[0]["content"][0] == {"type": "text", "text": "look"}
    assert traced[0]["content"][1]["image_url"].startswith("<image image/png len=")
    assert traced[1]["tool_calls"] == [{"id": "c1", "name": "f", "arguments": '{"secret":1}'}]
    assert traced[2] == {"role": "tool", "content": "tool output", "tool_call_id": "c1"}


def test_redacted_mode_hashes_text_tool_arguments_and_results(monkeypatch) -> None:
    monkeypatch.setattr(langfuse_client.settings, "LANGFUSE_CAPTURE_FULL_CONTENT", False)

    traced = str(sanitize_native_messages(_messages()))

    for secret in ("look", "secret", "tool output", "AAAA"):
        assert secret not in traced


def test_streamed_tool_calls_are_accumulated_for_the_trace(monkeypatch) -> None:
    monkeypatch.setattr(langfuse_client.settings, "LANGFUSE_CAPTURE_FULL_CONTENT", True)
    output = CompletionOutput()
    for chunk in (
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"web_","arguments":"{\\"q"}}]}}]}',
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"search","arguments":"\\":1}"}}]}}]}',
        '{"choices":[{"delta":{"content":"done"}}]}',
        "not json",
    ):
        output.add(chunk)

    assert output.traced() == {
        "completion": "done",
        "tool_calls": [{"name": "web_search", "arguments": '{"q":1}'}],
    }
