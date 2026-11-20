"""A Google Form: what it asks, its responses page by page, a tally per question counted here, and a watch.

A response carries its answers by question id; the titles a person recognises come from the form. Google promises
no order of responses: a page is put newest first, and the page token is Google's own, so a page is read once.

A responses watch remembers the submit time of the newest response handed on (Google's clock, UTC) and the ids on
exactly that time, so two in the same instant are neither lost nor shown twice. A check asks Google only for responses
at or after that time. A form that is gone closes its watch.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal

from standard import ToolBase

from .common import HttpError, connect, failed, get_item, no_account, refused, segment
from .sheets_model import plain
from .watching import Seen, run

ANSWER_MOST = 500
TALLY_MOST = 50_000
OPTIONS_SHOWN = 50


def moment(stamp: str) -> str:
    """An RFC 3339 UTC time that sorts as text: Google writes anywhere from no fraction to nanoseconds."""
    match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z$", str(stamp or ""))
    return f"{match.group(1)}.{(match.group(2) or '').ljust(9, '0')}Z" if match else ""


def utc(text: str) -> str:
    """An ISO 8601 date-time with a zone as RFC 3339 UTC, the only form the Forms filter takes; "" when not one."""
    try:
        parsed = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        return ""
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Questions:
    """A form's questions by id: the title a person sees, the kind, the options. A grid is one question per row."""

    KINDS = {"RADIO": "multiple_choice", "CHECKBOX": "checkboxes", "DROP_DOWN": "dropdown"}

    def __init__(self, form: dict):
        self.rows: list[dict] = []
        for item in form.get("items") or []:
            title = str(item.get("title") or "").strip()
            if "questionItem" in item:
                self._add((item["questionItem"] or {}).get("question") or {}, title)
            elif "questionGroupItem" in item:
                group = item["questionGroupItem"] or {}
                options = [str(o.get("value") or "") for o in ((group.get("grid") or {}).get("columns") or {})
                           .get("options") or []]
                for question in group.get("questions") or []:
                    row = str(((question.get("rowQuestion") or {}).get("title")) or "").strip()
                    self._add(question, f"{title} — {row}" if row else title, "grid", options)
        self.titles = self._unique()

    def _add(self, question, title, kind="", options=None):
        other = False
        if not kind:
            kind, options, other = self._kind(question)
        self.rows.append({"question_id": str(question.get("questionId") or ""), "title": title, "kind": kind,
                          "options": options or [], "required": bool(question.get("required")), "other": other})

    @classmethod
    def _kind(cls, question):
        if "choiceQuestion" in question:
            choice = question["choiceQuestion"] or {}
            options = [str(o.get("value") or "") for o in choice.get("options") or [] if not o.get("isOther")]
            other = any(o.get("isOther") for o in choice.get("options") or [])
            return cls.KINDS.get(str(choice.get("type") or ""), "choice"), options, other
        if "textQuestion" in question:
            return ("paragraph" if (question["textQuestion"] or {}).get("paragraph") else "short_text"), [], False
        if "scaleQuestion" in question:
            scale = question["scaleQuestion"] or {}
            return "scale", [str(n) for n in range(int(scale.get("low") or 0), int(scale.get("high") or 0) + 1)], \
                False
        if "ratingQuestion" in question:
            most = int((question["ratingQuestion"] or {}).get("ratingScaleLevel") or 0)
            return "rating", [str(n) for n in range(1, most + 1)], False
        for key, kind in (("dateQuestion", "date"), ("timeQuestion", "time"), ("fileUploadQuestion", "file_upload")):
            if key in question:
                return kind, [], False
        return "other", [], False

    def _unique(self):
        titles, seen = {}, {}
        for row in self.rows:
            title = row["title"] or "Untitled question"
            seen[title] = seen.get(title, 0) + 1
            titles[row["question_id"]] = f"{title} ({seen[title]})" if seen[title] > 1 else title
        return titles


def values_of(answer: dict) -> list[str]:
    texts = [str(a.get("value") or "") for a in (answer.get("textAnswers") or {}).get("answers") or []]
    files = [f"file: {f.get('fileName') or f.get('fileId') or 'unnamed'}"
             for f in (answer.get("fileUploadAnswers") or {}).get("answers") or []]
    return texts + files


