"""A database's schema, and its rows.

The schema is what every row write is checked against and what a filter is built from, so it is the first thing to
read about a database: property names, their types, and the options a select, multi-select or status property
allows. Since Notion's 2025-09-03 model it is a data source's; ``database_id`` takes either id (sources.py).
"""

from standard import ToolBase
from standard.http import HttpError

from .notion_api import client_for, invalid, not_connected
from .schema import PageRow, SchemaProblem
from .sources import Several, people_for, resolve
from .text import Text


class DatabasesTool(ToolBase):
    id = "databases"

    async def get(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        try:
            schema = await resolve(client, str(call.inputs["database_id"]))
        except Several as several:
            # Not a failure: the person picks one, and each is read by its own id.
            return {"database_id": str(several.database.get("id") or ""), "title": Text.rich(several.database.get("title")),
                    "url": str(several.database.get("url") or ""), "properties": [],
                    "data_sources": several.sources}, "success"
        except HttpError as failed:
            return failed.answer()
        return {"database_id": schema.database_id, "data_source_id": schema.source_id, "title": schema.title,
                "url": schema.url, "properties": schema.rows()}, "success"

    @staticmethod
    def conditions(inputs):
        """The single filter_* inputs and the compound filter as one group, or None."""
        group = []
        if inputs.get("filter_property"):
            if "filter_operator" not in inputs:
                raise SchemaProblem("filter_property needs filter_operator.")
            group.append({"property": inputs["filter_property"], "operator": inputs["filter_operator"],
                          "value": inputs.get("filter_value")})
        compound = inputs.get("filter")
        if compound:
            if not group:
                return compound
            group.append(compound)
        return {"match": "all", "conditions": group} if group else None

    async def query(self, call):
        client, why = await client_for(call)
        if client is None:
            return not_connected(why)
        inputs = call.inputs
        most = int(inputs.get("max_results") or 20)
        try:
            schema = await resolve(client, str(inputs["database_id"]))
            try:
                given = self.conditions(inputs)
                people = await people_for(client, schema, filter_=given)
                filter_ = None
                if given:
                    filter_ = schema.compound(given, people)
                    # One condition alone is sent as itself, as Notion's examples do.
                    joined = next(iter(filter_.values()))
                    if len(joined) == 1 and "property" in joined[0]:
                        filter_ = joined[0]
                sorts = None
                if inputs.get("sort_property"):
                    sorts = [schema.sort(str(inputs["sort_property"]),
                                         str(inputs.get("sort_direction") or "descending"))]
            except SchemaProblem as problem:
                return invalid(str(problem))
            answer = await client.query(schema.source_id, most, str(inputs.get("start_cursor") or ""),
                                        filter_, sorts)
        except Several as several:
            return invalid(str(several))
        except HttpError as failed:
            return failed.answer()
        rows = [PageRow.row(page) for page in (answer.get("results") or [])[:most]
                if page.get("object", "page") == "page"]
        result = {"database": schema.title, "data_source_id": schema.source_id, "rows": rows}
        if answer.get("has_more") and answer.get("next_cursor"):
            result["next_cursor"] = str(answer["next_cursor"])
        return result, "success"
