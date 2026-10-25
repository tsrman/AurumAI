from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class Capability(StrEnum):
    ADMIN_USERS_READ = "admin.users.read"
    ADMIN_USERS_WRITE = "admin.users.write"
    AGENTS_HERMES = "agents.hermes"
    CHAT_SEND = "chat.send"
    IMAGES_GENERATE = "images.generate"
    SHARING_MANAGE = "sharing.manage"
    WEB_ACCESS = "web.access"
    WORKSPACE_AGENTS_CREATE = "workspace.agents.create"
    WORKSPACE_KNOWLEDGE_CREATE = "workspace.knowledge.create"
    WORKSPACE_SKILLS_CREATE = "workspace.skills.create"


CAPABILITY_VALUES_SQL = (
    "'admin.users.read', 'admin.users.write', 'agents.hermes', 'chat.send', 'images.generate', "
    "'sharing.manage', 'web.access', 'workspace.agents.create', "
    "'workspace.knowledge.create', 'workspace.skills.create'"
)


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    key: Capability
    label: str
    description: str

    def __post_init__(self) -> None:
        if not self.label.strip() or not self.description.strip():
            raise ValueError(f"Capability {self.key.value} requires UI metadata")


class CapabilityCatalog:
    def __init__(self, definitions: Iterable[CapabilityDefinition]) -> None:
        by_key: dict[Capability, CapabilityDefinition] = {}
        for definition in definitions:
            if definition.key in by_key:
                raise ValueError(f"Duplicate capability: {definition.key.value}")
            by_key[definition.key] = definition
        self._definitions = tuple(sorted(by_key.values(), key=lambda item: item.key.value))

    def keys(self) -> tuple[str, ...]:
        return tuple(definition.key.value for definition in self._definitions)

    def definitions(self) -> tuple[CapabilityDefinition, ...]:
        return self._definitions

    def parse(self, value: str) -> Capability:
        try:
            capability = Capability(value)
        except ValueError as exc:
            raise ValueError(f"Unknown capability: {value}") from exc
        if capability not in {definition.key for definition in self._definitions}:
            raise ValueError(f"Unknown capability: {value}")
        return capability


CAPABILITY_CATALOG = CapabilityCatalog(
    (
        CapabilityDefinition(
            Capability.ADMIN_USERS_READ,
            "View users",
            "View users and their effective access inside the organization.",
        ),
        CapabilityDefinition(
            Capability.ADMIN_USERS_WRITE,
            "Manage users",
            "Manage organization user settings outside Keycloak group membership.",
        ),
        CapabilityDefinition(
            Capability.AGENTS_HERMES,
            "Use Hermes Agent",
            "Run the Hermes Agent system agent in its isolated sandbox.",
        ),
        CapabilityDefinition(
            Capability.CHAT_SEND,
            "Send chat messages",
            "Start and continue AI conversations.",
        ),
        CapabilityDefinition(
            Capability.IMAGES_GENERATE,
            "Generate images",
            "Create and edit images in chat with the organization's image models.",
        ),
        CapabilityDefinition(
            Capability.SHARING_MANAGE,
            "Manage sharing",
            "Manage resource grants; v1 additionally requires the org_admin role.",
        ),
        CapabilityDefinition(
            Capability.WEB_ACCESS,
            "Access the web",
            "Search and read public web pages through the platform's isolated web service; "
            "where the organization allows it, also lets Hermes Agent sandboxes reach the "
            "public internet.",
        ),
        CapabilityDefinition(
            Capability.WORKSPACE_AGENTS_CREATE,
            "Create agents",
            "Create agent definitions in the organization workspace.",
        ),
        CapabilityDefinition(
            Capability.WORKSPACE_KNOWLEDGE_CREATE,
            "Create knowledge bases",
            "Create knowledge bases and upload inherited documents.",
        ),
        CapabilityDefinition(
            Capability.WORKSPACE_SKILLS_CREATE,
            "Create skills",
            "Create skills in the organization workspace.",
        ),
    )
)
