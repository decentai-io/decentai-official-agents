"""Comments on a Drive file (a Doc, a Sheet, a PDF): read freely, written only on the person's go-ahead, watched.

Comments live in Drive (``files/{id}/comments``), which answers with nothing useful unless told which ``fields`` to
send. Adding, replying and resolving are seen by everyone who can open the file: level 3.

A comments watch remembers the creation time of the newest comment or reply handed on (Google's clock) and the ids
created exactly then, so two in the same millisecond are neither lost nor shown twice. Drive filters on *modified*
time, which a new reply moves, so a check asks for everything modified since and decides what is new by creation
time. What the account wrote itself is never news. A file that is gone closes its watch.
"""

from __future__ import annotations

from standard import ToolBase

from .common import DOCUMENT, HttpError, connect, failed, get_item, no_account, refused, segment
from .docs_model import paragraphs
from .docs_tool import document
from .watching import Seen, run

TEXT_MOST = 2000
REPLIES_SHOWN = 5
WATCH_PAGES = 10
AUTHOR = "author(displayName,emailAddress,me)"
REPLY_FIELDS = f"id,content,{AUTHOR},createdTime,modifiedTime,action,deleted"
COMMENT_FIELDS = (f"id,content,quotedFileContent,{AUTHOR},createdTime,modifiedTime,resolved,deleted,"
                  f"replies({REPLY_FIELDS})")


def cut(text, most=TEXT_MOST) -> str:
    text = str(text or "")
    return text if len(text) <= most else text[:most] + "…"


def quoted(comment) -> str:
    return cut((comment.get("quotedFileContent") or {}).get("value"), 500)


def author(item) -> str:
    who = item.get("author") or {}
    return str(who.get("displayName") or who.get("emailAddress") or "")


def events(comments):
    """Every comment and reply, oldest first: (created, id, comment, reply or None)."""
    found = []
    for comment in comments:
        comment_id = str(comment.get("id") or "")
        if not comment.get("deleted"):
            found.append((str(comment.get("createdTime") or ""), comment_id, comment, None))
        for reply in comment.get("replies") or []:
            if not reply.get("deleted"):
                found.append((str(reply.get("createdTime") or ""), f"{comment_id}/{reply.get('id') or ''}",
                              comment, reply))
    return sorted(found, key=lambda e: (e[0], e[1]))


