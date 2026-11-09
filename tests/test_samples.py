"""Every samples.yaml fits its own manifest as the platform loads it (agents.md §10; api_server/src/agents/samples.py):
its files exist and fit their resource, its records' fields are declared, typed right and the required ones given,
each "@ref" names a row before it; and the dates the example prompts name are the dates the sheets hold."""

import re
from datetime import date
from decimal import Decimal

import pytest
import yaml

from tests.harness import Agents, Chat, Records, Refusal, file_type

AGENTS = Agents().folders
WITH_SAMPLES = sorted(agent_id for agent_id, folder in AGENTS.items() if (folder / "samples.yaml").is_file())
POINTER = re.compile(r"@([a-z][a-z0-9_]{0,39})")


def sheet(agent_id: str) -> dict:
    return yaml.safe_load((AGENTS[agent_id] / "samples.yaml").read_text(encoding="utf-8"))


def problems(agent_id: str) -> list[str]:
    folder = AGENTS[agent_id]
    manifest = yaml.safe_load((folder / "manifest.yaml").read_text(encoding="utf-8"))
    declared = {r["id"]: (kind, r) for kind in ("secrets", "data", "files")
                for r in (manifest.get("resources") or {}).get(kind) or []}
    found, made = sheet(agent_id), set()
    wrong = [] if 0 < len(str(found.get("story") or "")) <= 200 else ["story: 1 to 200 characters"]
    if len(found.get("records") or []) + len(found.get("files") or []) > 200:
        wrong.append("more than 200 rows")
    for row in found.get("files") or []:
        kind, resource = declared.get(row.get("resource"), ("", {}))
        path = folder / str(row.get("path"))
        if kind != "files" or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            wrong.append(f"{row}: a files resource's file, at most 2 MiB")
        elif Chat.unfit(resource, path.name, path.stat().st_size):
            wrong.append(Chat.unfit(resource, path.name, path.stat().st_size))
        made.add(row.get("ref"))
    for row in found.get("records") or []:
        kind, resource = declared.get(row.get("resource"), ("", {}))
        given = {name: value for name, value in (row.get("fields") or {}).items()
                 if not (isinstance(value, str) and POINTER.fullmatch(value))}
        pointed = [value[1:] for value in (row.get("fields") or {}).values()
                   if isinstance(value, str) and POINTER.fullmatch(value)]
        wrong += [f"{row['resource']}: @{name} names no row before it" for name in pointed if name not in made]
        if kind not in ("data", "secrets"):
            wrong.append(f"{row.get('resource')}: not a data or secrets resource")
            continue
        try:
            Records.split(resource, given)
        except Refusal as refused:
            wrong.append(f"{row['resource']}: {refused}")
        wrong += [f"{row['resource']}: {f['name']} is required" for f in resource.get("fields") or []
                  if f.get("required") and f["type"] not in ("secret", "form") and f["name"] not in row["fields"]]
        made.add(row.get("ref"))
    return wrong


def test_the_agents_that_keep_records_ship_samples():
    assert WITH_SAMPLES == ["documents", "expenses", "json_data", "note", "notebook", "sheets", "slides", "tasks",
                            "timesheets"]


@pytest.mark.parametrize("agent_id", WITH_SAMPLES)
def test_each_sheet_fits_its_manifest(agent_id):
    assert problems(agent_id) == []


def test_the_renewals_keep_their_notice_deadlines():
    """The lease the example prompt names: renews 1 March 2027 with 90 days' notice, so the day to act is 1 December
    2026."""
    leases = [row for row in sheet("tasks")["records"]
              if row["resource"] == "agreement" and row["fields"]["kind"] == "lease"]
    assert leases and leases[0]["fields"]["notice_deadline"] == "2026-12-01"
    assert leases[0]["fields"]["renewal_date"] == "2027-03-01"


def test_the_timesheet_week_has_one_short_day():
    """The week the example prompt names: 7 September 2026, every entry in its own ISO week, and by Thursday the 10th
    only Wednesday short."""
    by_day = {}
    for row in sheet("timesheets")["records"]:
        if row["resource"] != "entry":
            continue
        fields = row["fields"]
        year, week, _ = date.fromisoformat(fields["date"]).isocalendar()
        assert fields["week"] == f"{year}-W{week:02d}", fields
        by_day[fields["date"]] = by_day.get(fields["date"], Decimal(0)) + Decimal(fields["hours"])
    assert sorted(day for day, hours in by_day.items() if hours < 8) == ["2026-09-09"]


def test_every_sample_file_is_small_and_plain():
    for agent_id in WITH_SAMPLES:
        for item in sheet(agent_id).get("files") or []:
            path = AGENTS[agent_id] / item["path"]
            assert path.stat().st_size < 200_000, path
            assert file_type(path.name) != "application/octet-stream", path
