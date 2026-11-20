"""Who can open a file, and changing that on the person's go-ahead.

Drive has no link object: sharing by link opens the file to the account's organization, or to anyone, and the file's
own address is the link. The organization is the account's own hosted domain (Google's ``hd``), never taken from the
address: a personal account has none, and is told so.
"""

from __future__ import annotations

from standard import ToolBase

from .common import EMAIL, ONE, HttpError, connect, failed, get_item, no_account, refused, segment

ROLES = {"read": "reader", "comment": "commenter", "write": "writer"}
SHOWN = {"owner": "owner", "organizer": "manager", "fileOrganizer": "content manager", "writer": "write",
         "commenter": "comment", "reader": "read"}


def permission_row(permission: dict) -> dict:
    kind = str(permission.get("type") or "")
    role = SHOWN.get(str(permission.get("role") or ""), str(permission.get("role") or ""))
    if kind == "anyone":
        who, via = "anyone with the link", "link"
    elif kind == "domain":
        who, via = f"people at {permission.get('domain') or 'the organization'}", "link"
    else:
        who = str(permission.get("emailAddress") or permission.get("displayName") or "")
        via = "owner" if role == "owner" else ("group" if kind == "group" else "invite")
    details = permission.get("permissionDetails") or []
    if details and all(d.get("inherited") for d in details):
        via = "inherited"
    return {"permission_id": str(permission.get("id") or ""), "who": who, "role": role, "via": via}


class SharingTool(ToolBase):
    id = "sharing"

    @staticmethod
    async def _permissions(drive, item_id):
        return await drive.token_pages(f"files/{segment(item_id)}/permissions", items="permissions", params={
            **ONE, "pageSize": 100, "fields": "nextPageToken,permissions(id,type,role,emailAddress,domain,"
                                              "displayName,permissionDetails)"})

    async def permissions(self, call):
        clients, why = await connect(call, ask=False)
        if clients is None:
            return no_account(why)
        item_id = str(call.inputs["item_id"])
        try:
            item = await get_item(clients.drive, item_id, "id,name,webViewLink")
            granted = await self._permissions(clients.drive, item_id)
        except HttpError as error:
            return failed(error)
        return {"name": str(item.get("name") or ""), "link": str(item.get("webViewLink") or ""),
                "permissions": [permission_row(p) for p in granted]}, "success"

    async def invite(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        inputs = call.inputs
        recipients = [str(r).strip() for r in inputs["recipients"] if str(r).strip()]
        wrong = [r for r in recipients if not EMAIL.match(r)]
        if wrong:
            return refused("Recipients are email addresses; find the address for " + ", ".join(wrong) + " first.")
        item_id, role = str(inputs["item_id"]), str(inputs.get("role") or "read")
        notify = inputs.get("notify", True) is not False
        try:
            item = await get_item(clients.drive, item_id, "id,name,webViewLink")
        except HttpError as error:
            return failed(error)
        granted, problem = [], None
        # One person per request; the first refusal stops the rest, and what was granted is kept on record.
        for recipient in recipients:
            params = {**ONE, "sendNotificationEmail": "true" if notify else "false"}
            if notify and inputs.get("message"):
                params["emailMessage"] = str(inputs["message"])
            try:
                made = await clients.drive.post(f"files/{segment(item_id)}/permissions", params=params,
                                                json={"type": "user", "role": ROLES[role], "emailAddress": recipient})
            except HttpError as error:
                problem = error
                break
            granted.append((recipient, str(made.get("id") or "")))
        share_ref = ""
        if granted:
            share_ref = await call.records.create("share", {
                "item_id": item_id, "kind": "invite", "role": role, "status": "granted",
                "permission_id": ",".join(p for _, p in granted), "account": clients.address,
                "name": str(item.get("name") or ""), "who": ", ".join(r for r, _ in granted)})
        if problem is not None:
            done = f" Already shared with: {', '.join(r for r, _ in granted)}." if granted else ""
            return {"error": problem.message + done, "kind": problem.kind,
                    **({"share_ref": share_ref} if share_ref else {})}, "error"
        return {"share_ref": share_ref, "invited": [r for r, _ in granted], "role": role,
                "link": str(item.get("webViewLink") or "")}, "success"

    async def link(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        item_id = str(call.inputs["item_id"])
        role = str(call.inputs.get("role") or "read")
        scope = str(call.inputs.get("scope") or "organization")
        try:
            item = await get_item(clients.drive, item_id, "id,name,webViewLink")
            if scope == "organization":
                domain = await clients.domain()
                if not domain:
                    return refused(f"{clients.address or 'This account'} is a personal Google account: it belongs to "
                                   f"no organization, so there is nobody to share 'within the organization' with. "
                                   f"Ask whether anyone with the link is meant, or invite people by address.",
                                   "invalid", personal=True)
                body = {"type": "domain", "domain": domain, "role": ROLES[role], "allowFileDiscovery": False}
                reach = f"people at {domain}"
            else:
                body = {"type": "anyone", "role": ROLES[role], "allowFileDiscovery": False}
                reach = "anyone with the link"
            made = await clients.drive.post(f"files/{segment(item_id)}/permissions", json=body,
                                            params={**ONE, "sendNotificationEmail": "false"})
        except HttpError as error:
            return failed(error)
        url = str(item.get("webViewLink") or "")
        share_ref = await call.records.create("share", {
            "item_id": item_id, "kind": "link", "role": role, "status": "granted", "account": clients.address,
            "permission_id": str(made.get("id") or ""), "name": str(item.get("name") or ""), "who": reach,
            "link": url})
        return {"share_ref": share_ref, "link": url, "reach": reach, "role": role}, "success"

    async def revoke(self, call):
        clients, why = await connect(call)
        if clients is None:
            return no_account(why)
        item_id, permission_id = str(call.inputs["item_id"]), str(call.inputs["permission_id"])
        try:
            item = await get_item(clients.drive, item_id, "id,name,webViewLink")
            current = {str(p.get("id") or ""): p for p in await self._permissions(clients.drive, item_id)}
        except HttpError as error:
            return failed(error)
        if permission_id not in current:
            return refused("That file has no such permission; sharing_permissions lists the ones it has.",
                           "not_found")
        row = permission_row(current[permission_id])
        if row["via"] == "owner":
            return refused("The owner's access cannot be removed.")
        if row["via"] == "inherited":
            return refused("This access comes from the shared drive or a parent folder; change it there.")
        try:
            await clients.drive.delete(f"files/{segment(item_id)}/permissions/{segment(permission_id)}", params=ONE)
        except HttpError as error:
            return failed(error)
        share_ref = await call.records.create("share", {
            "item_id": item_id, "kind": "link" if row["via"] == "link" else "invite",
            "role": row["role"] if row["role"] in ROLES else "read", "status": "revoked",
            "permission_id": permission_id, "account": clients.address, "name": str(item.get("name") or ""),
            "who": row["who"]})
        return {"revoked": True, "who": row["who"], "share_ref": share_ref}, "success"
