"""Proposals out of notes. The model reads; the code checks — every
quote against the notes verbatim, every owner against the names the
notes actually contain (whole names, never a part of one), every date
through the deterministic parser. What fails a check is not dropped
silently: the quote is marked unverified (and kept so), the owner is
cleared, the date stays as written. A date read rather than stated
("Thursday", "11 September") is said with what it was read as.

Confirming is safe to repeat: the proposal is marked "confirming"
before its task is made, the task names the proposal it came from, and
a second confirm (a retry after a lost answer) finds that task instead
of making another."""

import asyncio
import json
import re

from standard import ResourceDenied, ToolBase

from .dates import parse_iso, reading, resolve

PROPOSE_SYSTEM = (
    "You read meeting notes and list the action items in them. Answer ONLY "
    "with a JSON object {\"items\": [...]}. Each item: {\"title\": <short "
    "imperative phrase>, \"kind\": \"commitment\" when a named person said "
    "they will do it, else \"suggestion\", \"owner\": <the person's name "
    "exactly as written, or \"\" when the notes name nobody>, \"due\": <the "
    "deadline exactly as written, or \"\">, \"quote\": <the exact passage, "
    "copied verbatim, that states it>}. Never invent an owner or a date; "
    "never merge two actions into one."
)


class ExtractTool(ToolBase):
    id = "extract"

    def __init__(self, agent):
        super().__init__(agent)
        #: One confirm at a time per proposal in this chat, so two overlapping confirms make one task.
        self._locks: dict[str, asyncio.Lock] = {}

    async def propose(self, call):
        notes = str(call.inputs["notes"])
        source = str(call.inputs.get("source") or "")
        anchor = parse_iso(str(call.inputs.get("meeting_date") or ""))
        people = [str(p).strip() for p in call.inputs.get("people") or [] if str(p).strip()]
        prompt = (f"MEETING DATE: {anchor.isoformat() if anchor else 'unknown'}\n"
                  + (f"PEOPLE PRESENT: {', '.join(people)}\n" if people else "")
                  + f"\nNOTES:\n{notes}\n\nList the action items as JSON.")
        await call.progress("Reading the notes for action items")
        try:
            answer = await call.model(prompt, system=PROPOSE_SYSTEM)
        except Exception as exc:
            return {"error": f"The model could not be asked: {exc}"}, "error"
        parsed = self._json(answer)
        items = parsed.get("items") if isinstance(parsed, dict) else None
        if not isinstance(items, list):
            return {"error": "The model's answer was not a JSON object with "
                             "an items list; try again."}, "error"

        flat = " ".join(notes.split()).lower()
        proposals, readings, commitments, suggestions, unassigned = [], [], 0, 0, 0
        for item in items[:50]:
            if not isinstance(item, dict) or not str(item.get("title") or "").strip():
                continue
            title = str(item["title"]).strip()[:200]
            kind = "commitment" if str(item.get("kind") or "").lower() == "commitment" else "suggestion"
            quote = " ".join(str(item.get("quote") or "").split())
            verified = len(quote) >= 8 and quote.lower() in flat
            owner = str(item.get("owner") or "").strip()
            if owner and not self._named(owner, notes, people):
                owner = ""                      # a name the notes never wrote
            due_text = str(item.get("due") or "").strip()[:80]
            due_date, how = resolve(due_text, anchor)
            due = due_date.isoformat() if due_date else ""
            said = reading(due_text, due_date, how, anchor)
            if kind == "commitment":
                commitments += 1
            else:
                suggestions += 1
            if not owner:
                unassigned += 1
            extraction_ref = await call.records.create("extraction", {
                "title": title, "kind": kind, "owner": owner, "due": due,
                "due_text": due_text, "due_reading": said, "quote": quote[:300], "source": source,
                "verified": "yes" if verified else "no", "status": "proposed",
            })
            proposals.append({"extraction_ref": extraction_ref,
                              "title": title, "kind": kind, "owner": owner,
                              "due": due, "due_text": due_text,
                              **({"due_reading": said} if said else {}),
                              "quote": quote[:300], "verified": verified})
            if said:
                readings.append(f"{title}: {said}")
        return {"proposals": proposals, "commitments": commitments,
                "suggestions": suggestions, "unassigned": unassigned,
                "unverified": sum(not p["verified"] for p in proposals),
                "date_readings": readings}, "success"

    async def confirm(self, call):
        tasks, unassigned, skipped = [], [], []
        for item in call.inputs["items"]:
            ref = str(item["extraction_ref"])
            lock = self._locks.setdefault(ref, asyncio.Lock())
            async with lock:
                outcome = await self._confirm_one(call, ref, item)
            if "reason" in outcome:
                skipped.append({"extraction_ref": ref, "reason": outcome["reason"]})
                continue
            tasks.append(outcome)
            if not outcome["owner"]:
                unassigned.append(outcome["task_ref"])
        return {"tasks": tasks, "unassigned": unassigned, "skipped": skipped}, "success"

    async def _confirm_one(self, call, ref, item):
        """One proposal made a task, once. The proposal is marked "confirming" before the task is made, and the task
        names the proposal it came from; a confirm that finds a proposal confirming or confirmed finds its task and
        hands it back instead of making a second."""
        try:
            record = await call.records.read("extraction", ref)
        except ResourceDenied:
            return {"reason": "no such proposal"}
        keys = record.get("fields") or {}
        status = keys.get("status")
        if status in ("confirming", "confirmed"):
            found = await self._made_from(call, ref, keys)
            if found is not None:
                if status != "confirmed" or keys.get("task_ref") != found["task_ref"]:
                    await call.records.update("extraction", ref, {"status": "confirmed",
                                                                 "task_ref": found["task_ref"]})
                return {**found, "extraction_ref": ref, "already": True}
            if status == "confirmed":
                return {"reason": "already confirmed"}
        elif status != "proposed":
            return {"reason": f"already {status}"}
        if keys.get("verified") != "yes" and not item.get("accept_unverified"):
            return {"reason": "unverified: its quote is not in the notes word for word. Show the person the quote, "
                              "and confirm it again with accept_unverified only if they agree it was said"}
        owner = str(item.get("owner") or keys.get("owner") or "").strip()
        due = str(item.get("due") or keys.get("due") or "").strip()
        if due and (parse_iso(due) is None or parse_iso(due).isoformat() != due):
            return {"reason": f"due must be YYYY-MM-DD, not '{due}'"}
        title = str(keys.get("title") or "")
        if status != "confirming":
            await call.records.update("extraction", ref, {"status": "confirming", "owner": owner, "due": due})
        task_ref = await call.records.create("task", {
            "title": title, "owner": owner, "due": due, "status": "open", "priority": "normal", "repeat": "none",
            "source": str(keys.get("source") or ""), "extraction_ref": ref,
            "note": f"From notes: \"{keys.get('quote') or ''}\""[:500],
        })
        await call.records.update("extraction", ref, {"status": "confirmed", "task_ref": task_ref})
        return {"task_ref": task_ref, "extraction_ref": ref, "title": title, "owner": owner, "due": due,
                **({"due_reading": keys["due_reading"]} if keys.get("due_reading") and due == keys.get("due")
                   else {})}

    @staticmethod
    async def _made_from(call, ref, keys):
        """The task a proposal already became: one naming it, or the one it names."""
        found = await call.records.list("task", {"extraction_ref": ref})
        if not found and keys.get("task_ref"):
            found = [r for r in await call.records.list("task") if r["ref"] == keys["task_ref"]]
        if not found:
            return None
        task = found[0].get("fields") or {}
        return {"task_ref": found[0]["ref"], "title": str(task.get("title") or ""),
                "owner": str(task.get("owner") or ""), "due": str(task.get("due") or "")}

    async def reject(self, call):
        ref = str(call.inputs["extraction_ref"])
        record = await call.records.read("extraction", ref)
        if (record.get("fields") or {}).get("status") in ("confirmed", "confirming"):
            return {"error": "That proposal was already confirmed into a task; "
                             "cancel the task instead."}, "error"
        await call.records.update("extraction", ref, {"status": "rejected"})
        return {"rejected": True}, "success"

    @staticmethod
    def _named(owner: str, notes: str, people) -> bool:
        """Whether the notes, or the people present, name this owner as a whole name: "Al" is not "Alice", though
        "Dana" is a name of "Dana Haddad"."""
        words = owner.split()
        if not words:
            return False
        pattern = re.compile(r"(?<!\w)" + r"\s+".join(re.escape(w) for w in words) + r"(?!\w)", re.IGNORECASE)
        return any(pattern.search(p) for p in people) or bool(pattern.search(notes))

    @staticmethod
    def _json(answer: str):
        text = str(answer or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return None
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None