class CommentsTool(ToolBase):
    id = "comments"

    @staticmethod
    async def _page(drive, file_id, size, token="", since=""):
        params = {"pageSize": size, "includeDeleted": "false", "fields": f"nextPageToken,comments({COMMENT_FIELDS})"}
        if token:
            params["pageToken"] = token
        if since:
            params["startModifiedTime"] = since
        return await drive.get(f"files/{segment(file_id)}/comments", params=params)

    async def list(self, call):
        """Open comments only (by default) without pages that come back empty: pages are read until enough are
        found, and the page token says where in a page to go on."""
        clients, why = await connect(call, ask=False)
        if clients is None:
            return no_account(why)
        file_id = str(call.inputs["file_id"])
        include_resolved = call.inputs.get("include_resolved") is True
        most = int(call.inputs.get("max_results") or 20)
        token, skip = "", 0
        given = str(call.inputs.get("page_token") or "")
        if given:
            head, _, rest = given.partition(":")
            if not head.isdigit():
                return refused("page_token comes from an earlier comments_list.")
            skip, token = int(head), rest
        comments, replies, next_token = [], [], ""
        try:
            for _ in range(20):
                answer = await self._page(clients.drive, file_id, 100, token)
                page = answer.get("comments") or []
                for position, comment in enumerate(page[skip:], skip):
                    if comment.get("resolved") and not include_resolved:
                        continue
                    if len(comments) >= most:
                        next_token = f"{position}:{token}"
                        break
                    thread = [r for r in comment.get("replies") or [] if not r.get("deleted")]
                    comment_id = str(comment.get("id") or "")
                    comments.append({"comment_id": comment_id, "author": author(comment),
                                     "created": str(comment.get("createdTime") or ""),
                                     "text": cut(comment.get("content")), "quoted": quoted(comment),
                                     "resolved": bool(comment.get("resolved")), "reply_count": len(thread)})
                    for reply in thread[-REPLIES_SHOWN:]:
                        replies.append({"comment_id": comment_id, "reply_id": str(reply.get("id") or ""),
                                        "author": author(reply), "created": str(reply.get("createdTime") or ""),
                                        "text": cut(reply.get("content")), "action": str(reply.get("action") or "")})
                skip = 0
                if next_token or not answer.get("nextPageToken"):
                    break
                token = str(answer["nextPageToken"])
                if len(comments) >= most:
                    next_token = f"0:{token}"
                    break
        except HttpError as error:
            return failed(error)
        result = {"comments": comments, "replies": replies}
        if next_token:
            result["next_page_token"] = next_token
        return result, "success"

    async def add(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        file_id = str(call.inputs["file_id"])
        quote = str(call.inputs.get("quote") or "").strip()
        body = {"content": str(call.inputs["text"])}
        try:
            if quote:
                item = await get_item(clients.drive, file_id, "id,mimeType")
                if item.get("mimeType") != DOCUMENT:
                    return refused("A quote can be checked only in a Google Doc; leave quote out for other files.")
                # A quote is shown as the passage the comment is about, so it must be words the document holds.
                text = "\n".join(p.text for p in paragraphs(await document(clients.docs, file_id)))
                if quote not in text:
                    return refused("The document does not contain that quote; copy it exactly from docs_read.")
                body["quotedFileContent"] = {"mimeType": "text/plain", "value": quote}
            made = await clients.drive.post(f"files/{segment(file_id)}/comments", json=body,
                                            params={"fields": COMMENT_FIELDS})
        except HttpError as error:
            return failed(error)
        return {"comment_id": str(made.get("id") or ""), "created": str(made.get("createdTime") or "")}, "success"

    async def reply(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        try:
            made = await clients.drive.post(
                f"files/{segment(str(call.inputs['file_id']))}/comments/{segment(str(call.inputs['comment_id']))}"
                f"/replies", params={"fields": REPLY_FIELDS}, json={"content": str(call.inputs["text"])})
        except HttpError as error:
            return failed(error)
        return {"reply_id": str(made.get("id") or ""), "created": str(made.get("createdTime") or "")}, "success"

    async def resolve(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        path = (f"files/{segment(str(call.inputs['file_id']))}/comments/"
                f"{segment(str(call.inputs['comment_id']))}")
        try:
            if (await clients.drive.get(path, params={"fields": COMMENT_FIELDS})).get("resolved"):
                return {"resolved": True, "already": True}, "success"
            # Drive resolves a comment by a reply that carries the action.
            body = {"action": "resolve", **({"content": str(call.inputs["text"])} if call.inputs.get("text") else {})}
            made = await clients.drive.post(f"{path}/replies", params={"fields": REPLY_FIELDS}, json=body)
        except HttpError as error:
            return failed(error)
        return {"resolved": True, "reply_id": str(made.get("id") or "")}, "success"

    # -- the watch -------------------------------------------------------------------------------------------------
    @classmethod
    async def _all(cls, drive, file_id, since=""):
        found, token = [], ""
        for _ in range(WATCH_PAGES):
            answer = await cls._page(drive, file_id, 100, token, since)
            found += answer.get("comments") or []
            token = str(answer.get("nextPageToken") or "")
            if not token:
                break
        return found

    async def watch(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        file_id = str(call.inputs["file_id"])
        try:
            item = await get_item(clients.drive, file_id, "id,name,modifiedTime")
            seen = events(await self._all(clients.drive, file_id))
        except HttpError as error:
            return failed(error)
        if seen:
            since = seen[-1][0]
            ids = ",".join(e[1] for e in seen if e[0] == since)
        else:
            # No comment yet: the file's last change is a moment, in Google's clock, every later comment follows.
            since, ids = str(item.get("modifiedTime") or ""), ""
        label = str(item.get("name") or "")
        watch_ref = await call.records.create("watch", {
            "kind": "comments", "status": "active", "account": clients.address, "target_id": file_id,
            "label": label, "cursor": since, "cursor_ids": ids, "note": str(call.inputs.get("note") or "")})
        return {"watch_ref": watch_ref, "label": label, "since": since}, "success"

    async def new(self, call):
        async def read(clients, watch, most):
            fields = watch["fields"]
            file_id, since = str(fields.get("target_id") or ""), str(fields.get("cursor") or "")
            seen = {i for i in str(fields.get("cursor_ids") or "").split(",") if i}
            fresh = [e for e in events(await self._all(clients.drive, file_id, since))
                     if e[0] > since or (e[0] == since and e[1] not in seen)]
            more = len(fresh) > most
            fresh = fresh[:most]
            rows = []
            for created, _, comment, reply in fresh:
                item = reply or comment
                if (item.get("author") or {}).get("me"):
                    continue
                rows.append({"watch_ref": watch["ref"], "kind": "reply" if reply else "comment",
                             "file_id": file_id, "label": str(fields.get("label") or ""),
                             "comment_id": str(comment.get("id") or ""),
                             **({"reply_id": str(reply.get("id") or "")} if reply else {}),
                             "author": author(item), "created": created, "text": cut(item.get("content")),
                             **({"quoted": quoted(comment)} if quoted(comment) else {}),
                             **({"in_reply_to": cut(comment.get("content"), 300)} if reply else {})})
            if not fresh:
                return Seen(rows)
            newest = fresh[-1][0]
            at_newest = {e[1] for e in fresh if e[0] == newest} | (seen if newest == since else set())
            return Seen(rows, {"cursor": newest, "cursor_ids": ",".join(sorted(at_newest))}, more=more)

        return await run(call, "comments", read), "success"
