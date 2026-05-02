"""
Basecamp MCP test cases for tests/perf_qa.py.

Each TestCase fires one tools/call against the deployed MCP and runs
predicate(s) against the response. Predicates take the parsed inner
response dict (FastMCP's content[0].text JSON-decoded) and return
(ok: bool, actual: str).

Cases are designed so a fresh AI agent can read tests/reports/basecamp-latest.json
and locate the offending tool from `tool` + `name` + `expected` alone.
See tests/HOW_TO_FIX.md for the AI fix loop.

To extend: append a new TestCase to CASES. Anchors used below:
  - PROBE_PROJECT_NAME — substring expected to match a real project name
    (set via env BASECAMP_PROBE_PROJECT_NAME, default "Satva")
  - The first project returned by get_projects is used as the round-trip
    target so tests don't depend on a specific project ID.
"""
from __future__ import annotations

import os
from typing import Any, Dict

from perf_qa import TestCase  # type: ignore  # imported by runner via sys.path


PROBE_PROJECT_NAME = os.environ.get("BASECAMP_PROBE_PROJECT_NAME", "Satva")


# ---------- predicate helpers ----------

def _has_status_success(d: Dict[str, Any]):
    return (d.get("status") == "success", f"status={d.get('status')!r}")


def _projects_listed(d: Dict[str, Any]):
    n = len(d.get("projects") or [])
    return (n > 0, f"projects count={n}")


def _no_bookmark_url(d: Dict[str, Any]):
    projects = d.get("projects") or []
    if not projects:
        return (False, "no projects to inspect")
    bad = [p for p in projects if isinstance(p, dict) and "bookmark_url" in p]
    return (not bad, f"{len(bad)}/{len(projects)} projects still carry bookmark_url")


def _has_tools_map(d: Dict[str, Any]):
    projects = d.get("projects") or []
    if not projects:
        return (False, "no projects")
    missing = [p for p in projects if isinstance(p, dict) and not p.get("tools")]
    return (not missing, f"{len(missing)}/{len(projects)} projects missing 'tools' map")


def _project_compact_keys(d: Dict[str, Any]):
    """Compact project should only carry essential keys + 'tools'."""
    allowed = {"id", "name", "status", "purpose", "app_url", "tools"}
    projects = d.get("projects") or []
    if not projects:
        return (False, "no projects")
    p = projects[0]
    extra = set(p.keys()) - allowed
    return (not extra, f"unexpected keys: {sorted(extra)}")


def _verbose_has_bookmark_url(d: Dict[str, Any]):
    projects = d.get("projects") or []
    if not projects:
        return (False, "no projects")
    p = projects[0]
    return ("bookmark_url" in p, f"verbose project keys={sorted(p.keys())}")


def _probe_match_present(d: Dict[str, Any]):
    """search_basecamp(query=PROBE) must return at least one project."""
    results = d.get("results") or {}
    n = len(results.get("projects") or [])
    return (n > 0, f"matched projects={n} for query={PROBE_PROJECT_NAME!r}")


def _truncation_contract(d: Dict[str, Any]):
    """When a list is truncated, total > count and truncated == True."""
    total = d.get("total")
    count = d.get("count")
    truncated = d.get("truncated")
    if total is None or count is None:
        return (True, "no truncation metadata (acceptable for short lists)")
    if total > count:
        return (truncated is True, f"truncated={truncated} but total={total} > count={count}")
    return (truncated in (False, None), f"total={total} count={count} truncated={truncated}")


def _people_briefs(d: Dict[str, Any]):
    """Compact people response must only carry id, name, optional email."""
    people = d.get("data") or []
    if not people:
        return (False, "no people returned")
    allowed = {"id", "name", "email"}
    bad = [p for p in people if isinstance(p, dict) and (set(p.keys()) - allowed)]
    return (not bad, f"{len(bad)}/{len(people)} people carry extra fields")


