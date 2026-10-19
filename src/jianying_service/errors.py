from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """A public, structured error with no implementation details in its message."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


class ValidationFailure(ServiceError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(code, message, status=422, details=details)
