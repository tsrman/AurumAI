import pytest

from src.core.capability_catalog import (
    CAPABILITY_CATALOG,
    CAPABILITY_VALUES_SQL,
    Capability,
    CapabilityCatalog,
    CapabilityDefinition,
)


def test_default_catalog_exposes_the_approved_capabilities_in_stable_order() -> None:
    assert CAPABILITY_CATALOG.keys() == (
        "admin.users.read",
        "admin.users.write",
        "chat.send",
        "images.generate",
        "sharing.manage",
        "web.access",
        "workspace.agents.create",
        "workspace.knowledge.create",
        "workspace.skills.create",
    )
    assert CAPABILITY_CATALOG.parse("chat.send") is Capability.CHAT_SEND


def test_catalog_rejects_duplicate_keys_instead_of_silently_overwriting_metadata() -> None:
    duplicate = CapabilityDefinition(Capability.CHAT_SEND, "Duplicate", "Must fail")

    with pytest.raises(ValueError, match="Duplicate capability"):
        CapabilityCatalog(
            (
                CapabilityDefinition(Capability.CHAT_SEND, "Chat", "Send chat messages"),
                duplicate,
            )
        )


def test_catalog_rejects_unknown_capability_at_the_api_boundary() -> None:
    with pytest.raises(ValueError, match="Unknown capability"):
        CAPABILITY_CATALOG.parse("chat.delete_everything")


def test_catalog_definitions_have_nonempty_ui_metadata() -> None:
    for definition in CAPABILITY_CATALOG.definitions():
        assert definition.label.strip()
        assert definition.description.strip()


def test_catalog_describes_every_capability_the_enum_declares() -> None:
    """The catalog is the API-facing source both UIs read. A Capability with
    no CapabilityDefinition is invisible to admins yet still grantable by an
    org_admin bypass (effective_capabilities returns set(Capability)), so the
    two must not drift."""
    assert set(CAPABILITY_CATALOG.keys()) == {capability.value for capability in Capability}


def test_sql_check_constraint_covers_every_capability_the_enum_declares() -> None:
    """CAPABILITY_VALUES_SQL feeds the CHECK constraint on both capability
    tables (models/*_capability.py) and is mirrored verbatim by the Alembic
    revision that created them. A Capability missing from it passes every
    Python-level check and then fails at INSERT time in production."""
    declared = {value.strip().strip("'") for value in CAPABILITY_VALUES_SQL.split(",")}

    assert declared == {capability.value for capability in Capability}
