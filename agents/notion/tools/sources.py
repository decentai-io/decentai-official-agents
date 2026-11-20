"""From an id the person or a search gave to the data source that holds a database's schema and rows.

A search, and every row's parent, names a *data source*; a link copied from Notion names the *database*. Either is
taken: a data source id is read as one, and a database id is read for its data sources. A database with one data
source (most of them) is that data source; one with several is refused, naming each, so the person can say which.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from standard.http import HttpError

from .notion_api import Notion
from .schema import DatabaseSchema, People
from .text import Text


class Several(Exception):
    """A database of several data sources: which one is for the person to say."""

    def __init__(self, database: Dict[str, Any], sources: List[Dict[str, Any]]):
        self.database = database
        self.sources = [{"data_source_id": str(s.get("id") or ""), "name": str(s.get("name") or "")}
                        for s in sources]
        title = Text.rich(database.get("title")) or str(database.get("id") or "")
        named = "; ".join(f"{s['name'] or '(unnamed)'}: {s['data_source_id']}" for s in self.sources)
        super().__init__(f"The database '{title}' holds {len(self.sources)} data sources ({named}). Say which, and "
                         f"give its data_source id as database_id.")


async def resolve(client: Notion, given: str) -> DatabaseSchema:
    """The data source ``given`` names, itself or as its database's only one. Raises Several or HttpError."""
    given = str(given or "").strip()
    try:
        return DatabaseSchema(await client.data_source(given))
    except HttpError as failed:
        # A database's id is no data source's: Notion answers 404 (or 400 for an id of another kind).
        if failed.kind not in ("not_found", "invalid"):
            raise
    database = await client.database(given)
    sources = database.get("data_sources") or []
    if len(sources) == 1:
        return DatabaseSchema(await client.data_source(str(sources[0].get("id") or "")), database)
    if not sources:
        raise HttpError("not_found", f"The database '{Text.rich(database.get('title'))}' has no data source this "
                                     f"integration can open.")
    raise Several(database, sources)


async def people_for(client: Notion, schema: DatabaseSchema, values: Optional[Dict[str, Any]] = None,
                     filter_: Any = None) -> Optional[People]:
    """The workspace's people, read only when these values or this filter name someone."""
    if not schema.needs_people(values, filter_):
        return None
    return People(await client.users())
