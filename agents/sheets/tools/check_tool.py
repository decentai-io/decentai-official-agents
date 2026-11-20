from collections import defaultdict

from standard import ToolBase

from .common import failed, job, opened
from .tables import blank_key, normalize


class CheckTool(ToolBase):
    id = "check"

    async def quality(self, call):
        try:
            book, sheet = await opened(call)
            keys = [str(c) for c in call.inputs.get("key_columns") or []]
            required = [str(c) for c in call.inputs.get("required_columns") or []]
            categorical = [str(c) for c in call.inputs.get("categorical_columns") or []]
            for name in keys + required + categorical:
                sheet.column(name)
        except Exception as exc:
            return failed(exc)
        limit = int(call.inputs.get("max_findings") or 100)

        groups, blank_keys = defaultdict(list), 0
        if keys:
            phone_like = {c for c in keys if any(w in c.casefold() for w in ("phone", "mobile", "هاتف", "جوال"))}
            for index, row in enumerate(sheet.rows):
                if all(sheet.cell(row, c).blank for c in sheet.columns):
                    continue
                key = tuple(normalize(sheet.cell(row, c).text, "digits" if c in phone_like else "lowercase")
                            for c in keys)
                if blank_key(key):
                    blank_keys += 1
                    continue
                groups[key].append(sheet.row_number(index))
        duplicate_groups = [{"key": " | ".join(k), "rows": rows} for k, rows in groups.items() if len(rows) > 1]

        missing = []
        for name in required:
            rows = [sheet.row_number(i) for i, row in enumerate(sheet.rows) if sheet.cell(row, name).blank]
            if rows:
                missing.append({"column": name, "rows": rows[:limit], "count": len(rows)})

        inconsistent = []
        for name in categorical:
            variants = defaultdict(set)
            for row in sheet.rows:
                text = sheet.cell(row, name).text
                if text.strip():
                    variants[normalize(text, "lowercase")].add(text)
            inconsistent += [{"column": name, "variants": sorted(seen)} for seen in variants.values() if len(seen) > 1]

        truncated = len(duplicate_groups) > limit or len(inconsistent) > limit
        summary = (f"{len(sheet.rows)} rows; {len(duplicate_groups)} duplicate group(s) by "
                   f"{', '.join(keys) or 'no key'}; {blank_keys} row(s) with a blank key not compared; "
                   f"{sum(m['count'] for m in missing)} blank required cell(s); "
                   f"{len(inconsistent)} inconsistent value set(s)")
        job_ref = await job(call, "quality", book.file_ref, summary)
        return {"job_ref": job_ref, "rows": len(sheet.rows), "header_row": sheet.header_row,
                "duplicate_groups": duplicate_groups[:limit], "blank_keys": blank_keys, "missing": missing,
                "inconsistent": inconsistent[:limit], "truncated": truncated}, "success"
