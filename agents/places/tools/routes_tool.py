"""Distance, travel time and one trip's route, from Google's Routes API.

Billing: a matrix element and a route are the Essentials tier; driving at a stated time asks for traffic, which is
Pro. A transit trip asks for each step's line, stops and times and the fare (``routes.legs.steps.transitDetails``,
``routes.travelAdvisory.transitFare``) only when the mode is transit.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode

from standard import ToolBase
from standard.http import HttpError

from .account_tool import client_for, no_key
from .localized import choose, routes_region

# Routes API travel modes, and the words a Google Maps link uses for them.
MODES = {"drive": ("DRIVE", "driving"), "walk": ("WALK", "walking"),
         "bicycle": ("BICYCLE", "bicycling"), "transit": ("TRANSIT", "transit")}

MATRIX_FIELDS = ["originIndex", "destinationIndex", "status", "condition",
                 "distanceMeters", "duration", "staticDuration"]
ROUTE_FIELDS = ["routes.distanceMeters", "routes.duration", "routes.staticDuration",
                "routes.description", "routes.warnings",
                "routes.legs.steps.distanceMeters",
                "routes.legs.steps.navigationInstruction.instructions"]
#: Asked only for a transit trip: each step's mode, the line, stops and times, and the fare where Google has one.
TRANSIT_FIELDS = ["routes.legs.steps.travelMode", "routes.legs.steps.transitDetails",
                  "routes.travelAdvisory.transitFare"]
MAX_STEPS = 20
#: How far from now Google plans a transit trip: 7 days back to 100 days ahead.
TRANSIT_PAST, TRANSIT_AHEAD = timedelta(days=7), timedelta(days=100)


class Location:
    """Where a trip starts or ends, as the person gave it: an address, a Google place id, or coordinates; exactly
    one."""

    def __init__(self, given):
        self.address = str(given.get("address") or "").strip()
        self.place_id = str(given.get("place_id") or "").strip()
        self.has_point = "lat" in given and "lng" in given
        self.lat = float(given["lat"]) if self.has_point else 0.0
        self.lng = float(given["lng"]) if self.has_point else 0.0
        chosen = sum(1 for present in (self.address, self.place_id, self.has_point) if present)
        if chosen != 1:
            raise ValueError("each location is exactly one of: an address, a place_id, or both lat and lng")

    def label(self):
        if self.address:
            return self.address
        if self.place_id:
            return f"place_id:{self.place_id}"
        return f"{self.lat},{self.lng}"

    def waypoint(self):
        if self.address:
            return {"address": self.address}
        if self.place_id:
            return {"placeId": self.place_id}
        return {"location": {"latLng": {"latitude": self.lat, "longitude": self.lng}}}

    def link_params(self, role):
        # A Maps URL needs the text form even when a place id is given; the place id then wins.
        if self.place_id:
            return {role: self.place_id, f"{role}_place_id": self.place_id}
        if self.address:
            return {role: self.address}
        return {role: f"{self.lat},{self.lng}"}


def seconds(duration):
    """Google's durations are strings such as "754s"."""
    text = str(duration or "").strip()
    if not text.endswith("s"):
        return None
    try:
        return float(text[:-1])
    except ValueError:
        return None


def minutes(duration):
    value = seconds(duration)
    return None if value is None else round(value / 60, 1)


def directions_link(origin, destination, mode):
    params = {"api": "1", **origin.link_params("origin"),
              **destination.link_params("destination"), "travelmode": MODES[mode][1]}
    return "https://www.google.com/maps/dir/?" + urlencode(params)


def money(value):
    """Google's Money ({currencyCode, units, nanos}) as an exact decimal text and its currency; None when absent."""
    if not isinstance(value, dict) or not value.get("currencyCode"):
        return None
    amount = Decimal(str(value.get("units") or 0)) + Decimal(int(value.get("nanos") or 0)) / Decimal(10 ** 9)
    return {"currency": str(value["currencyCode"]), "amount": format(amount.normalize(), "f")}


