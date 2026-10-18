from src.workflows.conversation import ConversationWorkflow


def test_append_message_once_dedupes_loaded_user_signal() -> None:
    workflow = ConversationWorkflow()
    workflow.messages = [{"role": "user", "content": "hello"}]

    workflow._append_message_once({"role": "user", "content": "hello", "model": "gpt-4o-mini"})

    assert workflow.messages == [{"role": "user", "content": "hello"}]


def test_append_message_once_appends_new_message() -> None:
    workflow = ConversationWorkflow()
    workflow.messages = [{"role": "assistant", "content": "hi"}]

    workflow._append_message_once({"role": "user", "content": "hello", "model": "gpt-4o-mini"})

    assert workflow.messages == [
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "hello", "turn_id": None},
    ]


def test_append_message_once_appends_same_content_with_different_turn_ids() -> None:
    workflow = ConversationWorkflow()
    workflow.messages = [{"role": "user", "content": "hello", "turn_id": "turn-1"}]

    workflow._append_message_once(
        {"role": "user", "content": "hello", "turn_id": "turn-2", "model": "gpt-4o-mini"}
    )

    assert workflow.messages == [
        {"role": "user", "content": "hello", "turn_id": "turn-1"},
        {"role": "user", "content": "hello", "turn_id": "turn-2"},
    ]


def test_append_message_once_dedupes_same_content_and_turn_id() -> None:
    workflow = ConversationWorkflow()
    workflow.messages = [{"role": "user", "content": "hello", "turn_id": "turn-1"}]

    workflow._append_message_once(
        {"role": "user", "content": "hello", "turn_id": "turn-1", "model": "gpt-4o-mini"}
    )

    assert workflow.messages == [{"role": "user", "content": "hello", "turn_id": "turn-1"}]


def test_build_skills_system_message_shows_input_schema_for_normal_skills() -> None:
    from src.workflows.conversation import _build_skills_system_message

    skills = [
        {
            "id": "skill-1",
            "name": "get-sum",
            "type": "mcp",
            "description": "Sums two numbers",
            "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}}},
        }
    ]

    message = _build_skills_system_message(skills)

    assert "id=skill-1 name=get-sum: Sums two numbers" in message
    assert '"a": {"type": "number"}' in message


def test_build_skills_system_message_shows_task_hint_for_code_skills() -> None:
    from src.workflows.conversation import _build_skills_system_message

    skills = [
        {
            "id": "skill-2",
            "name": "greet",
            "type": "code",
            "description": "Greets the user",
            "inputSchema": {},
        }
    ]

    message = _build_skills_system_message(skills)

    assert "id=skill-2 name=greet: Greets the user" in message
    assert '{"task": "<describe what you want the skill to accomplish>"}' in message


def test_feature_off_removes_question_tool_from_both_runtime_prompts() -> None:
    from src.workflows.agent import AgentExecutionSnapshot, _build_agent_system_prompt
    from src.workflows.conversation import _build_skills_system_message

    agent = AgentExecutionSnapshot(
        agent_definition_id="00000000-0000-4000-8000-000000000001",
        name="Agent",
        model="provider/model",
        system_prompt="Help.",
        temperature=0.2,
    )

    assert "ask_user" not in _build_skills_system_message(
        [], user_questions_enabled=False
    )
    assert "ask_user" not in _build_agent_system_prompt(
        agent, [], user_questions_enabled=False
    )
