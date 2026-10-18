from typing import Any


class PlatformException(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 500,
        details: dict[str, Any] | None = None,
    ):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(message)


class NotFoundException(PlatformException):
    def __init__(self, resource: str, resource_id: str):
        super().__init__("NOT_FOUND", f"{resource} {resource_id} not found", 404)


class ConflictException(PlatformException):
    def __init__(self, message: str):
        super().__init__("CONFLICT", message, 409)


class ForbiddenException(PlatformException):
    def __init__(self, message: str = "Forbidden"):
        super().__init__("FORBIDDEN", message, 403)
