"""Gmail's REST API over ``standard.google``: retries on 429 and Google's 403 rate reasons, ``scope`` for a missing
permission, ``unknown`` for a write that got no answer, every request off the agent's loop.

Requests are as Gmail's reference documents them (developers.google.com/workspace/gmail/api/reference/rest):

- a draft goes as JSON ``{"message": {"raw", "threadId"}}`` while small, and as a multipart upload to
  ``/upload/gmail/v1/users/me/drafts`` (``message/rfc822``, at most 35 MB) when it carries files;
- ``history.list`` with ``startHistoryId`` answers 404 once that id has expired (typically after a week, sometimes
  hours): the caller then reads the inbox again from its last time;
- ``attachments.get`` answers ``{size, data}`` with the data in base64url.
"""

from __future__ import annotations

import asyncio
import json
import secrets as random_tokens
from typing import Any, Awaitable, Iterable, TypeVar

from standard.accounts import account
from standard.google import Google
from standard.http import HttpError, segment

from .mime import b64url_decode, b64url_encode, bare, header, part_text, walk

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
#: Gmail sends at most 25 MB of attachments in one message.
ATTACHMENTS_MOST = 25 * 1000 * 1000
#: What drafts.create takes through the upload address (its reference: 35 MB, message/rfc822).
RAW_MOST = 35 * 1024 * 1024
#: A draft larger than this goes as an upload, never inside a JSON body.
JSON_MOST = 1024 * 1024
METADATA = ["From", "To", "Cc", "Date", "Subject", "Message-ID", "Reply-To"]

T = TypeVar("T")


class Gmail(Google):
    SERVICE = "Gmail"

    def __init__(self, secret: dict[str, Any]):
        super().__init__(secret, base=BASE)
        self._me: set[str] | None = None
        self._send_as: list[dict[str, Any]] | None = None
        self._labels: list[dict[str, Any]] | None = None
        self._profile: dict[str, Any] | None = None

    @property
    def upload_base(self) -> str:
        return self.base.replace("/gmail/v1/", "/upload/gmail/v1/", 1)

    # -- who the account is ------------------------------------------------------------------------------------
    async def profile(self, fresh: bool = False) -> dict[str, Any]:
        if self._profile is None or fresh:
            self._profile = await self.get("profile")
        return self._profile

    async def send_as(self) -> list[dict[str, Any]]:
        """The addresses the account sends as (settings.sendAs.list, allowed by gmail.modify)."""
        if self._send_as is None:
            try:
                self._send_as = list((await self.get("settings/sendAs")).get("sendAs") or [])
            except HttpError as refused:
                if refused.kind in ("auth", "rate", "unknown"):
                    raise
                self._send_as = []
        return self._send_as

    async def me(self) -> set[str]:
        """Every address that is the account itself: its own and each send-as alias."""
        if self._me is None:
            found = {self.account.lower()} - {""}
            found |= {str(a.get("sendAsEmail") or "").lower() for a in await self.send_as()} - {""}
            if not found:
                found.add(str((await self.profile()).get("emailAddress") or "").lower())
            self._me = found
        return self._me

    async def address(self) -> str:
        return self.account or str((await self.profile()).get("emailAddress") or "")

    # -- reads -------------------------------------------------------------------------------------------------
    async def message(self, message_id: str, fmt: str = "full") -> dict[str, Any]:
        params: dict[str, Any] = {"format": fmt}
        if fmt == "metadata":
            params["metadataHeaders"] = METADATA
        return await self.get(f"messages/{segment(message_id)}", params=params)

    async def thread(self, thread_id: str, fmt: str = "full") -> dict[str, Any]:
        params: dict[str, Any] = {"format": fmt}
        if fmt == "metadata":
            params["metadataHeaders"] = METADATA
        return await self.get(f"threads/{segment(thread_id)}", params=params)

    async def attachment(self, message_id: str, attachment_id: str) -> bytes:
        answer = await self.get(f"messages/{segment(message_id)}/attachments/{segment(attachment_id)}")
        return b64url_decode(answer.get("data"))

    async def labels(self) -> list[dict[str, Any]]:
        if self._labels is None:
            self._labels = list((await self.get("labels")).get("labels") or [])
        return self._labels

    async def text_of(self, message: dict[str, Any]) -> tuple[str, list[dict], int]:
        """A message's readable text, its attachments, and how many inline images were left out. A text part too
        long to come inline is fetched; each is decoded by its own charset; HTML only when there is no text part."""
        plain, rich, attachments, inline = walk(message.get("payload") or {})
        chosen = plain or rich
        pieces: list[str] = []
        for part in chosen:
            body = part.get("body") or {}
            if body.get("data") is not None:
                raw = b64url_decode(body.get("data"))
            elif body.get("attachmentId"):
                raw = await self.attachment(str(message.get("id") or ""), str(body["attachmentId"]))
            else:
                raw = b""
            pieces.append(await asyncio.to_thread(part_text, part, raw) if len(raw) > 50_000 else part_text(part, raw))
        return "\n\n".join(p.strip("\n") for p in pieces if p.strip()), attachments, inline

    # -- writes ------------------------------------------------------------------------------------------------
    async def create_draft(self, raw: bytes, thread_id: str = "") -> dict[str, Any]:
        if len(raw) <= JSON_MOST:
            message: dict[str, Any] = {"raw": b64url_encode(raw)}
            if thread_id:
                message["threadId"] = thread_id
            return await self.post("drafts", json={"message": message})
        boundary = "decentai_" + random_tokens.token_hex(16)
        while boundary.encode() in raw:
            boundary = "decentai_" + random_tokens.token_hex(16)
        metadata = json.dumps({"message": {"threadId": thread_id}} if thread_id else {"message": {}})
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{metadata}\r\n"
                f"--{boundary}\r\nContent-Type: message/rfc822\r\n\r\n").encode() + raw + f"\r\n--{boundary}--\r\n".encode()
        return await self.request("POST", f"{self.upload_base}/drafts", params={"uploadType": "multipart"}, data=body,
                                  headers={"Content-Type": f"multipart/related; boundary={boundary}"},
                                  timeout=max(120, len(raw) / 200_000))

    async def send_draft(self, draft_id: str) -> dict[str, Any]:
        # The id travels in the body: there is no drafts/{id}/send.
        return await self.post("drafts/send", json={"id": draft_id})

    async def delete_draft(self, draft_id: str) -> None:
        await self.delete(f"drafts/{segment(draft_id)}")


