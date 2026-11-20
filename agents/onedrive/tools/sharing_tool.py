"""Who can open a file, and changing that: level 3, since others see it.

Every grant and every revocation is kept as a share record, so "what have I shared, and with whom" has an answer
that does not depend on remembering the chat. Who it went to is kept encrypted (values), never in plain keys.

An invitation is sent as Microsoft documents it for both kinds of account: recipients, roles, requireSignIn and
sendInvitation, and the message only when there is one. Microsoft can refuse to send the invitation email from a
personal account ("There was a problem sharing", or a 207 asking the account to be verified): then the access is
granted without Microsoft's email, and the person is told to send the link themselves.
"""

from __future__ import annotations

from standard import ToolBase
from standard.http import HttpError

from .common import client, failed, ids, refused
from .drive import permission_row

REACH = {"organization": "people in your organization", "anonymous": "anyone with the link"}


def emails(permission):
    found = set()
    for key in ("grantedToV2", "grantedTo"):
        user = (permission.get(key) or {}).get("user") or {}
        if user.get("email"):
            found.add(str(user["email"]).lower())
    for identity in permission.get("grantedToIdentitiesV2") or permission.get("grantedToIdentities") or []:
        user = (identity or {}).get("user") or {}
        if user.get("email"):
            found.add(str(user["email"]).lower())
    if (permission.get("invitation") or {}).get("email"):
        found.add(str(permission["invitation"]["email"]).lower())
    return found


def answered(response):
    """(permissions granted, recipients Microsoft could not notify) from a 200 or a 207 Multi-Status."""
    granted, problems = [], []
    body = response.json() if response.content else {}
    for permission in body.get("value") or []:
        error = permission.get("error")
        if error:
            problems.append({"who": ", ".join(sorted(emails(permission))) or "a recipient",
                             "error": str(error.get("message") or error.get("code") or "not notified"),
                             **({"fix": str(error["fixItUrl"])} if error.get("fixItUrl") else {})})
        granted.append(permission)
    return granted, problems


