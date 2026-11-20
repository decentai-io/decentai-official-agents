from standard import ResourceDenied, ToolBase
from standard.http import HttpError

from .maps_api import Maps

NO_KEY = ("No Google Maps Platform key is granted to this agent: an administrator adds one on the agent's "
          "Credentials tab")


async def client_for(call):
    """A client over the key kept; with none kept, the person is asked to fill one in. One key per deployment: this
    is not an account agent, so no function takes an ``account``."""
    try:
        secret = await call.records.secret("maps")
    except ResourceDenied as exc:
        return None, f"{NO_KEY}: {exc}"
    return Maps(secret), ""


def no_key(why: str):
    return {"error": why, "kind": "auth"}, "error"


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        """Both APIs checked with calls Google does not bill: a Text Search asking for ids only (the free
        "Essentials (IDs Only)" SKU), and a route request with no destination, which Google refuses as incomplete
        after it has accepted the key and found the Routes API enabled. Nothing is routed, nothing billed."""
        kept = await call.records.list("maps")
        client, why = await client_for(call) if kept else (None, f"{NO_KEY}.")
        if client is None:
            return {"connected": False, "places_api": "", "routes_api": "", "problem": why}, "success"
        places, routes = "ok", "ok"
        try:
            await client.search_text({"textQuery": "coffee", "pageSize": 1}, ["places.id"])
        except HttpError as exc:
            places = exc.message
        try:
            point = {"location": {"latLng": {"latitude": 25.2048, "longitude": 55.2708}}}
            await client.routes({"origin": point}, ["routes.distanceMeters"])
            routes = "ok"
        except HttpError as exc:
            # The refusal of the incomplete request is the answer wanted: the key and the API were accepted.
            routes = "ok" if exc.kind == "invalid" else exc.message
        finally:
            client.close()
        problems = [p for p in (places, routes) if p != "ok"]
        result = {"connected": not problems, "places_api": places, "routes_api": routes}
        if problems:
            result["problem"] = " ".join(problems)
        return result, "success"
