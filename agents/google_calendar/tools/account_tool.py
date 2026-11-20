"""Which Google accounts are connected, and each one's zone and today; and the calendars of one."""

from __future__ import annotations

from standard import ResourceDenied, ToolBase
from standard.accounts import listed
from standard.google import HttpError

from .common import Refused, answers, calendar_zone, client, today


class AccountTool(ToolBase):
    id = "account"

    async def status(self, call):
        """A status asks nobody: with no account kept, it says so. Each account is tried on its own."""
        wanted = str(call.inputs.get("account") or "").strip()
        try:
            names = [wanted] if wanted else await listed(call, "google")
        except ResourceDenied as failed:
            return {"accounts": [], "problem": f"No Google account is connected to this agent: {failed}"}, "success"
        if not names:
            return {"accounts": [], "problem": "No Google account is connected to this agent."}, "success"
        rows = []
        for name in names:
            try:
                google = await client(call, "calendar", name, ask=False)
                zone, zone_name = await calendar_zone(self.agent, google, "primary")
                row = {"account": google.account or name, "connected": True, "timezone": zone_name,
                       "today": today(zone).isoformat()}
                try:
                    tasks = await client(call, "tasks", google.account or name, ask=False)
                    await tasks.get("users/@me/lists", params={"maxResults": 1})
                except (Refused, HttpError) as failed:
                    row["problem"] = f"Calendar works, Google Tasks does not: {failed.message}"
                rows.append(row)
            except (Refused, HttpError) as failed:
                rows.append({"account": name, "connected": False, "problem": failed.message})
        return {"accounts": rows}, "success"


class CalendarsTool(ToolBase):
    id = "calendars"

    @answers
    async def list(self, call):
        google = await client(call)
        found = await google.token_pages("users/me/calendarList", params={"maxResults": 250})
        rows = [{"calendar_id": str(c.get("id") or ""),
                 "name": str(c.get("summaryOverride") or c.get("summary") or ""),
                 "primary": c.get("primary") is True,
                 "access": str(c.get("accessRole") or ""),
                 "timezone": str(c.get("timeZone") or "")} for c in found if not c.get("deleted")]
        rows.sort(key=lambda r: (not r["primary"], r["name"].lower()))
        return {"account": google.account, "calendars": rows}, "success"
