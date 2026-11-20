"""Mail as Outlook keeps it: a message's row, a conversation, a folder by its name, attachments listed, and chat files
attached to a draft (small ones in one request, larger ones through an upload session)."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
from typing import Any
from urllib.parse import urlsplit

from standard.graph import Graph, address, addresses, bare
from standard.http import FILE_MOST, HttpError, human_size, segment

from .common import TEXT, Refused, local_text
from .zones import Zone

FIELDS = ("id,conversationId,internetMessageId,subject,bodyPreview,from,toRecipients,ccRecipients,receivedDateTime,"
          "sentDateTime,hasAttachments,isDraft,isRead,flag,importance,webLink,parentFolderId")
ATTACHMENT_FIELDS = "id,name,contentType,size,isInline"
KINDS = {"#microsoft.graph.fileAttachment": "file", "#microsoft.graph.itemAttachment": "item",
         "#microsoft.graph.referenceAttachment": "reference"}

#: Folders Outlook names the same in every mailbox and language, and the words people use for them.
WELL_KNOWN = {"inbox": "inbox", "archive": "archive", "drafts": "drafts", "sent": "sentitems", "sentitems": "sentitems",
              "sent items": "sentitems", "deleted": "deleteditems", "deleteditems": "deleteditems",
              "deleted items": "deleteditems", "trash": "deleteditems", "junk": "junkemail", "junkemail": "junkemail",
              "junk email": "junkemail", "spam": "junkemail", "outbox": "outbox"}

#: Under this, a file goes in one request (its base64 stays inside Graph's 4 MB request); over it, an upload session.
INLINE_MOST = 3_000_000
#: Each PUT of an upload session: under Microsoft's 4 MB, in order.
CHUNK = 2 * 1024 * 1024
UPLOAD_HOSTS = ("outlook.office.com", "outlook.office365.com")


def row(message: dict[str, Any], zone: Zone) -> dict[str, Any]:
    flag = str(((message.get("flag") or {}).get("flagStatus")) or "notFlagged")
    found = {
        "message_id": str(message.get("id") or ""),
        "thread_id": str(message.get("conversationId") or ""),
        "from": address(message.get("from")),
        "to": addresses(message.get("toRecipients")),
        "date": local_text(message.get("receivedDateTime") or message.get("sentDateTime"), zone),
        "subject": str(message.get("subject") or ""),
        "snippet": str(message.get("bodyPreview") or "")[:300],
        "read": bool(message.get("isRead")),
        "flag": {"notFlagged": "none"}.get(flag, flag),
        "has_attachments": bool(message.get("hasAttachments")),
        "link": str(message.get("webLink") or ""),
    }
    if message.get("ccRecipients"):
        found["cc"] = addresses(message.get("ccRecipients"))
    return found


def received(message: dict[str, Any]) -> str:
    return str(message.get("receivedDateTime") or message.get("sentDateTime") or "")


def mine(message: dict[str, Any], me: Any) -> bool:
    return bare(message.get("from")) in me.mine


async def conversation(graph: Graph, thread_id: str, select: str = FIELDS, most: int = 500) -> list[dict[str, Any]]:
    """A conversation's messages, oldest first, drafts left out. Graph filters on conversationId and is not asked to
    order as well: a filter with an order on another property is refused ("too complex")."""
    escaped = str(thread_id).replace("'", "''")
    found = await graph.pages("me/messages", params={"$filter": f"conversationId eq '{escaped}'", "$select": select,
                                                     "$top": 50}, headers=TEXT, most=most)
    found = [m for m in found if not m.get("isDraft")]
    return sorted(found, key=lambda m: (received(m), str(m.get("id") or "")))


async def thread_of(graph: Graph, inputs: dict[str, Any]) -> str:
    """The conversation named by thread_id, or the one message_id belongs to."""
    if inputs.get("thread_id"):
        return str(inputs["thread_id"])
    if inputs.get("message_id"):
        message = await graph.get(f"me/messages/{segment(inputs['message_id'])}",
                                  params={"$select": "id,conversationId"})
        return str(message.get("conversationId") or "")
    raise Refused("Name the conversation with thread_id, or a message of it with message_id.")


# -- folders --------------------------------------------------------------------------------------------------------

async def folders(graph: Graph, most: int = 500) -> list[dict[str, Any]]:
    """Every mail folder, children under their parents, with its path ("Clients/Harbourline")."""
    select = "id,displayName,parentFolderId,childFolderCount,unreadItemCount,totalItemCount"
    found: list[dict[str, Any]] = []
    level = [({**f, "path": str(f.get("displayName") or "")})
             for f in await graph.pages("me/mailFolders", params={"$select": select, "$top": 100}, most=most)]
    depth = 0
    while level and len(found) < most:
        found.extend(level)
        following = []
        for folder in level:
            if int(folder.get("childFolderCount") or 0) and depth < 4 and len(found) + len(following) < most:
                for child in await graph.pages(f"me/mailFolders/{segment(folder['id'])}/childFolders",
                                               params={"$select": select, "$top": 100}, most=most):
                    following.append({**child, "path": f"{folder['path']}/{child.get('displayName') or ''}"})
        level, depth = following, depth + 1
    return found[:most]


async def folder_id(graph: Graph, name: Any) -> tuple[str, str]:
    """(the id or well-known name Graph takes, the folder's name) for a folder a person names."""
    wanted = str(name or "").strip()
    if not wanted:
        return "inbox", "Inbox"
    if wanted.lower() in WELL_KNOWN:
        return WELL_KNOWN[wanted.lower()], wanted
    found = await folders(graph)
    for key in ("id", "path", "displayName"):
        hits = [f for f in found if str(f.get(key) or "").lower() == wanted.lower()]
        if len(hits) == 1:
            return str(hits[0]["id"]), str(hits[0].get("path") or hits[0].get("displayName") or "")
        if len(hits) > 1:
            raise Refused(f"Several folders are called {wanted!r}: " + ", ".join(h["path"] for h in hits) +
                          ". Name it by its path.")
    names = ", ".join(f["path"] for f in found[:30])
    raise Refused(f"There is no folder {wanted!r} in this mailbox. Folders: {names}.", "not_found")


def folder_path(folder: str) -> str:
    return f"me/mailFolders/{segment(folder)}/messages"


# -- attachments ----------------------------------------------------------------------------------------------------

def attachment_kind(item: dict[str, Any]) -> str:
    return KINDS.get(str(item.get("@odata.type") or ""), "file")


async def attachments(graph: Graph, message_id: str) -> list[dict[str, Any]]:
    """A message's attachments, each with its kind, inline pictures (a signature's logo) left out."""
    found = await graph.pages(f"me/messages/{segment(message_id)}/attachments",
                              params={"$select": ATTACHMENT_FIELDS}, most=100)
    rows = []
    for item in found:
        mime = str(item.get("contentType") or "")
        if item.get("isInline") and (mime.startswith("image/") or not mime):
            continue
        kind, size = attachment_kind(item), int(item.get("size") or 0)
        rows.append({"message_id": message_id, "attachment_id": str(item.get("id") or ""),
                     "filename": str(item.get("name") or ""), "kind": kind, "mime_type": mime, "size": size,
                     "can_save": kind != "reference" and size <= FILE_MOST})
    return rows


async def attach_files(call: Any, graph: Graph, draft_id: str, given: list[Any]) -> list[dict[str, Any]]:
    """Chat files attached to a draft: each under 3 MB in one request, a larger one (at most a chat file's size)
    through an upload session, its pieces in order."""
    done = []
    for one in given or []:
        path = await call.file(str(one))
        data = await asyncio.to_thread(path.read_bytes)
        name = path.name
        if len(data) > FILE_MOST:
            raise Refused(f"“{name}” is {human_size(len(data))}; at most {human_size(FILE_MOST)} can be attached "
                          f"from the chat.", "too_large")
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        base = f"me/messages/{segment(draft_id)}/attachments"
        if len(data) < INLINE_MOST:
            await graph.post(base, json={"@odata.type": "#microsoft.graph.fileAttachment", "name": name,
                                         "contentType": mime,
                                         "contentBytes": base64.b64encode(data).decode("ascii")})
            how = "attached"
        else:
            await call.progress(f"Uploading {name} ({human_size(len(data))})")
            session = await graph.post(f"{base}/createUploadSession", json={"AttachmentItem": {
                "attachmentType": "file", "name": name, "size": len(data), "contentType": mime}})
            await upload(graph, str(session.get("uploadUrl") or ""), data, name)
            how = "uploaded"
        done.append({"filename": name, "size": len(data), "how": how})
    return done


async def upload(graph: Graph, url: str, data: bytes, name: str) -> None:
    """The pieces of one file PUT to an upload session's own address: pre-authorized, so no token goes with them."""
    host = (urlsplit(url).hostname or "").lower()
    ours = (urlsplit(graph.base).hostname or "").lower()
    if not url.startswith(("https://", "http://")) or not (host in UPLOAD_HOSTS or host == ours):
        raise HttpError("http", f"Outlook gave an upload address on {host or 'no host'}, which is not Outlook's; "
                                f"“{name}” was not sent there.")
    total = len(data)
    for start in range(0, total, CHUNK):
        piece = data[start:start + CHUNK]
        answer = await graph.request("PUT", url, data=piece, raw=True, authorized=False, write=True, headers={
            "Content-Type": "application/octet-stream", "Content-Length": str(len(piece)),
            "Content-Range": f"bytes {start}-{start + len(piece) - 1}/{total}"})
        if answer.status_code not in (200, 201):
            raise HttpError("http", f"Outlook answered {answer.status_code} to a piece of “{name}”.",
                            answer.status_code)
        if answer.status_code == 201 and start + len(piece) < total:
            raise HttpError("http", f"Outlook ended the upload of “{name}” early.")
