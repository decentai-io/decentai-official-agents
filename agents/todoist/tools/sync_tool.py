"""Watching what was ticked off in Todoist, so a schedule can carry it elsewhere (the platform's Tasks agent, a
report) without anyone asking.

Where completions are read:

- **The activity log** (``GET /activities``, ``item:completed``): one event per completion, so a recurring task's
  occurrences are there too. It is the default. How far back it reaches depends on the person's plan.
- **Completed tasks** (``GET /tasks/completed/by_completion_date``), when the plan has no activity log: a recurring
  task stays open at its next date and is never listed there, so such a watch says it cannot see them.

A watch remembers how far completions have been handed on: the time of the newest one seen, in Todoist's own clock,
and the ids sitting exactly on that time (activity event ids, or task ids for completed tasks), so a second
completion in the same instant is neither lost nor shown twice. ``standard.watch.check`` reads each watch alone and
writes the cursors only once every watch was read: one that fails keeps its place, one whose project is gone closes.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from standard import ToolBase
from standard.accounts import account
from standard.http import HttpError
from standard.watch import Gone, Seen, check

from .account_tool import SECRET, client_for, failure, not_connected
from .catalog import Catalog, task_row
from .todoist_api import Todoist, task_link

#: Todoist answers at most three months of completed tasks per request.
WINDOW = timedelta(days=89)
#: How far back a new watch looks for the newest completion to start from.
LOOKBACK = timedelta(days=1)
#: A check's time, well inside sync_completed's timeout.
BUDGET = 60.0
ACTIVE = ("active", "watching")   # "watching" is what 1.0.0 wrote
NO_RECURRING = ("This Todoist plan has no activity log, so the watch reads completed tasks: a recurring task's "
                "completions are not seen there.")


def parse_time(text):
    """Todoist's '2026-09-13T09:00:00.000000Z', or None."""
    text = str(text or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def wire_time(moment):
    """Whole seconds, UTC: how a range is asked for. Rounded down, so the range still includes the cursor itself;
    the ids on it decide."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def denied(exc):
    """The plan has no activity log (or the token may not read it)."""
    return exc.kind in ("denied", "scope")


class Completions:
    """Reading one watch's completions from its source, newest first as Todoist pages them, sorted here."""

    def __init__(self, client: Todoist):
        self.client = client

    async def activity(self, since, project_id):
        """(rows of (moment, id, event), where the read stopped short of now or None). The window is halved while it holds more than can be read, so
        the oldest completions are never the ones left out."""
        end = datetime.now(timezone.utc) + timedelta(seconds=1)
        for _ in range(5):
            events, left = await self.client.completions(wire_time(since), wire_time(end), project_id)
            if not left:
                break
            end = since + (end - since) / 2
        else:
            raise HttpError("http", "Too many completions to read in one check, even an hour at a time.")
        found = []
        for event in events:
            moment = parse_time(event.get("event_date"))
            if moment is not None and str(event.get("event_type") or "completed") == "completed":
                found.append((moment, str(event.get("id")), event))
        # Read only up to ``end``: the next check goes on from there.
        return found, (end if end < datetime.now(timezone.utc) else None)

    async def completed(self, since, project_id):
        now = datetime.now(timezone.utc)
        # A watch left alone for months is read three months at a time; otherwise up to a second past now, since
        # until is exclusive and the range is asked in whole seconds.
        capped = since + WINDOW < now
        until = since + WINDOW if capped else max(now + timedelta(seconds=1), since + timedelta(seconds=1))
        items, left = await self.client.completed(wire_time(since), wire_time(until), project_id)
        if left:
            raise HttpError("http", "Too many completions to read in one check.")
        found = []
        for task in items:
            moment = parse_time(task.get("completed_at"))
            if moment is not None:
                found.append((moment, str(task.get("id")), task))
        return found, (until if capped else None)


class SyncTool(ToolBase):
    id = "sync"

    async def watch(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        inputs = call.inputs
        project_id = project_name = ""
        try:
            if inputs.get("project"):
                project, problem = await Catalog(client).project(inputs["project"])
                if problem:
                    return problem, "error"
                project_id, project_name = str(project["id"]), str(project.get("name") or "")
            asked = str(inputs.get("since") or "").strip()
            moment = None
            if asked:
                moment = parse_time(asked)
                if moment is None:
                    return {"error": f"since must be an ISO 8601 date-time, not {asked!r}.", "kind": "invalid"}, "error"
                if datetime.fromisoformat(asked.replace("Z", "+00:00")).tzinfo is None:
                    # A time with no offset is the person's own: the zone given, else their Todoist's.
                    zone_name = str(inputs.get("timezone") or "").strip()
                    if not zone_name:
                        zone_name = str(((await client.user()).get("tz_info") or {}).get("timezone") or "UTC")
                    try:
                        zone = ZoneInfo(zone_name)
                    except (ZoneInfoNotFoundError, ValueError):
                        return {"error": f"{zone_name!r} is not an IANA time zone such as Asia/Dubai.",
                                "kind": "invalid"}, "error"
                    moment = datetime.fromisoformat(asked).replace(tzinfo=zone)
            now = datetime.now(timezone.utc)
            source = "activity"
            try:
                recent, _ = await Completions(client).activity(now - LOOKBACK, project_id)
            except HttpError as exc:
                if not denied(exc):
                    raise
                source = "completed"
                recent, _ = await Completions(client).completed(now - LOOKBACK, project_id)
            cursor_ids = ""
            if moment is not None:
                since = stamp(moment)
            elif recent:
                # From now, and "now" is Todoist's: the newest recent completion marks the spot and is not news.
                newest = max(m for m, _, _ in recent)
                at_newest = [(i, row) for m, i, row in recent if m == newest]
                since = str(at_newest[0][1].get("event_date" if source == "activity" else "completed_at"))
                cursor_ids = ",".join(sorted(i for i, _ in at_newest))
            else:
                since = stamp(now)
        except HttpError as exc:
            return failure(exc)
        watch_ref = await call.records.create("watch", {
            "since": since, "cursor_ids": cursor_ids, "status": "active", "source": source,
            "account": client.account, "project_id": project_id, "project": project_name,
            "note": str(inputs.get("note") or "")})
        return {"watch_ref": watch_ref, "since": since, "project": project_name, "source": source,
                "recurring_seen": source == "activity",
                **({"warning": NO_RECURRING} if source == "completed" else {})}, "success"

    async def completed(self, call):
        wanted = str(call.inputs.get("watch_ref") or "")
        most = int(call.inputs.get("max_results") or 20)
        who = str(call.inputs.get("account") or "").strip().lower()
        if wanted:
            watches = [await call.records.read("watch", wanted)]
        else:
            watches = await call.records.list("watch")
        watches = [w for w in watches if str(w["fields"].get("status") or "active") in ACTIVE
                   and (not who or who in str(w["fields"].get("account") or "").lower())]
        if not watches and not await call.records.list(SECRET):
            return not_connected("No Todoist account is connected to this agent.")
        clients, catalogs = {}, {}

        async def client_of(fields):
            name = str(fields.get("account") or "")
            if name not in clients:
                secret, why = await account(call, SECRET, name, ask=False, service="Todoist")
                clients[name] = (Todoist(secret) if secret else None, why)
                catalogs[name] = Catalog(clients[name][0]) if secret else None
            client, why = clients[name]
            if client is None:
                raise HttpError("auth", why)
            return client, catalogs[name]

        async def read(watch):
            fields = watch["fields"]
            ref = str(watch["ref"])
            client, catalog = await client_of(fields)
            project_id = str(fields.get("project_id") or "")
            if project_id and project_id not in {str(p.get("id")) for p in await catalog.projects()}:
                raise Gone(f"The project {fields.get('project') or project_id} is no longer in Todoist (deleted or archived).")
            since_text = str(fields.get("since") or "")
            since = parse_time(since_text) or datetime.now(timezone.utc)
            seen = {i for i in str(fields.get("cursor_ids") or "").split(",") if i}
            source = str(fields.get("source") or "completed")   # 1.0.0's watches read completed tasks
            cursor = {"status": "active"} if fields.get("status") != "active" else {}
            reader = Completions(client)
            if source == "activity":
                try:
                    found, stepped = await reader.activity(since, project_id)
                except HttpError as exc:
                    if not denied(exc):
                        raise
                    source = cursor["source"] = "completed"
                    found, stepped = await reader.completed(since, project_id)
            else:
                found, stepped = await reader.completed(since, project_id)
            fresh = sorted(((m, i, row) for m, i, row in found
                            if m > since or (m == since and i not in seen)), key=lambda e: (e[0], e[1]))
            more = stepped is not None
            if len(fresh) > most:
                more, fresh = True, fresh[:most]
            if not fresh:
                if stepped is not None:
                    # Nothing new up to where this read stopped: the next check goes on from there.
                    cursor.update({"since": wire_time(stepped), "cursor_ids": ""})
                return Seen([], cursor or None, more=more)
            names = await catalog.project_names()
            rows = []
            if source == "activity":
                open_tasks = await client.tasks_by_id(sorted({str(e.get("object_id")) for _, _, e in fresh}))
                for moment, _, event in fresh:
                    rows.append(self._event_row(event, open_tasks, names))
            else:
                rows = [{**task_row(task, names), "completed_at": str(task.get("completed_at") or "")}
                        for _, _, task in fresh]
            newest = fresh[-1][0]
            at_newest = {i for m, i, _ in fresh if m == newest}
            if newest == since:
                at_newest |= seen
            cursor.update({"since": str(fresh[-1][2].get("event_date" if source == "activity" else "completed_at")),
                           "cursor_ids": ",".join(sorted(at_newest))})
            return Seen([{**row, "watch_ref": ref} for row in rows], cursor, more=more)

        def named(watch):
            return str(watch["fields"].get("project") or "every project")

        found = await check(call, "watch", watches, read, budget=BUDGET, name=named)
        return {"checked": found["checked"], "completed": found["rows"], "more": found["more"],
                "problems": found["problems"], "closed": found["closed"]}, "success"

    @staticmethod
    def _event_row(event, open_tasks, names):
        """One completion: what the event says, and the task's own fields while it is still open (a recurring
        task is, at its next date)."""
        task_id = str(event.get("object_id") or "")
        extra = event.get("extra_data") or {}
        project_id = str(event.get("parent_project_id") or "")
        task = open_tasks.get(task_id)
        if task is not None:
            row = task_row(task, names)
            recurring = bool((task.get("due") or {}).get("is_recurring"))
            if recurring:
                # Its due is the next occurrence now, not the one completed.
                row["next_due_date"] = row.pop("due_date")
        else:
            row = {"task_id": task_id, "project_id": project_id, "project": names.get(project_id, ""),
                   "recurring": False, "link": task_link(task_id)}
        if extra.get("content"):
            row["content"] = str(extra["content"])
        row.setdefault("content", "")
        row["completed_at"] = str(event.get("event_date") or "")
        return row