def answer_text(answer: dict) -> str:
    text = ", ".join(values_of(answer))
    return text if len(text) <= ANSWER_MOST else text[:ANSWER_MOST] + "…"


def response_row(response: dict, questions: Questions) -> dict:
    answers, shaped = response.get("answers") or {}, {}
    for question in questions.rows:
        if answers.get(question["question_id"]):
            shaped[questions.titles[question["question_id"]]] = answer_text(answers[question["question_id"]])
    for question_id, answer in answers.items():
        if question_id not in questions.titles:
            shaped[f"question {question_id}"] = answer_text(answer)
    row = {"response_id": str(response.get("responseId") or ""),
           "submitted": str(response.get("lastSubmittedTime") or ""), "answers": shaped}
    if response.get("respondentEmail"):
        row["email"] = str(response["respondentEmail"])
    return row


def title_of(form: dict) -> str:
    info = form.get("info") or {}
    return str(info.get("title") or info.get("documentTitle") or "")


def newest_first(responses: list[dict]) -> list[dict]:
    return sorted(responses, key=lambda r: (moment(r.get("lastSubmittedTime")), str(r.get("responseId") or "")),
                  reverse=True)


class FormsTool(ToolBase):
    id = "forms"

    async def get(self, call):
        clients, why = await connect(call, ask=False)
        if clients is None:
            return no_account(why)
        try:
            form = await clients.forms.get(f"forms/{segment(str(call.inputs['form_id']))}")
        except HttpError as error:
            return failed(error)
        info, questions = form.get("info") or {}, Questions(form)
        rows = [{k: v for k, v in {**q, "title": questions.titles[q["question_id"]],
                                   "options": q["options"][:OPTIONS_SHOWN]}.items() if k != "other"}
                for q in questions.rows]
        result = {"form_id": str(form.get("formId") or ""), "title": title_of(form),
                  "description": str(info.get("description") or "")[:1000],
                  "responder_link": str(form.get("responderUri") or ""), "question_count": len(rows),
                  "questions": rows[:100]}
        if form.get("linkedSheetId"):
            result["linked_spreadsheet_id"] = str(form["linkedSheetId"])
        return result, "success"


