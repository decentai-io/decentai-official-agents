"""Finding places and reading one, asking Google only for the fields wanted.

Google bills each Places call by the most expensive field its mask names. The fields every row shows (id, name,
address, location, Maps link) are the Pro tier; rating, price, opening hours, phone and website are Enterprise, and
are asked for only when ``include`` names them. Each answer says which tier it was billed at (``tier``).
"""

import math

from standard import ToolBase
from standard.http import HttpError

from .account_tool import client_for, no_key
from .localized import choose

# What every row shows: Pro-tier fields of Text Search, Nearby Search and Place Details alike.
ROW = ["id", "displayName", "formattedAddress", "location", "googleMapsUri"]
DETAIL = ROW + ["businessStatus"]
#: The Enterprise-tier fields, each asked only when ``include`` names it.
EXTRAS = {"rating": ["rating", "userRatingCount"], "price": ["priceLevel"],
          "open_now": ["currentOpeningHours.openNow"], "hours": ["regularOpeningHours.weekdayDescriptions"],
          "phone": ["nationalPhoneNumber", "internationalPhoneNumber"], "website": ["websiteUri"]}

EARTH_RADIUS_KM = 6371.0088


def clip(value, size=200):
    text = str(value or "")
    return text if len(text) <= size else text[: size - 1] + "…"


def straight_line_km(lat1, lng1, lat2, lng2):
    """Great-circle distance: arithmetic on Google's own coordinates, and named as what it is, not a travel
    distance."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def fields(base, include):
    """The mask's fields, and the tier Google bills them at."""
    extra = [f for name in include or [] for f in EXTRAS[name]]
    return list(dict.fromkeys(base + extra)), "enterprise" if extra else "pro"


def row(place):
    """One place as the agent shows it. A field Google did not return is left out rather than filled in."""
    out = {"place_id": str(place.get("id") or ""),
           "name": clip((place.get("displayName") or {}).get("text"), 120),
           "address": clip(place.get("formattedAddress"))}
    location = place.get("location") or {}
    if "latitude" in location and "longitude" in location:
        out["lat"] = float(location["latitude"])
        out["lng"] = float(location["longitude"])
    if place.get("rating") is not None:
        out["rating"] = float(place["rating"])
    if place.get("userRatingCount") is not None:
        out["rating_count"] = int(place["userRatingCount"])
    level = str(place.get("priceLevel") or "")
    if level and level != "PRICE_LEVEL_UNSPECIFIED":
        out["price_level"] = level.replace("PRICE_LEVEL_", "").lower()
    hours = place.get("currentOpeningHours") or {}
    if "openNow" in hours:
        out["open_now"] = bool(hours["openNow"])
    for source, target in (("nationalPhoneNumber", "phone"), ("internationalPhoneNumber", "international_phone"),
                           ("websiteUri", "website"), ("businessStatus", "business_status")):
        if place.get(source):
            out[target] = clip(place[source], 300)
    weekdays = (place.get("regularOpeningHours") or {}).get("weekdayDescriptions")
    if weekdays:
        out["hours"] = [clip(line, 120) for line in weekdays[:7]]
    if place.get("googleMapsUri"):
        out["maps_link"] = str(place["googleMapsUri"])
    return out


def localized(body, language, region):
    return {**body, **({"languageCode": language} if language else {}), **({"regionCode": region} if region else {})}


def told(answer, language, region, tier):
    """What was asked of Google, beside its answer: the language and region sent, and the tier billed."""
    return {**answer, **({"language": language} if language else {}), **({"region": region} if region else {}),
            "tier": tier}


class PlacesTool(ToolBase):
    id = "places"

    async def search(self, call):
        client, why = await client_for(call)
        if client is None:
            return no_key(why)
        inputs = call.inputs
        size = int(inputs.get("max_results") or 10)
        if ("lat" in inputs) != ("lng" in inputs):
            return {"error": "A location bias needs both lat and lng.", "kind": "invalid"}, "error"
        language, region, refused = choose(inputs, [inputs["query"]])
        if refused:
            return {"error": refused, "kind": "invalid"}, "error"
        body = {"textQuery": str(inputs["query"]), "pageSize": size}
        if "lat" in inputs:
            body["locationBias"] = {"circle": {
                "center": {"latitude": float(inputs["lat"]), "longitude": float(inputs["lng"])},
                "radius": float(inputs.get("radius_m") or 5000)}}
        if inputs.get("open_now"):
            body["openNow"] = True
        wanted, tier = fields(ROW, inputs.get("include"))
        try:
            answer = await client.search_text(localized(body, language, region), [f"places.{f}" for f in wanted])
        except HttpError as exc:
            return exc.answer()
        finally:
            client.close()
        places = [row(p) for p in (answer.get("places") or [])[:size]]
        return told({"places": places, "count": len(places)}, language, region, tier), "success"

    async def nearby(self, call):
        client, why = await client_for(call)
        if client is None:
            return no_key(why)
        inputs = call.inputs
        size = int(inputs.get("max_results") or 10)
        language, region, refused = choose(inputs, [])
        if refused:
            return {"error": refused, "kind": "invalid"}, "error"
        if not inputs.get("place_id") and not ("lat" in inputs and "lng" in inputs):
            return {"error": "Give a place_id, or both lat and lng.", "kind": "invalid"}, "error"
        wanted, tier = fields(ROW, inputs.get("include"))
        try:
            if inputs.get("place_id"):
                # Nearby Search takes a circle, never a place, so a place id is resolved to Google's coordinates
                # for it first: Place Details asking for its location alone (the Essentials tier).
                centre = (await client.place(str(inputs["place_id"]), ["location"])).get("location") or {}
                if "latitude" not in centre or "longitude" not in centre:
                    return {"error": "Google returned no location for that place.", "kind": "not_found"}, "error"
                lat, lng = float(centre["latitude"]), float(centre["longitude"])
            else:
                lat, lng = float(inputs["lat"]), float(inputs["lng"])
            body = {"maxResultCount": size, "rankPreference": "DISTANCE",
                    "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lng},
                                                       "radius": float(inputs.get("radius_m") or 1000)}}}
            if inputs.get("types"):
                body["includedTypes"] = [str(t) for t in inputs["types"]]
            answer = await client.search_nearby(localized(body, language, region), [f"places.{f}" for f in wanted])
        except HttpError as exc:
            return exc.answer()
        finally:
            client.close()
        places = []
        for place in (answer.get("places") or [])[:size]:
            shown = row(place)
            if "lat" in shown:
                shown["straight_line_km"] = round(straight_line_km(lat, lng, shown["lat"], shown["lng"]), 2)
            places.append(shown)
        return told({"center": {"lat": lat, "lng": lng}, "places": places, "count": len(places)},
                    language, region, tier), "success"

    async def details(self, call):
        client, why = await client_for(call)
        if client is None:
            return no_key(why)
        language, region, refused = choose(call.inputs, [])
        if refused:
            return {"error": refused, "kind": "invalid"}, "error"
        wanted, tier = fields(DETAIL, call.inputs.get("include"))
        try:
            place = await client.place(str(call.inputs["place_id"]), wanted, language, region)
        except HttpError as exc:
            return exc.answer()
        finally:
            client.close()
        return told(row(place), language, region, tier), "success"
