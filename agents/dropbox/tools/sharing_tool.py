"""Who can open a file, and changing that on authorization.

Dropbox shares a file and a folder differently: a file has members of its own, while a folder must first become a
shared folder and then gains members. Links are separate objects with their own audience. A permission id here says
how to take the access away (``member:<address>``, ``account:<dbid>``, ``group:<id>`` or ``link:<url>``), because
Dropbox has no single permission id to hand back.

Sharing a folder with people is two writes: make it a shared folder, then add them. When the second is refused, the
first is undone (the folder is unshared again), so nothing is left half done; when even that fails, the half-done
share is recorded as ``unfinished`` and said so.
"""

import asyncio

from standard import ToolBase
from standard.http import HttpError

from .account_tool import ready
from .dropbox_api import link_row, member_rows, summary, tag

#: How long to wait, in all, for a job Dropbox finishes in the background, asking every second.
JOB_SECONDS = 20
#: The most permission rows read for one item.
MEMBERS_MOST = 500


async def finished(client, launched, check):
    """A Dropbox background job waited for, without blocking the agent: its last status. ``check`` asks after it."""
    job = str(launched.get("async_job_id") or "") if tag(launched) == "async_job_id" else ""
    status = launched
    for _ in range(JOB_SECONDS):
        if tag(status) not in ("async_job_id", "in_progress"):
            return status
        await asyncio.sleep(1)
        status = await check(job)
    return status