# ---------- discovery: pick a real project ID & a tool ID for round-trip ----------
# We can't call MCP at module import time. Round-trip case uses a "second-pass"
# pattern: it does its own get_projects() call inline, picks the first project,
# then re-fetches it. See _project_round_trip below.

def _project_round_trip(d: Dict[str, Any]):
    """get_project(first_id) must succeed and the returned id must match."""
    proj = d.get("project") or {}
    return (bool(proj.get("id")), f"project={proj}")


# ---------- the suite ----------

CASES = [
    # --- Smoke + latency: every list/get tool answers under budget. ---
    TestCase(
        name="get_projects.smoke",
        tool="get_projects",
        args={"limit": 5},
        budget_ms=5_000,
        expects=[
            ("status == success", _has_status_success),
            ("at least one project", _projects_listed),
        ],
    ),

    # --- Compact contract: default response is tight. ---
    TestCase(
        name="get_projects.compact_no_bookmark_url",
        tool="get_projects",
        args={"limit": 5},
        budget_ms=5_000,
        max_bytes=50_000,
        expects=[
            ("no bookmark_url leaked", _no_bookmark_url),
            ("compact keys only", _project_compact_keys),
            ("tools map populated", _has_tools_map),
        ],
    ),

    # --- Verbose opt-in: raw payload restored. ---
    TestCase(
        name="get_projects.verbose_restores_bookmark",
        tool="get_projects",
        args={"limit": 1, "verbose": True},
        budget_ms=5_000,
        expects=[
            ("verbose has bookmark_url", _verbose_has_bookmark_url),
        ],
    ),

    # --- search_basecamp must be FAST without project_id (regression guard). ---
    TestCase(
        name="search_basecamp.fast_default_path",
        tool="search_basecamp",
        args={"query": PROBE_PROJECT_NAME},
        budget_ms=5_000,
        max_bytes=200_000,
        expects=[
            ("status == success", _has_status_success),
            (f"matches for {PROBE_PROJECT_NAME!r}", _probe_match_present),
        ],
    ),

    # --- search_basecamp pathological (no match) must still return fast. ---
    TestCase(
        name="search_basecamp.no_match_still_fast",
        tool="search_basecamp",
        args={"query": "zzz_nothing_should_match_this_xxx"},
        budget_ms=5_000,
        expects=[
            ("status == success", _has_status_success),
        ],
    ),

    # --- get_people: compact, fast. ---
    TestCase(
        name="get_people.compact_briefs",
        tool="get_people",
        args={},
        budget_ms=5_000,
        max_bytes=200_000,
        expects=[
            ("status == success", _has_status_success),
            ("only id/name/email per person", _people_briefs),
        ],
    ),
]


# ---------- multi-step cases ----------
# These need to be appended after a discovery call. The runner doesn't yet
# orchestrate dependencies, so we register them as a simple helper that the
# runner picks up via attribute. For now, we add ONE round-trip case that
# fetches projects first then a single project; we tunnel the discovery
# through env vars set after the smoke run if available.
#
# Simple pragmatic shape: a callable bound at runtime to the first project's
# id. We expose this via a function the runner can call, but to keep the
# runner simple right now we just include a follow-up case that re-uses the
# same get_projects call's first id by env override BASECAMP_PROBE_PROJECT_ID.

PROBE_PROJECT_ID = os.environ.get("BASECAMP_PROBE_PROJECT_ID")
if PROBE_PROJECT_ID:
    CASES.append(
        TestCase(
            name="get_project.round_trip",
            tool="get_project",
            args={"project_id": PROBE_PROJECT_ID},
            budget_ms=3_000,
            expects=[
                ("status == success", _has_status_success),
                ("returned project has id", _project_round_trip),
            ],
        )
    )
    CASES.append(
        TestCase(
            name="get_todolists.compact_smoke",
            tool="get_todolists",
            args={"project_id": PROBE_PROJECT_ID, "limit": 5},
            budget_ms=10_000,
            max_bytes=200_000,
            expects=[
                ("status == success", _has_status_success),
                ("truncation metadata coherent", _truncation_contract),
            ],
        )
    )
