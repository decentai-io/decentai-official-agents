"""The reference agent, its program started and spoken to in the standard's messages, against records kept in memory. Each test pins one feature the manifest declares.
"""

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

from tests.harness import REPOSITORY, Agents, probe, requirements


ROOT = Path(__file__).resolve().parents[1] / "agents"
#: The platform's own checks of a manifest, in its checkout beside this repository.
#: Cloned as decentai-engine, or under its earlier name, decentai.
#: The platform is closed source: where its checkout is not beside this one, these checks are skipped.
PLATFORM_MANIFEST = next((found for name in ("decentai-engine", "decentai")
                          for found in (REPOSITORY.parent / name / "src" / "api_server" / "agents" / "manifest.py",
                                        REPOSITORY.parent / name / "api_server" / "src" / "agents" / "manifest.py")
                          if found.is_file()), None)



def standard():
    """The platform's Standard, loaded by its file's path as an install's check runs it."""
    if PLATFORM_MANIFEST is None:
        pytest.skip("the platform's own manifest checks need its checkout beside this repository")
    spec = importlib.util.spec_from_file_location("platform_manifest", PLATFORM_MANIFEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Standard


def test_the_catalog_offers_one_agent_that_implements_its_manifest(agents):
    """Verified the way installation verifies: the program started by
    itself says ready, naming exactly the manifest's functions."""
    assert set(agents) == {"note", "gmail", "google_calendar", "google_drive", "outlook", "onedrive", "dropbox", "teams", "slack", "notion", "documents", "tasks", "todoist", "timesheets", "sheets", "expenses", "places", "web_reader", "web_watch", "applications", "connection_check", "mail", "code_runner"}
    agent = agents["note"]
    errors = probe(agent)
    assert errors == [], errors


def test_every_agent_explains_itself_by_example():
    """Three prompts a person can send as they are, with a title each,
    in every manifest — what the agent's page shows first."""
    for agent_id, folder in Agents().folders.items():
        doc = yaml.safe_load((folder / "manifest.yaml").read_text(encoding="utf-8"))
        examples = doc["agent"].get("examples") or []
        assert len(examples) >= 3, f"{agent_id} offers {len(examples)} examples"
        for example in examples:
            assert example["title"].strip() and example["prompt"].strip(), agent_id
            assert len(example["prompt"]) <= 500, agent_id


def test_every_manifest_is_valid_and_every_folder_builds_the_same_bytes():
    """What a deployment reads, and what makes a version stand: each
    manifest passes the platform's checks, and each dependency is one
    exact version."""
    checks = standard()
    for agent_id, folder in Agents().folders.items():
        doc = yaml.safe_load((folder / "manifest.yaml").read_text(encoding="utf-8"))
        assert checks.manifest_errors(doc) == [], agent_id
        assert doc["agent"]["id"] == agent_id
        for requirement in requirements(folder):
            assert re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+-]+", requirement), (agent_id, requirement)
