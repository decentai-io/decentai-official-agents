"""A service's refusal, as every connector passes it on: its kind and its words. Apart from ``http.py`` so that what
reads it (``watch.py``) needs no HTTP library."""

from __future__ import annotations

from typing import Any


class HttpError(Exception):
    def __init__(self, kind: str, message: str, status: int = 0, body: Any = None):
        super().__init__(message)
        self.kind, self.message, self.status, self.body = kind, message, status, body

    def answer(self) -> tuple[dict[str, Any], str]:
        """The error result a function returns: the words, and the kind the assistant reads."""
        return {"error": self.message, "kind": self.kind}, "error"
