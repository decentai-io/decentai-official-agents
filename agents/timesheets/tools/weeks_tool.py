"""A week at a glance, locked when it is handed in, its gaps checked on a schedule, and the workbook that goes with
it.

A gap is a working day (Monday to Friday, or Sunday to Thursday, as the person's week is), up to as_of, with fewer
hours than the day should hold; days still to come are not gaps. Submitting locks the week's entries until it is
reopened. The workbook is read back cell by cell before it is returned: what the file holds is what was written.
"""

import asyncio
import io
from decimal import Decimal

from standard import ToolBase

from .common import (DAY_LIMIT, DAY_NAMES, ZERO, as_text, entries, hours_of, iso_date, keys_of, projects,
                     today, week_days, week_of, week_record, working, zone_of)

#: A text a spreadsheet would take for a formula: kept as text, never run (a meeting title "=HYPERLINK(…)").
FORMULA_START = ("=", "+", "-", "@")


async def summarize(call, week, days, as_of, daily, work_week):
    rows = await entries(call, {"week": week})
    known = await projects(call)
    grid = {}
    for entry in rows:
        day = iso_date(entry["date"])
        if day not in days:
            continue
        cells = grid.setdefault(entry["project_code"], [ZERO] * 7)
        cells[days.index(day)] += hours_of(entry["hours"]) or ZERO
    day_totals = [sum((cells[i] for cells in grid.values()), ZERO) for i in range(7)]
    table, billable = [], ZERO
    for code in sorted(grid):
        keys = (known.get(code) or {}).get("fields") or {}
        total = sum(grid[code], ZERO)
        if keys.get("billable") == "yes":
            billable += total
        table.append({"project_code": code, "name": str(keys.get("name") or ""),
                      "billable": keys.get("billable") == "yes",
                      "days": [as_text(h) for h in grid[code]], "total": as_text(total)})
    gaps = [{"date": day.isoformat(), "day": DAY_NAMES[day.weekday()], "logged": as_text(day_totals[i]),
             "short": as_text(daily - day_totals[i])}
            for i, day in enumerate(days) if working(day, work_week) and day <= as_of and day_totals[i] < daily]
    status = keys_of(await week_record(call, week)).get("status") or "open"
    return {"week": week, "from": days[0].isoformat(), "to": days[6].isoformat(),
            "status": status, "dates": [d.isoformat() for d in days], "as_of": as_of.isoformat(),
            "work_week": work_week, "projects": table, "day_totals": [as_text(t) for t in day_totals],
            "total": as_text(sum(day_totals, ZERO)), "billable_hours": as_text(billable),
            "gaps": gaps, "short_total": as_text(sum((Decimal(g["short"]) for g in gaps), ZERO)),
            "entries": len(rows)}


def _week(call):
    """(week, days, as_of, daily, work_week, problem): the week and today on the person's clock."""
    inputs = call.inputs
    _, zone, problem = zone_of(inputs)
    work_week = str(inputs.get("work_week") or "mon_fri")
    if problem:
        return None, None, None, None, work_week, ({"error": problem}, "error")
    now = today(inputs)
    week = str(inputs.get("week") or week_of(now)).strip()
    days = week_days(week)
    as_of = iso_date(inputs["as_of"]) if inputs.get("as_of") else now
    daily = hours_of(inputs.get("daily_hours", 8))
    if days is None:
        return week, days, as_of, daily, work_week, ({"error": "week is an ISO week, like 2026-W37."}, "error")
    if as_of is None:
        return week, days, as_of, daily, work_week, ({"error": "as_of must be YYYY-MM-DD."}, "error")
    if daily is None or daily <= 0 or daily > DAY_LIMIT:
        return week, days, as_of, daily, work_week, ({"error": "daily_hours must be between 0 and 24."}, "error")
    return week, days, as_of, daily, work_week, None


def _workbook(week, days, summary, rows):
    """The workbook's bytes, and what every cell was written as: blocking work, run off the agent's loop."""
    from openpyxl import Workbook

    wb = Workbook()
    written = {}

    def put(sheet, row):
        sheet.append(row)
        index = sheet.max_row
        for column, value in enumerate(row, 1):
            cell = sheet.cell(row=index, column=column)
            if isinstance(value, str) and value.startswith(FORMULA_START):
                cell.value, cell.data_type = value, "s"          # text, never a formula
            if isinstance(value, Decimal):
                cell.number_format = "0.00"
            written[(sheet.title, index, column)] = value

    sheet = wb.active
    sheet.title = "Timesheet"
    put(sheet, [f"Timesheet — week {week}, {days[0]:%d %b} to {days[6]:%d %b %Y} — {summary['status']}"])
    put(sheet, [])
    put(sheet, ["Project", "Name", "Billable"] + [f"{DAY_NAMES[i]} {d.day:02d}" for i, d in enumerate(days)]
        + ["Total"])
    for project in summary["projects"]:
        put(sheet, [project["project_code"], project["name"], "yes" if project["billable"] else "no"]
            + [Decimal(h) for h in project["days"]] + [Decimal(project["total"])])
    put(sheet, ["Total", "", ""] + [Decimal(t) for t in summary["day_totals"]] + [Decimal(summary["total"])])
    detail = wb.create_sheet("Entries")
    put(detail, ["Date", "Project", "Hours", "Note", "Source", "Status"])
    for entry in rows:
        put(detail, [entry["date"], entry["project_code"], Decimal(entry["hours"]), entry["note"], entry["source"],
                     entry["status"]])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue(), written