class ResponsesTool(ToolBase):
    id = "responses"

    @staticmethod
    def _since(inputs):
        if not inputs.get("since"):
            return "", None
        since = utc(inputs["since"])
        if not since:
            return "", refused(f"since is an ISO 8601 date-time with a time zone, not {inputs['since']!r}.")
        return since, None

    async def list(self, call):
        clients, why = await connect(call, ask=False)
        if clients is None:
            return no_account(why)
        inputs = call.inputs
        since, problem = self._since(inputs)
        if problem:
            return problem
        form_id = str(inputs["form_id"])
        params = {"pageSize": int(inputs.get("max_results") or 25)}
        if since:
            params["filter"] = f"timestamp >= {since}"
        if inputs.get("page_token"):
            params["pageToken"] = str(inputs["page_token"])
        try:
            form = await clients.forms.get(f"forms/{segment(form_id)}")
            answer = await clients.forms.get(f"forms/{segment(form_id)}/responses", params=params)
        except HttpError as error:
            return failed(error)
        questions = Questions(form)
        result = {"form": title_of(form),
                  "responses": [response_row(r, questions) for r in newest_first(answer.get("responses") or [])]}
        if answer.get("nextPageToken"):
            result["next_page_token"] = str(answer["nextPageToken"])
        return result, "success"

    async def tally(self, call):
        """Every response counted here, per question: each option's count, a scale's average; never the model's."""
        clients, why = await connect(call, ask=False)
        if clients is None:
            return no_account(why)
        since, problem = self._since(call.inputs)
        if problem:
            return problem
        form_id = str(call.inputs["form_id"])
        params = {"pageSize": 5000, **({"filter": f"timestamp >= {since}"} if since else {})}
        try:
            form = await clients.forms.get(f"forms/{segment(form_id)}")
            responses = await clients.forms.token_pages(f"forms/{segment(form_id)}/responses", items="responses",
                                                        params=params, most=TALLY_MOST + 1)
        except HttpError as error:
            return failed(error)
        cut_short = len(responses) > TALLY_MOST
        responses = responses[:TALLY_MOST]
        questions = Questions(form)
        tallies = []
        for question in questions.rows:
            counts = {option: 0 for option in question["options"]}
            answered, total, numbers = 0, Decimal(0), 0
            for response in responses:
                answer = (response.get("answers") or {}).get(question["question_id"])
                if not answer:
                    continue
                answered += 1
                for value in values_of(answer):
                    if question["kind"] in ("scale", "rating") and re.fullmatch(r"-?\d+", value):
                        total += Decimal(value)
                        numbers += 1
                    if question["options"] or question["kind"] in ("multiple_choice", "checkboxes", "dropdown"):
                        key = value if value in counts or not question["other"] else "Other"
                        counts[key] = counts.get(key, 0) + 1
            row = {"question": questions.titles[question["question_id"]], "kind": question["kind"],
                   "answered": answered}
            if counts:
                row["counts"] = [{"option": k, "count": v} for k, v in
                                 sorted(counts.items(), key=lambda kv: (-kv[1], list(counts).index(kv[0])))
                                 ][:OPTIONS_SHOWN]
            if numbers:
                row["average"] = plain((total / numbers).quantize(Decimal("0.01")))
            tallies.append(row)
        return {"form": title_of(form), "responses": len(responses), "cut_short": cut_short,
                **({"since": since} if since else {}), "questions": tallies}, "success"

    async def watch(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        form_id = str(call.inputs["form_id"])
        try:
            form = await clients.forms.get(f"forms/{segment(form_id)}")
            found = await clients.forms.token_pages(f"forms/{segment(form_id)}/responses", items="responses",
                                                    params={"pageSize": 5000})
            stamped = [r for r in found if moment(r.get("lastSubmittedTime"))]
            if stamped:
                since = max((str(r["lastSubmittedTime"]) for r in stamped), key=moment)
            else:
                # No response yet: the form's last change, in Google's clock, comes before any response to come.
                since = str((await get_item(clients.drive, form_id, "id,modifiedTime")).get("modifiedTime") or "")
        except HttpError as error:
            return failed(error)
        ids = sorted(str(r.get("responseId") or "") for r in stamped if moment(r["lastSubmittedTime"]) == moment(since))
        label = title_of(form)
        watch_ref = await call.records.create("watch", {
            "kind": "responses", "status": "active", "account": clients.address, "target_id": form_id,
            "label": label, "cursor": since, "cursor_ids": ",".join(ids), "note": str(call.inputs.get("note") or "")})
        return {"watch_ref": watch_ref, "label": label, "since": since, "responses_so_far": len(found)}, "success"

    async def new(self, call):
        return await run(call, "responses", self._read), "success"

    @staticmethod
    async def _read(clients, watch, most):
        fields = watch["fields"]
        form_id, since = str(fields.get("target_id") or ""), str(fields.get("cursor") or "")
        seen = {i for i in str(fields.get("cursor_ids") or "").split(",") if i}
        form = await clients.forms.get(f"forms/{segment(form_id)}")
        filtered = utc(since)
        found = await clients.forms.token_pages(f"forms/{segment(form_id)}/responses", items="responses", params={
            "pageSize": 5000, **({"filter": f"timestamp >= {filtered}"} if filtered else {})})
        # Oldest first. One handed on sits exactly on the cursor; one edited since has a later time and is news.
        fresh = sorted((r for r in found if moment(r.get("lastSubmittedTime")) > moment(since) or (
            moment(r.get("lastSubmittedTime")) == moment(since) and str(r.get("responseId") or "") not in seen)),
            key=lambda r: (moment(r.get("lastSubmittedTime")), str(r.get("responseId") or "")))
        more = len(fresh) > most
        fresh = fresh[:most]
        questions = Questions(form)
        rows = [{**response_row(r, questions), "watch_ref": watch["ref"], "form": title_of(form)} for r in fresh]
        if not fresh:
            return Seen(rows)
        newest = str(fresh[-1].get("lastSubmittedTime") or "")
        at_newest = {str(r.get("responseId") or "") for r in fresh if moment(r.get("lastSubmittedTime")) ==
                     moment(newest)} | (seen if moment(newest) == moment(since) else set())
        return Seen(rows, {"cursor": newest, "cursor_ids": ",".join(sorted(at_newest))}, more=more)