async def gmail_for(call: Any, wanted: Any = None, ask: bool = True) -> tuple[Gmail | None, str]:
    """The Gmail client over the connected account wanted, or None and why."""
    secret, why = await account(call, "google", wanted, ask, "Google")
    return (Gmail(secret), "") if secret is not None else (None, why)


async def gathered(jobs: Iterable[Awaitable[T]], most: int = 5) -> list[T | HttpError]:
    """Each job's answer in order, at most ``most`` at once; a refusal stands in its place."""
    gate = asyncio.Semaphore(most)

    async def one(job):
        async with gate:
            try:
                return await job
            except HttpError as refused:
                return refused

    return list(await asyncio.gather(*(one(j) for j in jobs)))


def link(account_address: str, message_id: str) -> str:
    who = account_address or "0"
    return f"https://mail.google.com/mail/u/{who}/#all/{message_id}"


def draft_link(account_address: str, message_id: str) -> str:
    who = account_address or "0"
    return f"https://mail.google.com/mail/u/{who}/#drafts?compose={message_id}"


def summary(message: dict[str, Any], account_address: str, names: dict[str, str] | None = None) -> dict[str, Any]:
    import html as _html

    message_id = str(message.get("id") or "")
    labels = [str(x) for x in message.get("labelIds") or []]
    row = {"message_id": message_id, "thread_id": str(message.get("threadId") or ""),
           "from": header(message, "From"), "to": header(message, "To"), "date": header(message, "Date"),
           "subject": header(message, "Subject"), "snippet": _html.unescape(str(message.get("snippet") or "")),
           "unread": "UNREAD" in labels, "starred": "STARRED" in labels,
           "link": link(account_address, message_id)}
    if names is not None:
        row["labels"] = [names.get(x, x) for x in labels]
    return row


def internal_ms(message: dict[str, Any]) -> int:
    try:
        return int(str(message.get("internalDate") or "0"))
    except ValueError:
        return 0


def sender(message: dict[str, Any]) -> str:
    return bare(header(message, "From"))


__all__ = ["ATTACHMENTS_MOST", "BASE", "Gmail", "HttpError", "JSON_MOST", "RAW_MOST", "draft_link", "gathered",
           "gmail_for", "internal_ms", "link", "sender", "summary"]
