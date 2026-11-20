"""Feeds read without watching them, and an OPML list of feeds turned
into feed watches at once."""

from __future__ import annotations

import asyncio

from standard import ResourceDenied, ToolBase

from .feed_fetch import resolve_feed
from .feed_parse import MAX_OUTLINES, FeedProblem, opml_feeds
from .fetch import FetchError
from .looking import now_utc
from .page_text import phrase_pattern
from .watches_tool import checked_address

#: The most an OPML file may be.
OPML_MOST = 2 * 1024 * 1024


class FeedsTool(ToolBase):
    id = "feeds"

    async def read(self, call):
        url = str(call.inputs["url"]).strip()
        most = int(call.inputs.get("max_results") or 10)
        wanted = [" ".join(str(w).split()) for w in call.inputs.get("keywords") or [] if str(w).strip()]
        await call.progress(f"Reading the feed at {url}")
        try:
            feed, page_url = await resolve_feed(url)
        except FetchError as exc:
            return exc.result(), "error"
        except FeedProblem as exc:
            return {"error": str(exc), "kind": "not_a_feed"}, "error"
        items = feed.items
        if wanted:
            items = [i for i in items if any(phrase_pattern(w).search(f"{i.title}\n{i.text}") for w in wanted)]
        return {"feed_url": feed.url, "discovered_from": page_url, "title": feed.title,
                "format": feed.format, "items": [i.row() for i in items[:most]],
                "total": len(items)}, "success"

    async def import_(self, call):
        text = str(call.inputs.get("opml") or "")
        name = "the OPML text"
        if call.inputs.get("file_ref"):
            try:
                path = await call.file(str(call.inputs["file_ref"]))
            except ResourceDenied as exc:
                return {"error": str(exc), "kind": "not_found"}, "error"
            if path.stat().st_size > OPML_MOST:
                return {"error": f"The OPML file is {path.stat().st_size:,} bytes; at most "
                                 f"{OPML_MOST:,} are read.", "kind": "too_large"}, "error"
            body, name = path.read_bytes(), path.name
        elif text.strip():
            body = text.encode("utf-8")
        else:
            return {"error": "Give the OPML file (file_ref) or its text (opml).", "kind": "invalid"}, "error"
        try:
            listed = await asyncio.to_thread(opml_feeds, body, name)
        except FeedProblem as exc:
            return {"error": str(exc), "kind": "invalid"}, "error"
        if not listed:
            return {"error": f"{name} lists no feed (no outline with an xmlUrl).", "kind": "invalid"}, "error"
        if len(listed) > MAX_OUTLINES:
            return {"error": f"{name} lists {len(listed)} feeds; at most {MAX_OUTLINES} are imported at "
                             f"once. Split it.", "kind": "too_large"}, "error"
        existing = {str(r["fields"].get("url") or "") for r in await call.records.list(
            "watch", {"kind": "feed", "status": "active"})}
        added, already, skipped, at = [], [], [], now_utc()
        for entry in listed:
            url = entry["url"]
            if url in existing:
                already.append(url)
                continue
            why = checked_address(url)
            if why:
                skipped.append({"url": url, "why": why})
                continue
            # Not fetched now: the first scheduled check takes the feed as it is, and hands on
            # only what is published after.
            ref = await call.records.create("watch", {
                "status": "active", "kind": "feed", "url": url, "label": entry["title"],
                "final_url": url, "primed": 0, "seen": "", "checked_at": "", "tried_at": "",
                "changed_at": "", "failures": 0, "last_error": ""})
            existing.add(url)
            added.append({"watch_ref": ref, "label": entry["title"], "url": url})
        return {"added": len(added), "already": len(already), "skipped": skipped,
                "watches": added[:100], "imported_at": at}, "success"
