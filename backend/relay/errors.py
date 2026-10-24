"""API error type shared by every router.

Raise ``ApiError(409, "duplicate_name", "A session called ... exists")``.
The app turns it into ``{"error": code, "message": message}`` with the status.
"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.extra}