class SharingTool(ToolBase):
    id = "sharing"

    @staticmethod
    async def _access(client, item):
        """Everyone who can open the item, the shared folder id its members belong to ("" for a file, or a folder
        nobody shares), and whether there were more than could be read."""
        sharing = item.get("sharing_info") or {}
        item_id = str(item.get("id") or "")
        more = False
        if tag(item) == "folder":
            shared_folder_id = str(sharing.get("shared_folder_id") or "")
            parent_id = str(sharing.get("parent_shared_folder_id") or "")
            if shared_folder_id or parent_id:
                members, more = await client.folder_members(shared_folder_id or parent_id, MEMBERS_MOST)
                rows = member_rows(members)
                if not shared_folder_id:
                    for row in rows:
                        if row["via"] != "owner":
                            row["via"] = "inherited"
            else:
                rows = []
        else:
            shared_folder_id = ""
            members, more = await client.file_members(item_id, MEMBERS_MOST)
            rows = member_rows(members)
        if not any(r["via"] == "owner" for r in rows):
            # An item nobody shares lists no members; its owner is the account itself.
            rows.insert(0, {"permission_id": "owner", "who": client.email or "you", "role": "owner", "via": "owner",
                            "link": ""})
        rows.extend(link_row(link) for link in await client.links(item_id))
        return rows, shared_folder_id, more

    async def permissions(self, call):
        client, refused = await ready(call)
        if client is None:
            return refused
        try:
            item = await client.metadata(str(call.inputs["item_id"]))
            rows, _, more = await self._access(client, item)
        except HttpError as failed:
            return failed.answer()
        return {"permissions": rows, "more": more}, "success"

    async def invite(self, call):
        client, refused = await ready(call)
        if client is None:
            return refused
        inputs = call.inputs
        recipients = [str(r).strip() for r in inputs["recipients"] if str(r).strip()]
        names = [r for r in recipients if "@" not in r]
        if names:
            return {"error": "Recipients are email addresses; find the address for " + ", ".join(names) + " first.",
                    "kind": "invalid"}, "error"
        item_id = str(inputs["item_id"])
        role = str(inputs.get("role") or "read")
        message = str(inputs.get("message") or "")
        try:
            item = await client.metadata(item_id)
        except HttpError as failed:
            return failed.answer()
        if tag(item) == "folder":
            return await self._invite_folder(call, client, item, recipients, role, message)
        granted, problem = [], None
        try:
            # One call for everyone; Dropbox answers per member, so a refusal for one does not undo the others.
            for answer in await client.add_file_members(str(item.get("id") or item_id), recipients, role, message):
                email = str((answer.get("member") or {}).get("email") or "")
                if tag(answer.get("result")) == "success":
                    granted.append(email)
                elif problem is None:
                    problem = HttpError("invalid", f"Dropbox did not share with {email}: "
                                                   f"{tag(answer.get('result'))}.")
        except HttpError as failed:
            problem = failed
        share_ref = ""
        if granted:
            share_ref = await self._record(call, client, item, "invite", granted, role, "granted")
        if problem is not None:
            done = f" Already shared with: {', '.join(granted)} (share {share_ref})." if granted else ""
            return {"error": problem.message + done, "kind": problem.kind}, "error"
        return {"share_ref": share_ref, "invited": granted, "role": role}, "success"

    async def _invite_folder(self, call, client, folder, recipients, role, message):
        existing = str((folder.get("sharing_info") or {}).get("shared_folder_id") or "")
        made_here = not existing
        try:
            shared_folder_id = existing or await self._share(client, folder)
        except HttpError as failed:
            return failed.answer()
        try:
            await client.add_folder_members(shared_folder_id, recipients, role, message)
        except HttpError as failed:
            if not made_here:
                return failed.answer()
            # The folder was made shared for these people alone: undo it, so nothing is left half done.
            undone = await self._unshare(client, shared_folder_id)
            if undone:
                return {"error": f"{failed.message} Nothing was shared: the folder was made a shared folder for "
                                 f"this and has been unshared again.", "kind": failed.kind}, "error"
            share_ref = await self._record(call, client, folder, "invite", recipients, role, "unfinished")
            return {"error": f"{failed.message} The folder was made a shared folder but nobody was added, and it "
                             f"could not be unshared again (share {share_ref} records it): unshare "
                             f"'{folder.get('path_display')}' in Dropbox, or try the invitation again.",
                    "kind": failed.kind}, "error"
        share_ref = await self._record(call, client, folder, "invite", recipients, role, "granted")
        return {"share_ref": share_ref, "invited": recipients, "role": role}, "success"

    @staticmethod
    async def _share(client, folder):
        """The folder made a shared folder: its id. Dropbox may finish that in the background; the job is asked
        after, never re-sent."""
        launched = await client.share_folder(str(folder.get("path_lower") or folder.get("path_display") or ""))
        status = launched if launched.get("shared_folder_id") else await finished(client, launched, client.share_job)
        if status.get("shared_folder_id"):
            return str(status["shared_folder_id"])
        if tag(status) in ("async_job_id", "in_progress"):
            raise HttpError("unknown", "Dropbox has not finished sharing the folder; check sharing_permissions in a "
                                       "minute before inviting again.")
        raise HttpError("invalid", f"Dropbox could not share the folder: {tag(status.get('failed')) or tag(status)}.")

    @staticmethod
    async def _unshare(client, shared_folder_id):
        try:
            status = await finished(client, await client.unshare_folder(shared_folder_id), client.job)
        except HttpError:
            return False
        return tag(status) == "complete" or status == {}

    @staticmethod
    async def _record(call, client, item, kind, who, role, status, link="", permission_id=""):
        return await call.records.create("share", {
            "item_id": str(item.get("id") or item.get("path_lower") or ""), "name": str(item.get("name") or ""),
            "kind": kind, "who": ", ".join(who) if isinstance(who, list) else str(who),
            "role": role, "link": link,
            "permission_id": permission_id or (",".join("member:" + w for w in who) if isinstance(who, list) else ""),
            "status": status, "account": client.email})

    async def link(self, call):
        client, refused = await ready(call)
        if client is None:
            return refused
        item_id = str(call.inputs["item_id"])
        link_type = str(call.inputs.get("link_type") or "view")
        scope = str(call.inputs.get("link_scope") or "organization")
        access = "editor" if link_type == "edit" else "viewer"
        try:
            item = await client.metadata(item_id)
            if scope == "organization":
                team = client.team
                if not team:
                    return {"error": "A personal Dropbox account has no team to share with; ask whether anyone with "
                                     "the link is meant.", "kind": "invalid"}, "error"
                audience = "team"
                reach = f"people in {team.get('name')}" if team.get("name") else "people in your team"
            else:
                audience, reach = "public", "anyone with the link"
            path = str(item.get("path_lower") or item.get("path_display") or item_id)
            try:
                made = await client.create_link(path, audience, access)
            except HttpError as failed:
                if "shared_link_already_exists" not in summary(failed):
                    raise
                # The item already has a link: Dropbox refused to make a second, so nothing was written. That link
                # is changed to what was asked instead.
                made = await client.modify_link(await self._existing_link(client, failed, path), audience, access)
        except HttpError as failed:
            return failed.answer()
        url = str(made.get("url") or "")
        share_ref = await self._record(call, client, item, "link", reach, "write" if link_type == "edit" else "read",
                                       "granted", link=url, permission_id=f"link:{url}")
        return {"share_ref": share_ref, "link": url, "reach": reach}, "success"

    @staticmethod
    async def _existing_link(client, failed, path):
        error = (failed.body or {}).get("error") or {}
        metadata = (error.get("shared_link_already_exists") or {}).get("metadata") or {}
        if metadata.get("url"):
            return str(metadata["url"])
        links = await client.links(path)
        if not links:
            raise failed
        return str(links[0].get("url") or "")

    async def revoke(self, call):
        client, refused = await ready(call)
        if client is None:
            return refused
        item_id = str(call.inputs["item_id"])
        permission_id = str(call.inputs["permission_id"])
        try:
            item = await client.metadata(item_id)
            rows, shared_folder_id, _ = await self._access(client, item)
        except HttpError as failed:
            return failed.answer()
        row = next((r for r in rows if r["permission_id"] == permission_id), None)
        if row is None:
            return {"error": "That item has no such permission; sharing_permissions lists the ones it has.",
                    "kind": "not_found"}, "error"
        if row["via"] == "owner":
            return {"error": "The owner's access cannot be removed.", "kind": "invalid"}, "error"
        if row["via"] == "inherited":
            return {"error": "This access comes from a shared parent folder; stop sharing the folder instead.",
                    "kind": "invalid"}, "error"
        kind, _, value = permission_id.partition(":")
        try:
            if kind == "link":
                await client.revoke_link(value)
            else:
                member = {".tag": "email", "email": value} if kind == "member" else \
                    {".tag": "dropbox_id", "dropbox_id": value}
                if tag(item) == "folder":
                    status = await finished(client, await client.remove_folder_member(shared_folder_id, member),
                                            client.remove_job)
                    if tag(status) == "failed":
                        return {"error": f"Dropbox could not remove {row['who']}: {tag(status.get('failed'))}.",
                                "kind": "invalid"}, "error"
                else:
                    await client.remove_file_member(str(item.get("id") or item_id), member)
        except HttpError as failed:
            return failed.answer()
        share_ref = await self._record(call, client, item, "link" if kind == "link" else "invite", row["who"],
                                       "write" if row["role"] == "write" else "read", "revoked", link=row["link"],
                                       permission_id=permission_id)
        return {"revoked": True, "share_ref": share_ref}, "success"