def step(found, transit):
    """One step as shown: its instruction and length; on a transit trip its mode, and for a ride the line, the
    stops, the times and the number of stops, each only where Google gave it."""
    shown = {"instruction": str((found.get("navigationInstruction") or {}).get("instructions") or "")[:200],
             "distance_km": round(int(found.get("distanceMeters") or 0) / 1000, 2)}
    if not transit:
        return shown
    if found.get("travelMode"):
        shown["mode"] = str(found["travelMode"]).lower()
    details = found.get("transitDetails") or {}
    if not details:
        return shown
    line = details.get("transitLine") or {}
    stops = details.get("stopDetails") or {}
    local = details.get("localizedValues") or {}
    vehicle = line.get("vehicle") or {}
    ride = {"line": str(line.get("nameShort") or line.get("name") or "")[:120],
            "line_name": str(line.get("name") or "")[:200],
            "vehicle": str((vehicle.get("name") or {}).get("text") or "")[:60],
            "vehicle_type": str(vehicle.get("type") or "").lower(),
            "agency": str(((line.get("agencies") or [{}])[0] or {}).get("name") or "")[:120],
            "headsign": str(details.get("headsign") or "")[:120],
            "from_stop": str((stops.get("departureStop") or {}).get("name") or "")[:120],
            "to_stop": str((stops.get("arrivalStop") or {}).get("name") or "")[:120],
            "departs": str(stops.get("departureTime") or ""),
            "arrives": str(stops.get("arrivalTime") or ""),
            "departs_local": str(((local.get("departureTime") or {}).get("time") or {}).get("text") or "")[:40],
            "arrives_local": str(((local.get("arrivalTime") or {}).get("time") or {}).get("text") or "")[:40]}
    shown.update({k: v for k, v in ride.items() if v})
    if details.get("stopCount") is not None:
        shown["stops"] = int(details["stopCount"])
    return shown


