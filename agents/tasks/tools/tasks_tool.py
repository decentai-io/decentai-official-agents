import asyncio

from standard import ToolBase

from .clock import day_or_today
from .dates import parse_iso
from .repeat import RULES, first_after

PRIORITIES = ("high", "normal", "low")
RANK = {p: n for n, p in enumerate(PRIORITIES)}
SHOWN = ("task_ref", "title", "owner", "due", "status", "priority", "repeat", "blocker", "source")


def _due(text: str):
    """A due date only when it is a real ISO date; anything else is an
    error to the caller, never a guess."""
    text = str(text or "").strip()
    if not text:
        return "", None
    found = parse_iso(text)
    if found is None or found.isoformat() != text:
        return "", f"due must be a date as YYYY-MM-DD, not '{text}'."
    return text, None


def _row(record):
    keys = record.get("fields") or {}
    return {
        "task_ref": str(record.get("ref") or ""),
        "title": str(keys.get("title") or ""),
        "owner": str(keys.get("owner") or ""),
        "due": str(keys.get("due") or ""),
        "status": str(keys.get("status") or "open"),
        "priority": str(keys.get("priority") or "normal"),
        "repeat": str(keys.get("repeat") or "none"),
        "repeat_day": int(float(keys["repeat_day"])) if keys.get("repeat_day") not in (None, "") else None,
        "blocker": str(keys.get("blocker") or ""),
        "source": str(keys.get("source") or ""),
        "evidence": str(keys.get("evidence") or ""),
        "note": str(keys.get("note") or ""),
        "next_ref": str(keys.get("next_ref") or ""),
    }


def _order(row):
    return (row["due"] or "9999-99-99", RANK.get(row["priority"], 1), row["title"], row["task_ref"])


