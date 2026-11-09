"""Secret-resource and level-3 external-action examples (simulated offline)."""

import hashlib
import json

from standard import ResourceDenied, ToolBase


class SyncTool(ToolBase):
    id = "sync"

    async def status(self, call):
        # The remote is simulated; the CONNECTION is real — the secret's
        # values arrive through record.read. A status asks nobody: with
        # no connection kept, it says so.
        kept = await call.records.list("connection")
        if not kept:
            return {"connected": False, "remote": "unbound"}, "success"
        secret = (await call.records.read("connection", kept[0]["ref"]))["fields"]
        return {
            "connected": bool(secret.get("api_token")),
            "remote": "simulated://notebook",
        }, "success"

    async def push(self, call):
        notebook = str(call.inputs["notebook"]).lower()

        try:
            await call.records.secret("connection")  # asked of the person when none is kept
        except ResourceDenied:
            return {"error": "No sync connection is kept."}, "error"

        records = await call.records.list("note", {"notebook": notebook})
        await call.progress(f"Pushing {len(records)} note(s) from '{notebook}'")

        # Simulated: nothing leaves. The digest is of the notes alone, never of the token.
        canonical = json.dumps(
            [record["fields"] for record in records], sort_keys=True, default=str,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return {"pushed": len(records), "digest": digest, "simulated": True}, "success"