class SharingTool(ToolBase):
    id = "sharing"

    async def permissions(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        item_id, drive_id = ids(call)
        try:
            granted = await drive.permissions(item_id, drive_id)
        except HttpError as error:
            return failed(error)
        return {"permissions": [permission_row(p) for p in granted]}, "success"

    async def invite(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        inputs = call.inputs
        recipients = list(dict.fromkeys(str(r).strip() for r in inputs["recipients"] if str(r).strip()))
        names = [r for r in recipients if "@" not in r]
        if names or not recipients:
            return refused("Recipients are email addresses; find the address for " + (", ".join(names) or "them")
                           + " first.")
        item_id, drive_id = ids(call)
        role = str(inputs.get("role") or "read")
        message = str(inputs.get("message") or "").strip()
        try:
            item = await drive.item(item_id, drive_id)
        except HttpError as error:
            return failed(error)
        if "root" in item:
            return refused("The top of a drive cannot be shared; share a folder or file in it.")
        body = {"recipients": [{"email": r} for r in recipients], "roles": [role], "requireSignIn": True,
                "sendInvitation": True, **({"message": message} if message else {})}
        emailed, why_not, problems = True, "", []
        try:
            granted, problems = answered(await drive.invite(item_id, drive_id, body))
        except HttpError as error:
            if error.kind in ("auth", "scope", "not_found", "denied", "rate", "unknown"):
                return failed(error)
            # Microsoft would not send the invitation. Whatever access it gave anyway is found first, so nobody is
            # invited twice; the rest are granted without Microsoft's email.
            why_not = error.message
            try:
                have = await drive.permissions(item_id, drive_id)
            except HttpError as again:
                return failed(again)
            granted = [p for p in have if emails(p) & {r.lower() for r in recipients}]
            missing = [r for r in recipients if not any(r.lower() in emails(p) for p in granted)]
            if missing:
                quiet = {"recipients": [{"email": r} for r in missing], "roles": [role], "requireSignIn": True,
                         "sendInvitation": False}
                try:
                    more, problems = answered(await drive.invite(item_id, drive_id, quiet))
                except HttpError as again:
                    return refused(f"Microsoft refused to share '{item.get('name')}' with {', '.join(missing)}: "
                                   f"{why_not}; and again without its email: {again.message}. Nothing was shared "
                                   f"with them.", again.kind)
                granted += more
            emailed = False
        links = [str((p.get("link") or {}).get("webUrl") or "") for p in granted if (p.get("link") or {}).get("webUrl")]
        link = links[0] if links else str(item.get("webUrl") or "")
        ref = await call.records.create("share", {
            "item_id": item_id, **({"drive_id": drive_id} if drive_id else {}), "name": str(item.get("name") or ""),
            "kind": "invite", "who": ", ".join(recipients), "role": role, "status": "granted",
            "permission_id": ",".join(str(p.get("id") or "") for p in granted)[:2000],
            **({"account": drive.account} if drive.account else {}), **({"link": link} if link else {})})
        result = {"share_ref": ref, "invited": recipients, "role": role, "emailed": emailed and not problems,
                  "link": link}
        if not emailed:
            result["note"] = (f"Access is granted, but Microsoft did not send the invitation email ({why_not}). Send "
                              f"them the link yourself.")
        if problems:
            result["not_notified"] = problems
        return result, "success"

    async def link(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        item_id, drive_id = ids(call)
        link_type = str(call.inputs.get("link_type") or "view")
        scope = str(call.inputs.get("link_scope") or "")
        try:
            personal = await drive.personal()
            if personal and scope == "organization":
                return refused("An organization-only link needs a work or school (Microsoft 365) account; on a "
                               "personal account there is no organization. Share with named people (sharing_invite), "
                               "or make a link for anyone with it (link_scope anonymous) if the person asks for that.",
                               "unsupported")
            if not scope:
                if personal:
                    return refused("On a personal account a link reaches anyone who has it. Ask the person whether "
                                   "that is what they want (link_scope anonymous), or invite named people instead.")
                scope = "organization"
            item = await drive.item(item_id, drive_id)
            if "root" in item:
                return refused("The top of a drive cannot be shared; share a folder or file in it.")
            made = await drive.create_link(item_id, drive_id, {"type": link_type, "scope": scope})
        except HttpError as error:
            return failed(error)
        url = str((made.get("link") or {}).get("webUrl") or "")
        ref = await call.records.create("share", {
            "item_id": item_id, **({"drive_id": drive_id} if drive_id else {}), "name": str(item.get("name") or ""),
            "kind": "link", "who": REACH[scope], "role": "write" if link_type == "edit" else "read", "link": url,
            "permission_id": str(made.get("id") or ""), "status": "granted",
            **({"account": drive.account} if drive.account else {})})
        return {"share_ref": ref, "link": url, "reach": REACH[scope]}, "success"

    async def revoke(self, call):
        drive, refusal = await client(call)
        if drive is None:
            return refusal
        item_id, drive_id = ids(call)
        permission_id = str(call.inputs["permission_id"])
        try:
            item = await drive.item(item_id, drive_id)
            current = {str(p.get("id") or ""): p for p in await drive.permissions(item_id, drive_id)}
        except HttpError as error:
            return failed(error)
        if permission_id not in current:
            return refused("That item has no such permission; sharing_permissions lists the ones it has.",
                           "not_found")
        row = permission_row(current[permission_id])
        if row["via"] == "owner":
            return refused("The owner's access cannot be removed.")
        if row["via"] == "inherited":
            return refused("This access comes from a parent folder; stop sharing the folder instead.")
        try:
            await drive.delete_permission(item_id, drive_id, permission_id)
        except HttpError as error:
            return failed(error)
        ref = await call.records.create("share", {
            "item_id": item_id, **({"drive_id": drive_id} if drive_id else {}), "name": str(item.get("name") or ""),
            "kind": "link" if row["via"] == "link" else "invite", "who": row["who"], "role": row["role"],
            **({"link": row["link"]} if row.get("link") else {}), "permission_id": permission_id,
            "status": "revoked", **({"account": drive.account} if drive.account else {})})
        return {"revoked": True, "share_ref": ref, "who": row["who"]}, "success"