class TasksTool(ToolBase):
    id = "tasks"

    def __init__(self, agent):
        super().__init__(agent)
        #: One change at a time per task in this chat, so two overlapping completions make one next task.
        self._locks: dict[str, asyncio.Lock] = {}

    async def create(self, call):
        due, problem = _due(call.inputs.get("due"))
        if problem:
            return {"error": problem}, "error"
        repeat = str(call.inputs.get("repeat") or "none")
        if repeat in RULES and not due:
            return {"error": f"A task that repeats {repeat} needs a due date (YYYY-MM-DD) to count from."}, "error"
        owner = str(call.inputs.get("owner") or "").strip()
        title = str(call.inputs["title"]).strip()
        fields = {"title": title, "owner": owner, "due": due, "status": "open",
                  "priority": str(call.inputs.get("priority") or "normal"), "repeat": repeat,
                  "source": str(call.inputs.get("source") or ""), "note": str(call.inputs.get("note") or "")}
        if repeat in ("monthly", "yearly"):
            fields["repeat_day"] = parse_iso(due).day
        task_ref = await call.records.create("task", fields)
        return {"task_ref": task_ref, "title": title, "owner": owner, "due": due, "status": "open",
                "priority": fields["priority"], "repeat": repeat, "unassigned": not owner}, "success"

    async def update(self, call):
        ref = str(call.inputs["task_ref"])
        lock = self._locks.setdefault(ref, asyncio.Lock())
        async with lock:
            return await self._update(call, ref)

    async def _update(self, call, ref):
        current = _row(await call.records.read("task", ref))
        changes = {}
        status = str(call.inputs.get("status") or "")
        if "due" in call.inputs:
            due, problem = _due(call.inputs.get("due"))
            if problem:
                return {"error": problem}, "error"
            changes["due"] = due
        if "owner" in call.inputs:
            changes["owner"] = str(call.inputs.get("owner") or "").strip()
        for name in ("note", "evidence", "priority"):
            if call.inputs.get(name):
                changes[name] = str(call.inputs[name])
        if call.inputs.get("repeat"):
            changes["repeat"] = str(call.inputs["repeat"])
        repeat = changes.get("repeat", current["repeat"])
        due_after = changes.get("due", current["due"])
        if repeat in RULES and not due_after:
            return {"error": f"A task that repeats {repeat} needs a due date (YYYY-MM-DD) to count from."}, "error"
        if repeat in ("monthly", "yearly") and ("due" in changes or current["repeat"] != repeat):
            changes["repeat_day"] = parse_iso(due_after).day
        made_next = {}
        if status:
            if status == "done" and not (call.inputs.get("evidence") or current["evidence"]):
                return {"error": "done needs evidence: what shows it was done "
                                 "(a storage_ref, a file_ref, a message id, "
                                 "or a sentence from the person)."}, "error"
            if status == "blocked" and not (call.inputs.get("blocker") or current["blocker"]):
                return {"error": "blocked needs a blocker: say what stops it."}, "error"
            changes["status"] = status
            if status == "done":
                done_on, zone, problem = day_or_today(call.inputs, "done_on")
                if problem:
                    return {"error": problem}, "error"
                changes["done_on"] = done_on.isoformat()
                if repeat in RULES and current["status"] != "done":
                    made_next = await self._next(call, current, {**current, **changes}, done_on)
                    changes["next_ref"] = made_next["next_ref"]
            if status != "blocked":
                changes["blocker"] = ""
        if call.inputs.get("blocker"):
            changes["blocker"] = str(call.inputs["blocker"])
        if not changes:
            return {"error": "Nothing to change."}, "error"
        await call.records.update("task", ref, changes)
        after = _row(await call.records.read("task", ref))
        return {**{k: after[k] for k in ("task_ref", "title", "owner", "due", "status", "priority", "repeat",
                                         "blocker", "evidence")}, **made_next}, "success"

    async def _next(self, call, current, merged, done_on):
        """The next occurrence of a repeating task, made once: a retry, or a second completion, finds the one made
        before (it names this task as its previous) instead of making another."""
        for record in await call.records.list("task", {"previous_ref": current["task_ref"]}):
            found = _row(record)
            return {"next_ref": found["task_ref"], "next_due": found["due"]}
        due = parse_iso(merged["due"])
        rule, day = merged["repeat"], merged.get("repeat_day")
        when = first_after(due, rule, day, max(due, done_on))
        fields = {"title": merged["title"], "owner": merged["owner"], "due": when.isoformat(), "status": "open",
                  "priority": merged["priority"], "repeat": rule, "source": merged["source"],
                  "note": merged["note"], "previous_ref": current["task_ref"]}
        if day:
            fields["repeat_day"] = day
        next_ref = await call.records.create("task", fields)
        return {"next_ref": next_ref, "next_due": when.isoformat()}

    async def list(self, call):
        filters = {}
        for name in ("owner", "status", "source", "priority"):
            if call.inputs.get(name):
                filters[name] = str(call.inputs[name])
        rows = sorted((_row(r) for r in await call.records.list("task", filters)), key=_order)
        return {"tasks": [{k: r[k] for k in SHOWN} for r in rows], "total": len(rows)}, "success"

    async def overdue(self, call):
        as_of, zone, problem = day_or_today(call.inputs, "as_of")
        if problem:
            return {"error": problem}, "error"
        within = call.inputs.get("within_days")
        within = 5 if within is None else int(within)
        filters = {"owner": str(call.inputs["owner"])} if call.inputs.get("owner") else {}
        rows = [_row(r) for r in await call.records.list("task", filters)]

        overdue, due_soon, blocked, unassigned, undated = [], [], [], [], []
        for row in sorted(rows, key=_order):
            if row["status"] == "blocked":
                blocked.append({k: row[k] for k in ("task_ref", "title", "owner", "priority", "blocker")})
                continue
            if row["status"] != "open":
                continue
            if not row["owner"]:
                unassigned.append({k: row[k] for k in ("task_ref", "title", "due", "priority")})
            due = parse_iso(row["due"]) if row["due"] else None
            if due is None:
                undated.append({k: row[k] for k in ("task_ref", "title", "owner", "priority")})
                continue
            delta = (due - as_of).days
            shown = {k: row[k] for k in ("task_ref", "title", "owner", "due", "priority")}
            if delta < 0:
                overdue.append({**shown, "days_late": -delta})
            elif delta <= within:
                due_soon.append({**shown, "days_left": delta})
        return {"as_of": as_of.isoformat(), **({"timezone": zone} if zone else {}), "within_days": within,
                "overdue": overdue, "due_soon": due_soon, "blocked": blocked, "unassigned": unassigned,
                "undated": undated}, "success"
