"""Applications: started on a form, driven one step at a time, reviewed,
submitted. The records are the truth — an application, one answer
record per step answered, one attachment record per document, the
document itself kept once it was accepted — and
every accepted answer is written at once, so a conversation that folds,
a person who comes back tomorrow, or a page that opens the records cold
all see the same application."""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from standard import ResourceDenied, ToolBase

from .documents import inspect, read_fields
from .forms import (TRUSTED, FormError, check_answer, is_required, load_forms,
                    public_step, run_checks, still_holds)

SKIP_WORDS = ("skip", "none", "n/a", "-")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class State:
    """One application as the functions see it: its record, its form,
    the answers so far (field -> value, None where skipped) and the
    documents so far (field -> what was kept)."""

    def __init__(self, ref: str, record: dict, form: dict,
                 answers: Dict[str, Any], answer_refs: Dict[str, str],
                 documents: Dict[str, dict], document_refs: Dict[str, str]):
        self.ref = ref
        self.keys = dict(record["fields"])
        self.form = form
        self.answers = answers
        self.answer_refs = answer_refs
        self.documents = documents
        self.document_refs = document_refs
        self.stale: Dict[str, str] = {}
        self.recheck()

    def recheck(self) -> List[str]:
        """Every rule that ties one answer or document to another, run
        again on the answers as they are now. A step whose rule no longer
        holds is open again: asked next, missing for submission, and shown
        with why. Returns the steps newly opened."""
        before = set(self.stale)
        self.stale = {}
        for step in self.form["steps"]:
            if step["field"] in self.answers or step["field"] in self.documents:
                problems = still_holds(step, self.answers, self.documents)
                if problems:
                    self.stale[step["field"]] = " ".join(problems)
        return [field for field in self.stale if field not in before]

    @property
    def status(self) -> str:
        return str(self.keys.get("status") or "draft")

    def pending(self) -> List[dict]:
        """Steps still to ask, in order. A step is done when answered
        (a skipped optional step counts) or when its document is in, and
        no later change to another answer broke its rules."""
        return [s for s in self.form["steps"]
                if s["field"] in self.stale
                or not (s["field"] in self.documents or s["field"] in self.answers)]

    def next(self) -> Dict[str, Any]:
        pending = self.pending()
        if not pending:
            return {"done": True, "remaining": 0}
        step = public_step(pending[0], len(pending))
        if pending[0]["field"] in self.stale:
            step["problem"] = self.stale[pending[0]["field"]]
        return {"done": False, "remaining": len(pending), "step": step}

    def progress(self) -> str:
        total = len(self.form["steps"])
        return f"{total - len(self.pending())} of {total}"

    def missing(self) -> List[str]:
        """What stops a submission: a required step with no answer, and any
        answer or document whose rules no longer hold."""
        return [s["field"] for s in self.form["steps"] if s["field"] in self.stale or (is_required(s) and (
            (s["kind"] == "file" and s["field"] not in self.documents)
            or (s["kind"] != "file" and self.answers.get(s["field"]) in (None, ""))))]

    def closed(self) -> Optional[dict]:
        if self.status != "draft":
            return {"error": f"This application is {self.status}; it cannot be changed.",
                    "kind": "closed"}
        return None


