"""A start/stop timer: one running at a time, kept as a record so it survives the chat's process ending. Stopping it
logs the time on the project, rounded to the quarter hour, on the day it started in the person's zone. A timer left
running for more than a working day is not logged without asking: it was probably forgotten."""

from datetime import datetime
from decimal import Decimal

from standard import ToolBase

from .common import (DAY_LIMIT, as_text, code_of, entries, entry_row, keys_of, locked, now_utc, projects,
                     quarter_hours, week_of, zone_of)

#: Running longer than this, a timer is logged only when the person says so.
LONG = Decimal(10)


def _started(keys):
    try:
        return datetime.fromisoformat(str(keys.get("started_at") or ""))
    except ValueError:
        return None


def _shown(ref, keys, now):
    started = _started(keys)
    minutes = int((now - started).total_seconds() // 60) if started else 0
    return {"timer_ref": ref, "project_code": str(keys.get("project_code") or ""),
            "note": str(keys.get("note") or ""), "started_at": str(keys.get("started_at") or ""),
            "running_minutes": max(minutes, 0), "hours_so_far": as_text(quarter_hours(max(minutes, 1)))}


async def _running(call):
    return [r for r in await call.records.list("timer", {"status": "running"})]


class TimerTool(ToolBase):
    id = "timer"

    async def start(self, call):
        inputs = call.inputs
        _, zone, problem = zone_of(inputs)
        if problem:
            return {"error": problem}, "error"
        now = now_utc()
        running = await _running(call)
        if running:
            shown = _shown(running[0]["ref"], keys_of(running[0]), now)
            return {"error": f"A timer is already running on {shown['project_code']} since {shown['started_at']} "
                             f"({shown['running_minutes']} minutes): stop or cancel it first."}, "error"
        code = code_of(inputs["project_code"])
        project = (await projects(call)).get(code)
        if project is None or project["fields"].get("status") != "active":
            return {"error": f"No active project {code}."}, "error"
        fields = {"project_code": code, "status": "running", "started_at": now.isoformat(timespec="seconds"),
                  "timezone": zone}
        if inputs.get("note"):
            fields["note"] = str(inputs["note"]).strip()
        ref = await call.records.create("timer", fields)
        return {"timer_ref": ref, "project_code": code, "started_at": fields["started_at"],
                "timezone": zone}, "success"

    async def status(self, call):
        now = now_utc()
        running = await _running(call)
        if not running:
            return {"running": False}, "success"
        return {"running": True, **_shown(running[0]["ref"], keys_of(running[0]), now)}, "success"

    async def cancel(self, call):
        running = await _running(call)
        if not running:
            return {"error": "No timer is running."}, "error"
        await call.records.update("timer", running[0]["ref"], {"status": "cancelled",
                                                               "stopped_at": now_utc().isoformat(timespec="seconds")})
        return {"cancelled": True, "timer_ref": running[0]["ref"], "logged": False}, "success"

    async def stop(self, call):
        """The running timer stopped and logged. It is marked stopped before its entry is made, and the entry names
        its timer, so a stop retried after a lost answer finishes that one (the entry made once) instead of failing
        or logging twice."""
        inputs = call.inputs
        running = await _running(call)
        if running:
            ref, keys, stopped, fresh = running[0]["ref"], keys_of(running[0]), now_utc(), True
        else:
            pending = [r for r in await call.records.list("timer", {"status": "stopped"})
                       if not keys_of(r).get("entry_ref")]
            if not pending:
                return {"error": "No timer is running."}, "error"
            ref, keys, fresh = pending[-1]["ref"], keys_of(pending[-1]), False
            try:
                stopped = datetime.fromisoformat(str(keys.get("stopped_at") or ""))
            except ValueError:
                stopped = now_utc()
        started = _started(keys)
        if started is None:
            return {"error": f"The timer {ref} has no start time; cancel it and log the time with entries_log."},                 "error"
        zone, zone_name, problem = zone_of({"timezone": inputs.get("timezone") or keys.get("timezone")})
        if problem:
            return {"error": problem}, "error"
        minutes = max((stopped - started).total_seconds() / 60, 0)
        hours = quarter_hours(minutes)
        day = started.astimezone(zone).date()
        if hours > DAY_LIMIT:
            return {"error": f"The timer ran {as_text(hours)} hours, more than a day holds. Cancel it with "
                             f"timer_cancel and log each day with entries_log."}, "error"
        if hours > LONG and fresh:
            answer = await call.ask(
                f"The timer on {keys.get('project_code')} has run since {started.astimezone(zone):%a %d %b %H:%M} "
                f"({as_text(hours)} hours). Log all of it on {day.isoformat()}?",
                choices=[f"Log {as_text(hours)} hours", "Don't log it"])
            if answer is None or not answer.startswith("Log"):
                return {"error": f"Not logged: the timer ran {as_text(hours)} hours, more than {LONG} hours. It is "
                                 f"still running; cancel it with timer_cancel and log the real time with entries_log."
                        }, "error"
        week = week_of(day)
        if await locked(call, week):
            return {"error": f"The week {week} is submitted; reopen it before logging the timer."}, "error"
        code = str(keys.get("project_code") or "")
        project = (await projects(call)).get(code)
        if project is None or project["fields"].get("status") != "active":
            return {"error": f"The timer's project {code} is no longer active; cancel the timer, or reactivate "
                             f"the project."}, "error"
        made = await entries(call, {"timer_ref": ref})
        day_total = sum((Decimal(e["hours"]) for e in await entries(call, {"date": day.isoformat()})), Decimal(0))
        if not made and day_total + hours > DAY_LIMIT:
            if not fresh:
                await call.records.update("timer", ref, {"status": "cancelled"})
            return {"error": f"That would make {day.isoformat()} {as_text(day_total + hours)} hours, more than a "
                             f"day holds; nothing was logged. Cancel the timer and log the time with entries_log."},                 "error"
        if fresh:
            await call.records.update("timer", ref, {"status": "stopped",
                                                     "stopped_at": stopped.isoformat(timespec="seconds")})
        if made:
            entry = made[0]
        else:
            fields = {"date": day.isoformat(), "week": week, "project_code": code, "hours": as_text(hours),
                      "source": "timer", "timer_ref": ref, "status": "draft"}
            note = str(inputs.get("note") or keys.get("note") or "").strip()
            if note:
                fields["note"] = note
            entry = entry_row(await call.records.create("entry", fields), fields)
            day_total += hours
        await call.records.update("timer", ref, {"entry_ref": entry["entry_ref"]})
        return {"timer_ref": ref, "entry": entry, "minutes": int(minutes), "day_total": as_text(day_total),
                "timezone": zone_name, **({"already": True} if made else {})}, "success"
