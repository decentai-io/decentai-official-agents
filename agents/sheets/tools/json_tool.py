"""JSON and JSON Lines: what is in a file, values by path, a schema checked (formats included), two versions
compared, records kept by conditions, and the way across to a table and back."""

import asyncio
import json
import re
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

from standard import ToolBase

from . import numbers
from .common import decimal_of, failed, job, opened
from .jsondoc import (as_json, cell, flatten, key_map, leaves, parse, parse_path, preview, same, type_of)
from .jsondoc import select as select_path
from .tables import Refused, raw_of
from .workbook import check_csv, named, write_csv

WALK_LIMIT = 1000
RECORDS_MOST = 100_000
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]{2,}$")
DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")
TIME = re.compile(r"^\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})?$")


async def document(call, key: str = "file_ref"):
    filename, raw = await raw_of(call, "source", str(call.inputs[key]))
    return await asyncio.to_thread(parse, str(call.inputs[key]), filename, raw)


def checker():
    """The schema's formats, held: email, date, date-time, time, uri, ipv4, ipv6, uuid, regex."""
    from jsonschema import FormatChecker

    found = FormatChecker()

    @found.checks("email")
    def _email(value):
        return not isinstance(value, str) or bool(EMAIL.match(value))

    @found.checks("date-time", raises=ValueError)
    def _date_time(value):
        if not isinstance(value, str):
            return True
        if not DATE_TIME.match(value):
            return False
        datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00").replace("t", "T"))
        return True

    @found.checks("time")
    def _time(value):
        return not isinstance(value, str) or bool(TIME.match(value))

    @found.checks("uri")
    def _uri(value):
        if not isinstance(value, str):
            return True
        parts = urlsplit(value)
        return bool(parts.scheme) and bool(parts.netloc or parts.path) and " " not in value

    return found


class Condition:
    def __init__(self, spec):
        self.path, self.op, self.value = str(spec["path"]), str(spec["op"]), spec.get("value")
        self.steps = parse_path(self.path)
        if self.op not in ("exists", "missing") and "value" not in spec:
            raise Refused(f"The condition on {self.path} ({self.op}) needs a value.")
        if self.op in ("gt", "gte", "lt", "lte") and not (
                isinstance(self.value, str) or (isinstance(self.value, (int, float))
                                                and not isinstance(self.value, bool))):
            raise Refused(f"'{self.op}' on {self.path} needs a number or a text (an ISO date compares as text).")

    def holds(self, record) -> bool:
        found = [value for _, value in select_path(record, self.steps, limit=200)]
        if self.op == "exists":
            return bool(found)
        if self.op == "missing":
            return not found
        if self.op == "ne":
            return not any(same(v, self.value) for v in found)
        if self.op == "eq":
            return any(same(v, self.value) for v in found)
        if self.op == "contains":
            wanted = str(self.value).casefold()
            return any((isinstance(v, str) and wanted in v.casefold())
                       or (isinstance(v, list) and any(same(x, self.value) for x in v)) for v in found)
        return any(self._ordered(v) for v in found)

    def _ordered(self, mine) -> bool:
        number = isinstance(self.value, (int, float)) and not isinstance(self.value, bool)
        if number != (isinstance(mine, (int, float)) and not isinstance(mine, bool)):
            return False
        if not number and not isinstance(mine, str):
            return False
        return {"gt": mine > self.value, "gte": mine >= self.value, "lt": mine < self.value,
                "lte": mine <= self.value}[self.op]