def _read_back(raw, written):
    """The cells that do not hold what was written, as (sheet, cell, wanted, found); [] when every one does."""
    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(raw))
    wrong = []
    if book.sheetnames != ["Timesheet", "Entries"]:
        return [("", "", "sheets Timesheet and Entries", ", ".join(book.sheetnames))]
    for (title, row, column), wanted in written.items():
        cell = book[title].cell(row=row, column=column)
        found = cell.value
        if isinstance(wanted, Decimal):
            same = isinstance(found, (int, float)) and Decimal(str(found)).quantize(Decimal("0.01")) == wanted
        else:
            same = (found if found is not None else "") == ("" if wanted is None else wanted)
            same = same and (not isinstance(wanted, str) or cell.data_type == "s" or not wanted)
        if not same:
            wrong.append((title, cell.coordinate, wanted, found))
    return wrong


class WeeksTool(ToolBase):
    id = "weeks"

    async def summary(self, call):
        week, days, as_of, daily, work_week, problem = _week(call)
        if problem:
            return problem
        return await summarize(call, week, days, as_of, daily, work_week), "success"

    async def gaps(self, call):
        """This week's gaps up to today (or yesterday), for a schedule: three record lists, no model."""
        week, days, as_of, daily, work_week, problem = _week(call)
        if problem:
            return problem
        if not call.inputs.get("include_today", False) and not call.inputs.get("as_of"):
            as_of = as_of.fromordinal(as_of.toordinal() - 1)
        found = await summarize(call, week, days, as_of, daily, work_week)
        gaps = found["gaps"]
        if gaps:
            said = (f"{len(gaps)} working day{'s' if len(gaps) != 1 else ''} of {week} up to {as_of.isoformat()} "
                    f"{'are' if len(gaps) != 1 else 'is'} short of {as_text(daily)} hours: "
                    + ", ".join(f"{g['day']} {g['date']} ({g['logged']} logged)" for g in gaps)
                    + f"; {found['short_total']} hours in all.")
        else:
            said = f"No gaps in {week} up to {as_of.isoformat()}."
        return {"week": week, "as_of": as_of.isoformat(), "work_week": work_week, "status": found["status"],
                "total": found["total"], "gaps": gaps, "short_total": found["short_total"], "said": said}, "success"

    async def submit(self, call):
        week, days, as_of, daily, work_week, problem = _week(call)
        if problem:
            return problem
        record = await week_record(call, week)
        if keys_of(record).get("status") == "submitted":
            return {"error": f"The week {week} was already submitted on "
                             f"{keys_of(record).get('submitted_on')}."}, "error"
        rows = await entries(call, {"week": week})
        if not rows:
            return {"error": f"Nothing is logged in {week}."}, "error"
        summary = await summarize(call, week, days, as_of, daily, work_week)
        for entry in rows:
            if entry["status"] != "submitted":
                await call.records.update("entry", entry["entry_ref"], {"status": "submitted"})
        fields = {"week": week, "status": "submitted", "submitted_on": today(call.inputs).isoformat(),
                  "total_hours": summary["total"]}
        if record:
            await call.records.update("week", record["ref"], fields)
        else:
            await call.records.create("week", fields)
        return {"week": week, "status": "submitted", "total": summary["total"],
                "entries": len(rows), "gaps": summary["gaps"]}, "success"

    async def reopen(self, call):
        week, _, _, _, _, problem = _week(call)
        if problem:
            return problem
        record = await week_record(call, week)
        if keys_of(record).get("status") != "submitted":
            return {"error": f"The week {week} is not submitted."}, "error"
        await call.records.update("week", record["ref"],
                                  {"status": "open", "submitted_on": ""})
        for entry in await entries(call, {"week": week}):
            await call.records.update("entry", entry["entry_ref"], {"status": "draft"})
        return {"week": week, "status": "open"}, "success"

    async def workbook(self, call):
        week, days, as_of, daily, work_week, problem = _week(call)
        if problem:
            return problem
        summary = await summarize(call, week, days, as_of, daily, work_week)
        if not summary["entries"]:
            return {"error": f"Nothing is logged in {week}."}, "error"
        rows = sorted(await entries(call, {"week": week}),
                      key=lambda e: (e["date"], e["project_code"], e["entry_ref"]))
        await call.progress(f"Writing the workbook for {week}")
        raw, written = await asyncio.to_thread(_workbook, week, days, summary, rows)
        try:
            wrong = await asyncio.to_thread(_read_back, raw, written)
        except Exception as exc:
            return {"error": f"The workbook did not read back: {exc}"}, "error"
        if wrong:
            where = "; ".join(f"{sheet}!{cell} holds {found!r}, not {wanted!r}" for sheet, cell, wanted, found
                              in wrong[:5])
            return {"error": f"The workbook did not read back as written ({len(wrong)} cells): {where}."}, "error"
        filename = f"timesheet-{week}.xlsx"
        return {"file_ref": call.give(filename, raw), "filename": filename, "total": summary["total"],
                "verified": True, "cells_checked": len(written)}, "success"
