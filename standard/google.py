"""Google's APIs, for the Google agents: the HTTP of ``http.py`` with Google's own ways.

- Google says "too many requests" with 429, and also with **403** and a reason of ``rateLimitExceeded``,
  ``userRateLimitExceeded`` or ``quotaExceeded`` per minute: both are waited on and tried again.
- A 403 for a **missing permission** (``insufficientPermissions``, ``ACCESS_TOKEN_SCOPE_INSUFFICIENT``) says to
  reconnect the account.
- **Pages** are a ``pageToken`` passed back, not a link: ``token_pages``.
- **Shared drives:** every Drive request that names files says ``supportsAllDrives``; a listing also says
  ``includeItemsFromAllDrives`` (``ALL_DRIVES``).
"""

from __future__ import annotations

from typing import Any

import requests

from .accounts import account
from .http import Http, HttpError, segment

GOOGLE = "https://www.googleapis.com"
ALL_DRIVES = {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
BUSY_REASONS = {"ratelimitexceeded", "userratelimitexceeded", "backenderror", "quotaexceeded",
                "dailylimitexceeded"}


class Google(Http):
    SERVICE = "Google"
    BASE = GOOGLE

    def __init__(self, secret: dict[str, Any], base: str = ""):
        super().__init__(str(secret.get("access_token") or ""), base=base, api_base_url=secret.get("api_base_url"))
        self.account = str(secret.get("account") or "")

    @staticmethod
    def reasons(response: requests.Response) -> set[str]:
        try:
            error = (response.json() or {}).get("error") or {}
        except (ValueError, AttributeError):
            return set()
        if not isinstance(error, dict):
            return set()
        found = {str(e.get("reason") or "").lower() for e in error.get("errors") or [] if isinstance(e, dict)}
        found |= {str(d.get("reason") or "").lower() for d in error.get("details") or [] if isinstance(d, dict)}
        found.add(str(error.get("status") or "").lower())
        return found - {""}

    def busy(self, response: requests.Response) -> bool:
        if response.status_code in (429, 503):
            return True
        if response.status_code == 403:
            reasons = self.reasons(response)
            # A daily quota is not waited out within a call.
            return bool(reasons & BUSY_REASONS) and "dailylimitexceeded" not in reasons
        return False

    def scope_refused(self, response: requests.Response, words: str) -> bool:
        reasons = self.reasons(response)
        return response.status_code == 403 and bool(
            reasons & {"insufficientpermissions", "access_token_scope_insufficient", "permission_denied"}
            or "insufficient authentication scopes" in words.lower())

    async def token_pages(self, path: str, *, params: dict[str, Any] | None = None, items: str = "items",
                          most: int = 0, token_param: str = "pageToken", next_key: str = "nextPageToken",
                          **kw: Any) -> list[Any]:
        """Every page's ``items`` until there is no next page token, or ``most`` are held."""
        found: list[Any] = []
        params = dict(params or {})
        while True:
            answer = await self.get(path, params=params, **kw)
            found.extend(answer.get(items) or [])
            token = str(answer.get(next_key) or "")
            if not token or (most and len(found) >= most):
                return found[:most] if most else found
            params[token_param] = token


async def google_for(call: Any, wanted: Any = None, ask: bool = True,
                     base: str = "") -> tuple[Google | None, str]:
    """The Google client over the connected account wanted, or None and why."""
    secret, why = await account(call, "google", wanted, ask, "Google")
    return (Google(secret, base), "") if secret is not None else (None, why)


__all__ = ["ALL_DRIVES", "GOOGLE", "Google", "HttpError", "google_for", "segment"]