class ApplicationsTool(ToolBase):
    id = "applications"

    # -- reading the state --------------------------------------------------
    async def _load(self, call, ref: str) -> Tuple[Optional[State], Optional[dict]]:
        try:
            forms = load_forms()
        except FormError as exc:
            return None, {"error": f"A form this agent carries is broken: {exc}",
                          "kind": "form"}
        try:
            record = await call.records.read("application", ref)
        except ResourceDenied:
            return None, {"error": f"No application '{ref}'.", "kind": "not_found"}
        form = forms.get(str(record["fields"].get("form") or ""))
        if form is None:
            return None, {"error": "The application's form is no longer carried by "
                                   "this agent.", "kind": "form"}
        answers, answer_refs = {}, {}
        # The value of an answer, and what was read from a document, are
        # the person's data: kept in values, so each record is read whole.
        for row in await call.records.list("answer", {"application": ref}):
            keys = (await call.records.read("answer", row["ref"]))["fields"]
            field = str(keys.get("field") or "")
            answers[field] = None if keys.get("skipped") == "yes" else keys.get("value")
            answer_refs[field] = row["ref"]
        documents, document_refs = {}, {}
        for row in await call.records.list("attachment", {"application": ref}):
            keys = dict((await call.records.read("attachment", row["ref"]))["fields"])
            field = str(keys.get("field") or "")
            try:
                keys["read"] = json.loads(keys.get("read") or "{}")
            except json.JSONDecodeError:
                keys["read"] = {}
            documents[field] = {k: v for k, v in keys.items() if k not in ("application", "label")}
            document_refs[field] = row["ref"]
        return State(ref, record, form, answers, answer_refs, documents, document_refs), None

    # -- functions ----------------------------------------------------------
    async def list(self, call):
        wanted = str(call.inputs.get("status") or "")
        rows = await call.records.list("application", {"status": wanted} if wanted else None)
        return {"applications": [{
            "application_ref": r["ref"],
            **{k: r["fields"].get(k, "")
               for k in ("form", "title", "status", "progress", "reference")},
        } for r in rows]}, "success"

    async def start(self, call):
        try:
            forms = load_forms()
        except FormError as exc:
            return {"error": f"A form this agent carries is broken: {exc}",
                    "kind": "form"}, "error"
        form = forms.get(str(call.inputs.get("form") or "").strip())
        if form is None:
            return {"error": f"No form '{call.inputs.get('form')}'. Forms: "
                             + ", ".join(forms) + ".", "kind": "not_found"}, "error"
        total = len(form["steps"])
        ref = await call.records.create("application", {
            "form": form["id"], "title": form["title"], "status": "draft",
            "progress": f"0 of {total}", "reference": "", "submitted_at": "",
        })
        state = State(ref, {"fields": {"status": "draft"}}, form, {}, {}, {}, {})
        return {"application_ref": ref, "title": form["title"],
                "next": state.next()}, "success"

    async def next(self, call):
        state, refusal = await self._load(call, str(call.inputs["application_ref"]))
        if refusal:
            return refusal, "error"
        return state.next(), "success"

    async def answer(self, call):
        state, refusal = await self._load(call, str(call.inputs["application_ref"]))
        if refusal:
            return refusal, "error"
        if state.closed():
            return state.closed(), "error"
        step = next((s for s in state.form["steps"] if s["field"] == call.inputs["field"]), None)
        if step is None:
            return {"error": f"The form has no step '{call.inputs['field']}'.",
                    "kind": "not_found"}, "error"
        return await self._record_answer(call, state, step, call.inputs.get("value")), "success"

    async def attach(self, call):
        state, refusal = await self._load(call, str(call.inputs["application_ref"]))
        if refusal:
            return refusal, "error"
        if state.closed():
            return state.closed(), "error"
        step = next((s for s in state.form["steps"] if s["field"] == call.inputs["field"]), None)
        if step is None or step["kind"] != "file":
            return {"error": f"'{call.inputs['field']}' is not a document step of this form.",
                    "kind": "not_found"}, "error"
        return await self._record_document(call, state, step,
                                           str(call.inputs["file_ref"])), "success"

    async def step(self, call):
        """Ask the next step ourselves, then record exactly as answer or
        attach would. One question per call: a call is short-lived, and
        a restart between two questions loses nothing, because every
        accepted answer is already a record."""
        state, refusal = await self._load(call, str(call.inputs["application_ref"]))
        if refusal:
            return refusal, "error"
        if state.closed():
            return state.closed(), "error"
        pending = state.pending()
        if not pending:
            return {"done": True, "accepted": True, "problems": [], "remaining": 0}, "success"
        step = pending[0]
        question = self._question(step)
        if step["kind"] == "file":
            if not is_required(step):
                # A document card has no way to say "I have none": ask
                # that first, in words, and only then for the file.
                have = await call.ask(f"{question} Do you have it now?",
                                      choices=["Attach it", "Skip"])
                if have is None:
                    return self._declined(state, step), "success"
                if str(have).strip().lower() in ("skip", *SKIP_WORDS):
                    outcome = await self._record_answer(call, state, step, "")
                    following = outcome.get("next") or {}
                    return {"done": bool(following.get("done")), "field": step["field"],
                            "accepted": True, "problems": [],
                            "remaining": int(following.get("remaining") or 0),
                            "next": following}, "success"
            answer = await call.ask(question, expects="file")
        else:
            choices = list(step["choices"]) if step["kind"] == "choice" else []
            if choices and not is_required(step):
                choices.append("Skip")
            answer = await call.ask(question, choices=choices or None)
        if answer is None:
            return self._declined(state, step), "success"
        answer = str(answer).strip()
        if step["kind"] == "file":
            outcome = await self._record_document(call, state, step, answer)
        else:
            skipped = not is_required(step) and answer.lower() in SKIP_WORDS
            outcome = await self._record_answer(call, state, step, "" if skipped else answer)
        following = outcome.get("next") or {}
        return {"done": bool(following.get("done")), "field": step["field"],
                "accepted": outcome["accepted"], "problems": outcome["problems"],
                **({"reopened": outcome["reopened"]} if outcome.get("reopened") else {}),
                "remaining": int(following.get("remaining") or 0),
                "next": following}, "success"

    @staticmethod
    def _declined(state: State, step: dict) -> dict:
        """The card was answered with no: the person chose not to answer.
        Said as that, so nobody asks again as if no one were there."""
        following = state.next()
        return {"done": False, "field": step["field"], "accepted": False, "declined": True,
                "problems": [f"You declined to answer: {step['label']} Nothing was recorded."],
                "remaining": int(following.get("remaining") or 0), "next": following}

    async def review(self, call):
        state, refusal = await self._load(call, str(call.inputs["application_ref"]))
        if refusal:
            return refusal, "error"
        answers = [{"field": s["field"], "label": s["label"],
                    "value": state.answers.get(s["field"]),
                    "skipped": s["field"] in state.answers
                    and state.answers[s["field"]] in (None, ""),
                    **({"problem": state.stale[s["field"]]} if s["field"] in state.stale else {})}
                   for s in state.form["steps"] if s["kind"] != "file"]
        documents = [{"field": s["field"], "label": s["label"],
                      **(state.documents.get(s["field"]) or {"attached": False}),
                      **({"problem": state.stale[s["field"]]} if s["field"] in state.stale else {})}
                     for s in state.form["steps"] if s["kind"] == "file"]
        missing = state.missing()
        return {"ready": not missing and state.status == "draft",
                "status": state.status, "title": state.keys.get("title", ""),
                "answers": answers, "documents": documents, "missing": missing,
                "reference": state.keys.get("reference", "")}, "success"

    async def submit(self, call):
        state, refusal = await self._load(call, str(call.inputs["application_ref"]))
        if refusal:
            return refusal, "error"
        if state.closed():
            return state.closed(), "error"
        missing = state.missing()
        if missing:
            return {"error": "Not ready to submit; still missing: " + ", ".join(missing) + ".",
                    "kind": "incomplete", "missing": missing}, "error"
        stamp = now_iso()
        reference = f"APP-{stamp[:10].replace('-', '')}-{secrets.token_hex(3).upper()}"
        await call.records.update("application", state.ref, {
            "status": "submitted", "reference": reference, "submitted_at": stamp})
        # Recorded here, and sent nowhere: an example has no one to send it to.
        return {"reference": reference, "submitted_at": stamp, "sent_to": "nobody: recorded here only"}, "success"

    async def withdraw(self, call):
        state, refusal = await self._load(call, str(call.inputs["application_ref"]))
        if refusal:
            return refusal, "error"
        if state.closed():
            return state.closed(), "error"
        await call.records.update("application", state.ref, {"status": "withdrawn"})
        return {"status": "withdrawn"}, "success"

    # -- recording --------------------------------------------------------
    @staticmethod
    def _question(step: dict) -> str:
        text = str(step["label"]).strip()
        if step.get("help"):
            text += f" {str(step['help']).strip()}"
        if step["kind"] == "date":
            text += " (YYYY-MM-DD)"
        if step["kind"] == "file":
            names = {"application/pdf": "a PDF", "image/png": "a PNG photo",
                     "image/jpeg": "a JPEG photo"}
            text += " — attach " + " or ".join(names.get(a, a) for a in step["accept"]) + "."
        if not is_required(step) and step["kind"] != "choice":
            text += " Say skip to leave it out."
        return text

    async def _record_answer(self, call, state: State, step: dict, value) -> dict:
        stored, problems = check_answer(step, value, state.answers)
        if problems:
            return {"accepted": False, "problems": problems, "next": state.next()}
        fields = {"application": state.ref, "field": step["field"], "label": step["label"],
                  "value": "" if stored is None else str(stored),
                  "skipped": "yes" if stored is None else "no"}
        existing = state.answer_refs.get(step["field"])
        if existing:
            await call.records.update("answer", existing, fields)
        else:
            state.answer_refs[step["field"]] = await call.records.create("answer", fields)
        state.answers[step["field"]] = stored
        reopened = state.recheck()
        await call.records.update("application", state.ref,
                                         {"progress": state.progress()})
        return {"accepted": True, "problems": [], "value": fields["value"],
                **({"reopened": [f"{field}: {state.stale[field]}" for field in reopened]} if reopened else {}),
                "next": state.next()}

    @staticmethod
    async def _confirmed(call, step: dict, read: Dict[str, Dict[str, Any]]) -> List[str]:
        """A value a rule decides on that the page could not confirm (read
        from a photo, or written in a way the text check doesn't take) is
        put to the person: yes makes it confirmed, by them; no leaves it
        assumed, and the rule refuses it."""
        for check in step.get("checks") or []:
            field = str(check.get("read_field") or "")
            found = read.get(field) or {}
            if not found.get("value") or found.get("status") in TRUSTED:
                continue
            what = (step.get("read") or {}).get(field) or field
            answer = await call.ask(
                f"The document was read as giving {what} as {found['value']}, and that could not be checked "
                f"against its text. Is {found['value']} what the document says?",
                choices=["Yes, that is right", "No"])
            if answer is None:
                return [f"You declined to confirm {what}; the document was not recorded."]
            if str(answer).strip().lower().startswith("yes"):
                found["status"] = "confirmed"
        return []

    async def _record_document(self, call, state: State, step: dict, given: str) -> dict:
        """A file handed to the call (or a kept document, by its ref),
        checked; once accepted, kept as a document of the application."""
        try:
            path = await call.file(given, "document")
        except ResourceDenied as exc:
            return {"accepted": False, "problems": [f"The file could not be read: {exc}"],
                    "next": state.next()}
        # pypdf parses the whole file: off the agent's loop, so the chat's
        # other calls go on meanwhile.
        document, problems = await asyncio.to_thread(inspect, path, step)
        if problems:
            return {"accepted": False, "problems": problems, "next": state.next()}
        read, problems = await read_fields(call, step, document)
        if not problems:
            problems = await self._confirmed(call, step, read)
        if not problems:
            problems = run_checks(step, read, state.answers)
        shown = {"attached": True, "filename": document.filename,
                 "type": document.mime, "size": document.size,
                 **({"pages": document.pages} if document.pages is not None else {}),
                 "read": read}
        if problems:
            return {"accepted": False, "problems": problems, "document": shown,
                    "next": state.next()}
        file_ref = await call.records.create_file("document", document.filename, document.raw)
        shown = {**shown, "file_ref": file_ref}
        fields = {"application": state.ref, "field": step["field"], "label": step["label"],
                  "file_ref": file_ref, "filename": document.filename,
                  "type": document.mime, "size": document.size,
                  **({"pages": document.pages} if document.pages is not None else {}),
                  "read": json.dumps(read, ensure_ascii=False)}
        existing = state.document_refs.get(step["field"])
        if existing:
            await call.records.update("attachment", existing, fields)
        else:
            state.document_refs[step["field"]] = await call.records.create("attachment", fields)
        state.documents[step["field"]] = shown
        state.recheck()
        await call.records.update("application", state.ref,
                                         {"progress": state.progress()})
        return {"accepted": True, "problems": [], "document": shown, "next": state.next()}
