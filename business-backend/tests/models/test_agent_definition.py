import uuid

import pytest
from pydantic import ValidationError

from src.models.agent_definition import AgentDefinition
from src.schemas.agent_definition import AgentDefinitionCreate, AgentDefinitionUpdate


@pytest.mark.parametrize(
    "fields,expected", [({}, 0.3), ({"temperature": None}, None),
                        ({"temperature": 0.0}, 0.0), ({"temperature": 1.2}, 1.2)]
)
async def test_created_agent_persists_temperature_intent(db, test_org, test_user, fields, expected):
    from src.core.authorization_types import AuthorizationSubject
    from src.repositories.agent_definition_repository import AgentDefinitionRepository

    agent = await AgentDefinitionRepository().create_with_owner_write_grant(
        db,
        AuthorizationSubject(test_user.id, test_org.id, "org_admin"),
        AgentDefinitionCreate(name="Temperature", modelId="test", systemPrompt="Test", **fields),
    )
    await db.refresh(agent)
    assert agent.temperature == expected


def test_agent_definition_has_stable_typed_dependency_columns() -> None:
    table = AgentDefinition.__table__

    assert table.c.id.primary_key is True
    assert table.c.org_id.nullable is False
    assert table.c.model_id.type.length == 255
    assert table.c.skill_ids.nullable is False
    assert table.c.knowledge_base_ids.nullable is False
    assert {constraint.name for constraint in table.constraints} >= {
        "ck_agent_definitions_name_nonblank",
        "ck_agent_definitions_model_nonblank",
        "ck_agent_definitions_temperature",
        "uq_agent_definitions_org_name",
    }


def test_agent_definition_create_normalizes_and_deduplicates_ids() -> None:
    skill_id = uuid.uuid4()
    base_id = uuid.uuid4()
    payload = AgentDefinitionCreate(
        name="  Research agent  ",
        description="  Finds evidence  ",
        modelId="  provider/model-v1  ",
        systemPrompt="  Be precise.  ",
        temperature=0.4,
        skillIds=[skill_id],
        knowledgeBaseIds=[base_id],
    )

    assert payload.name == "Research agent"
    assert payload.description == "Finds evidence"
    assert payload.model_id == "provider/model-v1"
    assert payload.system_prompt == "Be precise."

    with pytest.raises(ValidationError):
        AgentDefinitionCreate(
            name="Agent",
            modelId="model",
            systemPrompt="Prompt",
            skillIds=[skill_id, skill_id],
        )

    with pytest.raises(ValidationError):
        AgentDefinitionCreate(
            name="Agent",
            modelId="model",
            systemPrompt="Prompt",
            temperature=2.1,
        )


def test_agent_definition_update_rejects_null_for_non_nullable_fields() -> None:
    for field, value in (
        ("name", None),
        ("modelId", None),
        ("systemPrompt", None),
        ("skillIds", None),
        ("knowledgeBaseIds", None),
    ):
        with pytest.raises(ValidationError):
            AgentDefinitionUpdate.model_validate({field: value})

    assert AgentDefinitionUpdate(description=None).description is None


def test_agent_definition_update_accepts_null_temperature_to_inherit_org_default() -> None:
    # None is meaningful here (distinct from the field being omitted): it
    # clears an agent's own override so it inherits the organization default.
    assert AgentDefinitionUpdate.model_validate({"temperature": None}).temperature is None