class JsonTool(ToolBase):
    id = "json"

    async def inspect(self, call):
        try:
            doc = await document(call)
        except Exception as exc:
            return failed(exc)
        result = {"file_ref": doc.file_ref, "filename": doc.filename, "kind": doc.kind, "valid": doc.valid,
                  "top_level": doc.top_level, "records": len(doc.records), "bytes": doc.size, "keys": []}
        if doc.encoding:
            result["encoding"] = doc.encoding
        if not doc.valid:
            result["problem"] = doc.problem
            if doc.line:
                result["error_line"] = doc.line
            if doc.column:
                result["error_column"] = doc.column
            return result, "success"
        rows, truncated, scanned, depth = await asyncio.to_thread(
            key_map, doc.records, int(call.inputs.get("max_records") or 5000))
        result.update({"keys": rows, "keys_truncated": truncated, "scanned": scanned, "depth": depth})
        return result, "success"

    async def select(self, call):
        try:
            doc = await document(call)
            if not doc.valid:
                raise Refused(doc.problem)
            steps = parse_path(str(call.inputs["path"]))
        except ValueError as exc:
            return {"error": str(exc), "kind": "invalid"}, "error"
        except Exception as exc:
            return failed(exc)
        wanted = int(call.inputs.get("max_results") or 50)
        room = int(call.inputs.get("max_value_chars") or 2000)
        found = select_path(doc.data, steps, limit=WALK_LIMIT)
        results = []
        for where, value in found[:wanted]:
            entry = {"path": where, "type": type_of(value)}
            text = preview(value, room)
            entry.update({"preview": text, "truncated": True} if text.endswith("…") and len(text) > room
                         else {"value": value, "truncated": False})
            results.append(entry)
        return {"file_ref": doc.file_ref, "path": str(call.inputs["path"]), "matches": len(found),
                "matches_capped": len(found) >= WALK_LIMIT, "truncated": len(found) > wanted,
                "results": results}, "success"

    async def validate(self, call):
        try:
            doc = await document(call)
            if not doc.valid:
                raise Refused(doc.problem)
            schema = await self._schema(call)
        except Exception as exc:
            return failed(exc)
        from jsonschema.exceptions import SchemaError
        from jsonschema.validators import validator_for

        kind = validator_for(schema)
        try:
            kind.check_schema(schema)
        except SchemaError as exc:
            return {"error": f"The schema itself is not valid: {exc.message}", "kind": "invalid"}, "error"
        validator = kind(schema, format_checker=checker())
        cap = int(call.inputs.get("max_errors") or 50)
        path = str(call.inputs.get("record_path") or "")
        try:
            records = self._records(doc, path) if path else doc.records
        except Exception as exc:
            return failed(exc)
        each = bool(call.inputs.get("each_record")) or bool(path) or doc.kind == "jsonl"

        def run():
            errors, checked, failures, truncated = [], 0, 0, False
            targets = list(enumerate(records)) if each else [(None, doc.data)]
            for index, target in targets:
                checked += 1
                found = sorted(validator.iter_errors(target), key=lambda e: [str(p) for p in e.absolute_path])
                failures += bool(found)
                for error in found:
                    if len(errors) >= cap:
                        truncated = True
                        break
                    errors.append(self._error(error, index))
            return errors, checked, failures, truncated

        errors, checked, failures, truncated = await asyncio.to_thread(run)
        valid = failures == 0
        job_ref = await job(call, "validate", doc.file_ref,
                            f"{'valid' if valid else 'not valid'}: {checked} checked, {failures} failing",
                            result={"errors": errors})
        return {"job_ref": job_ref, "file_ref": doc.file_ref, "valid": valid, "checked": checked,
                "failures": failures, "truncated": truncated, "errors": errors}, "success"

    async def compare(self, call):
        docs = []
        for key in ("file_ref_a", "file_ref_b"):
            try:
                doc = await document(call, key)
            except Exception as exc:
                return failed(exc)
            if not doc.valid:
                return {"error": f"{doc.filename or key}: {doc.problem}", "kind": "invalid"}, "error"
            docs.append(doc)
        cap = int(call.inputs.get("max_changes") or 100)
        return await asyncio.to_thread(self._compared, docs[0].data, docs[1].data, cap), "success"

    @staticmethod
    def _compared(data_a, data_b, cap):
        before, after = leaves(data_a), leaves(data_b)
        changes, counts = [], {"added": 0, "removed": 0, "changed": 0}
        paths = list(before) + [p for p in after if p not in before]
        for path in paths:
            in_a, in_b = path in before, path in after
            if in_a and in_b:
                if same(before[path], after[path]):
                    continue
                kind = "changed"
            else:
                kind = "added" if in_b else "removed"
            counts[kind] += 1
            if len(changes) < cap:
                entry = {"kind": kind, "path": path}
                if in_a:
                    entry["before"] = as_json(before[path])
                if in_b:
                    entry["after"] = as_json(after[path])
                if in_a and in_b and type_of(before[path]) != type_of(after[path]):
                    entry["type_changed"] = f"{type_of(before[path])} → {type_of(after[path])}"
                changes.append(entry)
        total = sum(counts.values())
        return {"identical": total == 0, **counts, "truncated": total > len(changes), "changes": changes}

    async def filter(self, call):
        inputs = call.inputs
        try:
            doc = await document(call)
            if not doc.valid:
                raise Refused(doc.problem)
            records = self._records(doc, str(inputs.get("record_path") or ""))
            conditions = [Condition(spec) for spec in inputs["conditions"]]
        except ValueError as exc:
            return {"error": str(exc), "kind": "invalid"}, "error"
        except Exception as exc:
            return failed(exc)
        every = (inputs.get("match") or "all") == "all"

        def kept_of():
            kept = [r for r in records if (all if every else any)(c.holds(r) for c in conditions)]
            body = json.dumps(kept, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
            json.loads(body)                           # read back before it is handed over
            return kept, body

        kept, body = await asyncio.to_thread(kept_of)
        filename = named(str(inputs["filename"]), ".json")
        job_ref = await job(call, "filter", doc.file_ref, f"{len(kept)} of {len(records)} record(s) kept",
                            output=filename)
        return {"job_ref": job_ref, "file_ref": call.give(filename, body), "filename": filename,
                "records": len(kept), "records_read": len(records), "verified": True}, "success"

    async def to_table(self, call):
        inputs = call.inputs
        try:
            doc = await document(call)
            if not doc.valid:
                raise Refused(doc.problem)
            records = self._records(doc, str(inputs.get("record_path") or ""))
        except ValueError as exc:
            return {"error": str(exc), "kind": "invalid"}, "error"
        except Exception as exc:
            return failed(exc)
        wanted = [str(c) for c in inputs.get("columns") or []]
        columns, flattened, skipped = [], [], 0
        seen = set()
        for record in records:
            if not isinstance(record, dict):
                if wanted:
                    skipped += 1
                    continue
                record = {"value": record}
            flat = flatten(record)
            flattened.append(flat)
            if not wanted:
                for name in flat:
                    if name not in seen:
                        seen.add(name)
                        columns.append(name)
        columns = wanted or columns
        if not columns:
            return {"error": "The records have no fields to make columns from.", "kind": "invalid"}, "error"
        rows = [[cell(flat.get(name)) for name in columns] for flat in flattened]
        delimiter = {"comma": ",", "semicolon": ";", "tab": "\t"}[str(inputs.get("delimiter") or "comma")]
        raw = await asyncio.to_thread(write_csv, columns, rows, delimiter)
        await asyncio.to_thread(check_csv, raw, len(rows), delimiter)
        filename = named(str(inputs["filename"]), ".csv")
        job_ref = await job(call, "to_table", doc.file_ref, f"{len(rows)} row(s), {len(columns)} column(s)",
                            output=filename)
        return {"job_ref": job_ref, "file_ref": call.give(filename, raw), "filename": filename, "rows": len(rows),
                "columns": columns, "skipped": skipped, "verified": True}, "success"

    async def from_table(self, call):
        inputs = call.inputs
        try:
            book, sheet = await opened(call)
        except Exception as exc:
            return failed(exc)
        infer = inputs.get("infer_types", True) is not False
        expand = inputs.get("expand_dots", True) is not False
        readers, as_text = {}, []
        if infer:
            for index, name in enumerate(sheet.columns):
                try:
                    readers[index] = sheet.numbers(name, decimal_of(call))
                except Refused:
                    as_text.append(name)          # its decimal mark can't be told: its values stay text
        records = []
        for row in sheet.rows:
            if all(c.blank for c in row):
                continue
            record = {}
            for index, name in enumerate(sheet.columns):
                value = self._value(sheet.cell(row, index), row, readers.get(index)) if infer else (
                    sheet.cell(row, index).text or None)
                if expand and "." in name:
                    self._nest(record, name.split("."), value)
                else:
                    record[name] = value
            records.append(record)
        try:
            body = json.dumps(records, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        except ValueError as exc:
            return {"error": f"A value can't be written as JSON: {exc}", "kind": "invalid"}, "error"
        filename = named(str(inputs["filename"]), ".json")
        job_ref = await job(call, "from_table", book.file_ref, f"{len(records)} record(s)", output=filename)
        result = {"job_ref": job_ref, "file_ref": call.give(filename, body), "filename": filename,
                  "records": len(records), "columns": list(sheet.columns), "header_row": sheet.header_row,
                  "verified": True}
        if as_text:
            result["left_as_text"] = as_text
        return result, "success"

    # ------------------------------------------------------------------------------------------------------------
    async def _schema(self, call):
        inline = call.inputs.get("schema")
        ref = str(call.inputs.get("schema_ref") or "")
        if isinstance(inline, dict) and inline:
            return inline
        if not ref:
            raise Refused("No schema was given: pass schema as an object, or schema_ref naming an attached schema.")
        doc = await document(call, "schema_ref")
        if doc.kind != "json" or not doc.valid:
            raise Refused(f"The schema file is not valid JSON: {doc.problem}")
        if not isinstance(doc.data, dict):
            raise Refused("A JSON Schema must be an object.")
        return doc.data

    @staticmethod
    def _error(error, index):
        where = "$"
        for part in error.absolute_path:
            where = f"{where}[{part}]" if isinstance(part, int) else (str(part) if where == "$" else f"{where}.{part}")
        entry = {"path": where, "rule": str(error.validator), "message": preview(error.message, 300)}
        if index is not None:
            entry["record"] = index
        return entry

    @staticmethod
    def _records(doc, path):
        if not path:
            records = doc.records
        else:
            found = select_path(doc.data, parse_path(path), limit=RECORDS_MOST + 1)
            if not found:
                raise Refused(f"No record matched '{path}'.")
            records = found[0][1] if len(found) == 1 and isinstance(found[0][1], list) else [v for _, v in found]
        if len(records) > RECORDS_MOST:
            raise Refused(f"There are more than {RECORDS_MOST:,} records; at most {RECORDS_MOST:,} are taken. "
                          f"Narrow record_path. Nothing was written.", "too_large")
        return records

    @staticmethod
    def _value(found, row, read):
        """A cell as a JSON value: a number only when its column's writing makes it one and nothing is lost; an
        identifier ("007", "+971…", sixteen digits) stays text; true/false/null as themselves; a date as ISO text."""
        shown = found.shown
        if shown is None or (isinstance(shown, str) and not shown.strip()):
            return None
        if isinstance(shown, bool):
            return shown
        number = numbers.native(shown)
        if number is None and isinstance(shown, str):
            text = shown.strip()
            if text.lower() in ("true", "false"):
                return text.lower() == "true"
            if text.lower() == "null":
                return None
            found_shape = numbers.shape(text)
            if numbers.identifier(text) or read is None or found_shape is None or found_shape.currency:
                return shown                         # "AED 12" stays as written: a number would lose its currency
            number = read(row)
            if number is None:
                return shown
        if number is None:
            return found.text
        if number == number.to_integral_value() and number.as_tuple().exponent >= 0:
            return int(number)
        as_float = float(number)
        return as_float if Decimal(repr(as_float)) == number.normalize() or Decimal(repr(as_float)) == number \
            else found.text

    @staticmethod
    def _nest(record: dict, parts, value) -> None:
        cursor = record
        for part in parts[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[parts[-1]] = value
