from src.workflows.tools import extract_run_commands, extract_tool_calls


def test_extract_single_tool_call():
    text = 'some text <tool_call>{"skill_id": "web_search", "args": {"q": "hi"}}</tool_call> more'
    calls = extract_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["skill_id"] == "web_search"
    assert calls[0]["args"] == {"q": "hi"}
    assert "call_id" in calls[0]


def test_extract_multiple_tool_calls():
    text = (
        '<tool_call>{"skill_id": "a", "args": {}}</tool_call>'
        '<tool_call>{"skill_id": "b", "args": {"x": 1}}</tool_call>'
    )
    calls = extract_tool_calls(text)
    assert len(calls) == 2
    assert calls[0]["skill_id"] == "a"
    assert calls[1]["skill_id"] == "b"


def test_extract_list_payload():
    text = '<tool_call>[{"skill_id": "x", "args": {}}, {"skill_id": "y", "args": {}}]</tool_call>'
    calls = extract_tool_calls(text)
    assert len(calls) == 2


def test_extract_no_tool_calls_returns_empty():
    assert extract_tool_calls("Hello, world!") == []


def test_extract_malformed_json_is_skipped():
    text = '<tool_call>not-json</tool_call><tool_call>{"skill_id": "ok", "args": {}}</tool_call>'
    calls = extract_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["skill_id"] == "ok"
    assert calls[0]["args"] == {}
    assert "call_id" in calls[0]


def test_extract_unclosed_tag_is_skipped():
    text = '<tool_call>{"skill_id": "dangling"'
    assert extract_tool_calls(text) == []


def test_extract_tool_calls_includes_call_id():
    text = '<tool_call>{"skill_id": "web_search", "args": {"q": "hi"}}</tool_call>'
    calls = extract_tool_calls(text)
    assert len(calls) == 1
    assert "call_id" in calls[0]
    assert isinstance(calls[0]["call_id"], str)
    assert len(calls[0]["call_id"]) == 36


def test_extract_single_run_command():
    text = 'Let me check.<run_command>{"command": "ls -la"}</run_command>'
    calls = extract_run_commands(text)
    assert len(calls) == 1
    assert calls[0]["command"] == "ls -la"


def test_extract_multiple_run_commands():
    text = (
        '<run_command>{"command": "echo a"}</run_command>'
        '<run_command>{"command": "echo b"}</run_command>'
    )
    calls = extract_run_commands(text)
    assert [c["command"] for c in calls] == ["echo a", "echo b"]


def test_extract_run_commands_list_payload():
    text = '<run_command>[{"command": "a"}, {"command": "b"}]</run_command>'
    calls = extract_run_commands(text)
    assert [c["command"] for c in calls] == ["a", "b"]


def test_extract_no_run_commands_returns_empty():
    assert extract_run_commands("The task is complete.") == []


def test_extract_run_commands_malformed_json_is_skipped():
    text = '<run_command>not-json</run_command><run_command>{"command": "ok"}</run_command>'
    calls = extract_run_commands(text)
    assert len(calls) == 1
    assert calls[0]["command"] == "ok"


def test_extract_run_commands_missing_command_key_is_skipped():
    text = '<run_command>{"not_command": "x"}</run_command>'
    assert extract_run_commands(text) == []


def test_extract_run_commands_ignores_tool_call_tags():
    text = '<tool_call>{"skill_id": "x", "args": {}}</tool_call>'
    assert extract_run_commands(text) == []
