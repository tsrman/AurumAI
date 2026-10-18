from typing import Any, Optional
from pydantic import BaseModel


class UserSettingsUpdate(BaseModel):
    theme: Optional[str] = None
    language: Optional[str] = None
    default_model: Optional[str] = None
    code_theme: Optional[str] = None
    enter_to_send: Optional[bool] = None
    show_timestamps: Optional[bool] = None
    sidebar_collapsed: Optional[bool] = None
    notifications: Optional[dict[str, Any]] = None
    shortcuts: Optional[dict[str, Any]] = None


class UserSettingsResponse(BaseModel):
    user_id: str
    theme: str
    language: str
    default_model: Optional[str]
    organization_default_model: Optional[str]
    code_theme: str
    enter_to_send: bool
    show_timestamps: bool
    sidebar_collapsed: bool
    notifications: dict[str, Any]
    shortcuts: dict[str, Any]
