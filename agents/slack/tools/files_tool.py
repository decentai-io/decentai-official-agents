"""A file shared in a conversation, brought into the chat: at most what a chat file may be (8 MiB less 64 KiB). A
larger one is refused before its bytes are read, with its Slack link."""

import re

from standard import ToolBase
from standard.http import HttpError

from .account_tool import client_for, failure, not_connected
from .slack_api import FILE_MOST, too_large

FILE_ID = re.compile(r"^F[A-Z0-9]{2,}$")


class FilesTool(ToolBase):
    id = "files"

    async def read(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        file_id = str(call.inputs["file_id"]).strip()
        if not FILE_ID.match(file_id):
            return {"error": f"file_id is a Slack file id such as F0123ABCD, as a message's files give it, not "
                             f"{file_id!r}.", "kind": "invalid"}, "error"
        try:
            found = await client.file_info(file_id)
            name = str(found.get("name") or found.get("title") or file_id)
            link = str(found.get("permalink") or "")
            size = int(found.get("size") or 0)
            if found.get("mode") == "hidden_by_limit":
                return {"error": f"{name} is hidden by the workspace's plan limits, so Slack will not give it.",
                        "kind": "denied"}, "error"
            if found.get("is_external") or found.get("mode") == "external":
                return {"error": f"{name} is kept outside Slack: open it from its link{f' ({link})' if link else ''}.",
                        "kind": "invalid"}, "error"
            if size > FILE_MOST:
                raise too_large(name, size, link)
            url = str(found.get("url_private_download") or found.get("url_private") or "")
            if not url:
                return {"error": f"Slack gives no download for {name}.", "kind": "not_found"}, "error"
            await call.progress(f"Bringing {name} into the chat")
            data = await client.file_bytes(url, name)
        except HttpError as exc:
            if exc.kind == "too_large" and link and link not in exc.message:
                exc.message += f" Open it in Slack instead: {link}"
            return failure(exc)
        return {"file": call.give(name, data), "name": name, "size": len(data),
                "type": str(found.get("mimetype") or ""), "link": link}, "success"
