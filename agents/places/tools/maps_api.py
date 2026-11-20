"""Google Maps Platform over an API key, on the shared HTTP client (``standard.http``).

Two APIs, one key: Places API (New) and Routes API. Both refuse a request without ``X-Goog-FieldMask``, and both
bill by the fields a mask names, so every call here names exactly the fields its caller reads, never ``*``.

- The key goes in ``X-Goog-Api-Key``, never in an address.
- Every request runs off the agent's loop; a busy API (429 RESOURCE_EXHAUSTED, 503) is waited out within the shared
  client's budget and then reported as ``rate``.
- Google's own refusals are read from ``{"error": {"status", "message", "details": [{"reason"}]}}``: an invalid key,
  an API not enabled, a key restricted away from an API, or billing off are ``auth`` (a person fixes the setup),
  naming the API.
"""

from __future__ import annotations

import re
from typing import Any

import requests

from standard.http import Http, HttpError, segment

PLACES_BASE_URL = "https://places.googleapis.com/v1"
ROUTES_BASE_URL = "https://routes.googleapis.com"

# The names Google's console uses, so a person can find the switch.
PLACES_API = "Places API (New)"
ROUTES_API = "Routes API"

LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}$")
REGION = re.compile(r"^[A-Za-z]{2}$")


class Maps(Http):
    SERVICE = "Google Maps"
    BASE = PLACES_BASE_URL
    READ_TIMEOUT = 30
    # Every call here is a read sent as POST: Google's searches and routes take a body. None changes anything, so
    # each may be tried again like a read.
    WAIT_MOST = 20.0

    def __init__(self, secret: dict[str, Any]):
        super().__init__(str(secret.get("api_key") or "").strip(), PLACES_BASE_URL, secret.get("api_base_url"))
        # Tests point both APIs at one loopback server; their paths do not collide, so one override serves both.
        self.routes_base = self.base if self.base != PLACES_BASE_URL else ROUTES_BASE_URL

    def auth(self) -> dict[str, str]:
        if not self.token:
            raise HttpError("auth", "The Google Maps Platform credential has no API key: paste one in the agent's "
                                    "Credentials tab.")
        return {"X-Goog-Api-Key": self.token}

    @staticmethod
    def error_of(response: requests.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            return {"message": (response.text or "").strip()[:300]}
        if isinstance(body, list) and body:
            body = body[0]
        error = body.get("error") if isinstance(body, dict) else None
        return error if isinstance(error, dict) else {"message": str(body)[:300]}

    def detail(self, response: requests.Response) -> str:
        error = self.error_of(response)
        status, message = str(error.get("status") or ""), str(error.get("message") or "")[:300]
        return f"{message} ({status})" if status and status not in message else message or str(response.status_code)

    def refusal(self, response: requests.Response, method: str, said: str, words: str) -> HttpError:
        status = response.status_code
        api = ROUTES_API if ("directions/" in said or "distanceMatrix/" in said) else PLACES_API
        error = self.error_of(response)
        reasons = {str(d.get("reason") or "") for d in error.get("details") or [] if isinstance(d, dict)}
        lowered = str(error.get("message") or "").lower()
        if "API_KEY_INVALID" in reasons or "api key not valid" in lowered:
            return HttpError("auth", "Google refused the Google Maps Platform key as invalid: check the key pasted "
                                     "in the agent's Credentials tab.", status)
        if "SERVICE_DISABLED" in reasons or "has not been used in project" in lowered or "is disabled" in lowered:
            return HttpError("auth", f"The {api} is not enabled in the Google Cloud project this key belongs to: "
                                     f"enable the {api} there, wait a few minutes, and try again.", status)
        if "API_KEY_SERVICE_BLOCKED" in reasons or "are blocked" in lowered:
            return HttpError("auth", f"This key is restricted and may not call the {api}: add the {api} to the "
                                     f"key's API restrictions.", status)
        if "BILLING_DISABLED" in reasons or "billing" in lowered:
            return HttpError("auth", f"The {api} needs billing enabled on the Google Cloud project this key "
                                     f"belongs to.", status)
        if status in (401, 403):
            return HttpError("auth", f"Google refused this key for the {api}: {words}", status)
        if status == 404:
            return HttpError("not_found", f"Google has no such place: {words}", status)
        if status in (400, 422):
            return HttpError("invalid", f"The {api} refused the request: {words}", status, error)
        return HttpError("http", f"The {api} refused the request ({status}): {words}", status)

    async def ask(self, method: str, url: str, fields: list[str], body: Any = None,
                  params: dict[str, Any] | None = None) -> Any:
        if not fields or any(f.strip() in ("", "*") for f in fields):
            raise HttpError("invalid", "A Google Maps request names the fields it reads.")
        return await self.request(method, url, json=body, params=params, write=False,
                                  headers={"X-Goog-FieldMask": ",".join(fields)})

    # -- Places API (New) -------------------------------------------------------------------------------------------
    async def search_text(self, body: dict[str, Any], fields: list[str]) -> dict[str, Any]:
        return await self.ask("POST", f"{self.base}/places:searchText", fields, body)

    async def search_nearby(self, body: dict[str, Any], fields: list[str]) -> dict[str, Any]:
        return await self.ask("POST", f"{self.base}/places:searchNearby", fields, body)

    async def place(self, place_id: str, fields: list[str], language: str = "", region: str = "") -> dict[str, Any]:
        params = {**({"languageCode": language} if language else {}), **({"regionCode": region} if region else {})}
        return await self.ask("GET", f"{self.base}/places/{segment(place_id)}", fields, params=params or None)

    # -- Routes API ---------------------------------------------------------------------------------------------------
    async def route_matrix(self, body: dict[str, Any], fields: list[str]) -> list[dict[str, Any]]:
        # Over REST the matrix answers with a JSON array of elements, one per origin/destination pair, in no
        # promised order.
        answer = await self.ask("POST", f"{self.routes_base}/distanceMatrix/v2:computeRouteMatrix", fields, body)
        return answer if isinstance(answer, list) else []

    async def routes(self, body: dict[str, Any], fields: list[str]) -> dict[str, Any]:
        return await self.ask("POST", f"{self.routes_base}/directions/v2:computeRoutes", fields, body)
