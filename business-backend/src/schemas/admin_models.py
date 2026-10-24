from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from src.core.authorization_types import ResourceRef, ResourceType
from src.services.model_catalog import GatewayCatalogStatus


class AdminModelSchema(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class AdminModelEntryResponse(AdminModelSchema):
    id: str
    organization_readable: bool
    is_default: bool


class AdminModelCatalogResponse(AdminModelSchema):
    gateway_status: GatewayCatalogStatus
    default_model: str | None
    default_available: bool
    default_temperature: float | None
    models: list[AdminModelEntryResponse]


class OrganizationModelSettingsUpdate(AdminModelSchema):
    default_model: str | None = Field(max_length=255)
    default_temperature: float | None = Field(ge=0, le=2)

    @field_validator("default_model")
    @classmethod
    def canonical_model_id(cls, value: str | None) -> str | None:
        if value is not None:
            ResourceRef(ResourceType.MODEL, value)
        return value


class OrganizationModelSettingResponse(AdminModelSchema):
    default_model: str | None
    default_temperature: float | None


class OrganizationImageModelsResponse(AdminModelSchema):
    # The operator switch (APP_IMAGES_ENABLED); models only take effect with it on.
    enabled: bool
    gateway_status: GatewayCatalogStatus
    generation_model: str | None
    edit_model: str | None
    models: list[str]


class OrganizationImageModelsUpdate(AdminModelSchema):
    generation_model: str | None = Field(max_length=255)
    edit_model: str | None = Field(max_length=255)

    @field_validator("generation_model", "edit_model")
    @classmethod
    def canonical_model_id(cls, value: str | None) -> str | None:
        if value is not None:
            ResourceRef(ResourceType.MODEL, value)
        return value