class RoutesTool(ToolBase):
    id = "routes"

    @staticmethod
    def _trip(inputs):
        """The travel mode, the departure time Google is asked about, and whether the answer accounts for traffic;
        or a refusal."""
        mode = str(inputs.get("mode") or "drive")
        body = {"travelMode": MODES[mode][0]}
        departure = str(inputs.get("departure_time") or "").strip()
        if departure:
            try:
                moment = datetime.fromisoformat(departure.replace("Z", "+00:00"))
            except ValueError:
                return None, None, "departure_time must be ISO 8601 with an offset, e.g. 2026-09-14T08:30:00+04:00."
            if moment.tzinfo is None:
                return None, None, "departure_time needs an offset (e.g. +04:00); this agent does not guess a time " \
                                   "zone."
            now = datetime.now(timezone.utc)
            if mode == "transit" and not now - TRANSIT_PAST <= moment <= now + TRANSIT_AHEAD:
                return None, None, "departure_time for transit must be within 7 days before and 100 days after " \
                                   "now; Google plans no transit trip outside that."
            if mode != "transit" and moment < now:
                return None, None, "departure_time is in the past; Google estimates travel from now onwards."
            body["departureTime"] = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Traffic is only ever asked for when driving at a stated time; Google refuses a routing preference for any
        # other mode.
        traffic = mode == "drive" and bool(departure)
        if traffic:
            body["routingPreference"] = "TRAFFIC_AWARE"
        return body, traffic, ""

    @staticmethod
    def _asked_at():
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    @staticmethod
    def _localized(language, region):
        return {**({"languageCode": language} if language else {}),
                **({"regionCode": routes_region(region)} if region else {})}

    @staticmethod
    def _said(language, region):
        return {**({"language": language} if language else {}), **({"region": region} if region else {})}

    async def distance(self, call):
        inputs = call.inputs
        try:
            origin = Location(inputs["origin"])
            destinations = [Location(d) for d in inputs["destinations"]]
        except ValueError as exc:
            return {"error": str(exc), "kind": "invalid"}, "error"
        trip, traffic, problem = self._trip(inputs)
        language, region, refused = choose(inputs, [p.address for p in (origin, *destinations)])
        if problem or refused:
            return {"error": problem or refused, "kind": "invalid"}, "error"
        client, why = await client_for(call)
        if client is None:
            return no_key(why)
        mode = str(inputs.get("mode") or "drive")
        body = {"origins": [{"waypoint": origin.waypoint()}],
                "destinations": [{"waypoint": d.waypoint()} for d in destinations], **trip,
                **self._localized(language, region)}
        try:
            elements = await client.route_matrix(body, MATRIX_FIELDS)
        except HttpError as exc:
            return exc.answer()
        finally:
            client.close()

        # Elements arrive in no promised order, and a zero index is left out of the JSON entirely, so absence
        # means 0.
        by_index = {int(e.get("destinationIndex") or 0): e for e in elements if int(e.get("originIndex") or 0) == 0}
        results = []
        for index, destination in enumerate(destinations):
            shown = {"index": index, "destination": destination.label()[:200],
                     "directions_link": directions_link(origin, destination, mode)}
            element = by_index.get(index)
            status = (element or {}).get("status") or {}
            if element is None:
                shown.update(found=False, problem="Google returned no result for this destination.")
            elif status.get("code") or element.get("condition") != "ROUTE_EXISTS":
                shown.update(found=False, problem=str(
                    status.get("message") or element.get("condition") or "no route")[:200])
            else:
                shown["found"] = True
                shown["distance_km"] = round(int(element.get("distanceMeters") or 0) / 1000, 1)
                if traffic:
                    # Traffic-aware: duration includes traffic, and staticDuration is the same trip without it.
                    shown["duration_minutes"] = minutes(element.get("staticDuration"))
                    shown["duration_in_traffic_minutes"] = minutes(element.get("duration"))
                else:
                    shown["duration_minutes"] = minutes(element.get("duration"))
                shown = {k: v for k, v in shown.items() if v is not None}
            results.append(shown)
        answer = {"origin": origin.label()[:200], "mode": mode, "traffic_aware": traffic,
                  "estimated_at": self._asked_at(), "results": results, **self._said(language, region)}
        if trip.get("departureTime"):
            answer["departure_time"] = trip["departureTime"]
        return answer, "success"

    async def between(self, call):
        inputs = call.inputs
        try:
            origin = Location(inputs["origin"])
            destination = Location(inputs["destination"])
        except ValueError as exc:
            return {"error": str(exc), "kind": "invalid"}, "error"
        trip, traffic, problem = self._trip(inputs)
        language, region, refused = choose(inputs, [origin.address, destination.address])
        if problem or refused:
            return {"error": problem or refused, "kind": "invalid"}, "error"
        client, why = await client_for(call)
        if client is None:
            return no_key(why)
        mode = str(inputs.get("mode") or "drive")
        transit = mode == "transit"
        body = {"origin": origin.waypoint(), "destination": destination.waypoint(), **trip,
                **self._localized(language, region)}
        try:
            answer = await client.routes(body, ROUTE_FIELDS + (TRANSIT_FIELDS if transit else []))
        except HttpError as exc:
            return exc.answer()
        finally:
            client.close()
        result = {"origin": origin.label()[:200], "destination": destination.label()[:200],
                  "mode": mode, "traffic_aware": traffic, "estimated_at": self._asked_at(),
                  "directions_link": directions_link(origin, destination, mode), **self._said(language, region)}
        if trip.get("departureTime"):
            result["departure_time"] = trip["departureTime"]
        routes = answer.get("routes") or []
        if not routes:
            result.update(found=False, problem="Google found no route between these places.")
            return result, "success"
        route = routes[0]
        result["found"] = True
        result["distance_km"] = round(int(route.get("distanceMeters") or 0) / 1000, 1)
        if traffic:
            result["duration_minutes"] = minutes(route.get("staticDuration"))
            result["duration_in_traffic_minutes"] = minutes(route.get("duration"))
        else:
            result["duration_minutes"] = minutes(route.get("duration"))
        if route.get("description"):
            result["summary"] = str(route["description"])[:200]
        result["warnings"] = [str(w)[:200] for w in (route.get("warnings") or [])[:5]]
        steps = [s for leg in route.get("legs") or [] for s in leg.get("steps") or []]
        result["steps_total"] = len(steps)
        result["steps"] = [step(s, transit) for s in steps[:MAX_STEPS]]
        if transit:
            fare = money((route.get("travelAdvisory") or {}).get("transitFare"))
            if fare:
                result["fare"] = fare
        return {k: v for k, v in result.items() if v is not None}, "success"
