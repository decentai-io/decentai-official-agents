"""People in the workspace, found by address, name or user id, so a direct message goes to the right one.

An address is asked of Slack directly (users.lookupByEmail). A name is matched in code over the workspace's members:
exactly first (display name, full name or handle, any case), then by a part that names one person only. Several
matches are never guessed between: they come back as candidates for the person to choose from.
"""

import re

from standard import ToolBase
from standard.http import HttpError

from .account_tool import client_for, failure, not_connected
from .slack_api import display

USER_ID = re.compile(r"^[UW][A-Z0-9]{2,}$")
ADDRESS = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


def person(user):
    profile = user.get("profile") or {}
    row = {"user_id": str(user.get("id") or ""), "name": display(user) or str(user.get("id") or ""),
           "real_name": str(profile.get("real_name") or user.get("real_name") or ""),
           "handle": str(user.get("name") or "")}
    if profile.get("email"):
        row["email"] = str(profile["email"])
    if profile.get("title"):
        row["title"] = str(profile["title"])
    return row


def names_of(user, handle=True):
    profile = user.get("profile") or {}
    named = [profile.get("display_name"), profile.get("real_name"), user.get("real_name")]
    return [str(n).strip().casefold() for n in (named + [user.get("name")] if handle else named)
            if n and str(n).strip()]


async def resolve(client, to):
    """(the person, [], "") or (None, candidates, why)."""
    handle = str(to or "").strip().startswith("@")
    wanted = str(to or "").strip().removeprefix("@").strip()
    if not wanted:
        return None, [], "Say who: an address, a name or a Slack user id."
    if ADDRESS.match(wanted):
        try:
            user = await client.user_by_email(wanted)
        except HttpError as refused:
            if refused.kind == "not_found":
                return None, [], f"Nobody in this Slack workspace has the address {wanted}."
            raise
        return person(user), [], ""
    if USER_ID.match(wanted):
        try:
            return person(await client.user(wanted)), [], ""
        except HttpError as refused:
            if refused.kind == "not_found":
                return None, [], f"There is no user {wanted} in this Slack workspace."
            raise
    found = await matches(client, wanted, handle)
    if len(found) == 1:
        return person(found[0]), [], ""
    if not found:
        return None, [], f"Nobody in this Slack workspace is called {wanted!r}: give their address instead."
    return None, [person(u) for u in found[:10]], (f"{len(found)} people in this Slack workspace match {wanted!r}: "
                                                    f"ask which one, then send to their user_id.")


async def matches(client, wanted, handle=False):
    """Exactly a full name (or, written with @, a handle); else a part of a name that only one person has. A first
    name alone is a part: "Dana" with two Danas in the workspace is asked, never picked."""
    folded = wanted.casefold()
    users = [u for u in await client.users() if not u.get("deleted") and not u.get("is_bot")
             and str(u.get("id") or "") != "USLACKBOT"]
    exact = [u for u in users if folded in names_of(u, handle=handle)]
    return exact or [u for u in users if any(folded in n for n in names_of(u))]


class PeopleTool(ToolBase):
    id = "people"

    async def find(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        wanted = str(call.inputs["who"]).strip()
        try:
            found, candidates, problem = await resolve(client, wanted)
        except HttpError as exc:
            return failure(exc)
        people = [found] if found else candidates
        return {"people": people, **({"problem": problem} if problem and not people else {})}, "success"
