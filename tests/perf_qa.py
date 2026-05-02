#!/usr/bin/env python3
"""
Basecamp MCP performance + QA harness.

Drives the deployed MCP through O-Bot's mcp-connect proxy (the same path the
production agent uses) and produces a machine-readable JSON report. Failing
cases include enough context for a coding agent (Claude, gsd-debug, etc.) to
locate and fix the offending tool. See tests/HOW_TO_FIX.md for the loop.

Usage:
    python tests/perf_qa.py                       # all cases for the default mcp
    python tests/perf_qa.py --cases basecamp      # explicit case file
    python tests/perf_qa.py --tool get_projects   # filter by tool name
    python tests/perf_qa.py --report path.json    # explicit report path
    python tests/perf_qa.py --direct              # bypass O-Bot, hit subdomain
    python tests/perf_qa.py --no-color            # plain text output

Env required (or via .env / project root .env):
    OBOT_BASE_URL=https://obot.satva.xyz
    OBOT_API_KEY=ok1-...
    OBOT_MCP_ID=default-satva-basecamp-4059cf41
For --direct mode also set:
    BASECAMP_MCP_DIRECT_URL=https://basecampmcp.satva.xyz/mcp/basecamp/

Exit code: 0 if every case passes, 1 if any fails.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))


# ---------------------------------------------------------------------------
# Test case model
# ---------------------------------------------------------------------------

@dataclass
class TestCase:
    """One assertion against one MCP tool call.

    `expects` is a list of (label, predicate). Each predicate takes the parsed
    response dict (the inner JSON returned by the tool, NOT the JSON-RPC
    envelope) and returns (ok: bool, actual: str). All must pass.
    """
    name: str
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    budget_ms: int = 10_000
    max_bytes: Optional[int] = None
    expects: List[Any] = field(default_factory=list)  # list[(str, Callable)]

    def assertions(self):
        return list(self.expects)


@dataclass
class CaseResult:
    name: str
    tool: str
    args: Dict[str, Any]
    status: str            # "pass" | "fail" | "error"
    elapsed_ms: int
    budget_ms: int
    response_bytes: int
    expected: str = ""
    actual: str = ""
    raw_excerpt: str = ""


# ---------------------------------------------------------------------------
# MCP client (stateless-http, JSON-RPC over the O-Bot proxy)
# ---------------------------------------------------------------------------

class MCPError(Exception):
    pass


class MCPClient:
    """Minimal JSON-RPC client for the streamable-HTTP MCP transport.

    Establishes one session via initialize → notifications/initialized, then
    issues tools/call requests. Reuses the same Mcp-Session-Id across calls.
    """
    def __init__(self, url: str, api_key: Optional[str] = None, timeout: float = 60.0):
        self.url = url
        self.api_key = api_key
        self.timeout = timeout
        self._session_id: Optional[str] = None

    # ------- low-level -------

    def _post(self, payload: dict, expect_session: bool = False) -> tuple[int, Dict[str, str], bytes]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        # Single-shot JSON, not SSE — urllib has no SSE parser and would block.
        req.add_header("Accept", "application/json, text/event-stream")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        if self._session_id and not expect_session:
            req.add_header("Mcp-Session-Id", self._session_id)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ctype = resp.headers.get("Content-Type", "")
                if "text/event-stream" in ctype:
                    # Read SSE frames until the first "data:" with a JSON-RPC
                    # response, then stop. Avoids hanging on long-lived streams.
                    data_lines: List[str] = []
                    raw = bytearray()
                    while True:
                        line = resp.readline()
                        if not line:
                            break
                        raw.extend(line)
                        s = line.decode("utf-8", "replace").rstrip("\r\n")
                        if s.startswith("data:"):
                            data_lines.append(s[5:].lstrip())
                        elif s == "" and data_lines:
                            break
                    body_out = "\n".join(data_lines).encode("utf-8") if data_lines else bytes(raw)
                    return resp.status, dict(resp.headers), body_out
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read() or b""

    def initialize(self) -> None:
        status, headers, body = self._post({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "perf_qa", "version": "1"},
            },
        }, expect_session=True)
        if status != 200:
            raise MCPError(f"initialize HTTP {status}: {body[:300]!r}")
        sid = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        if not sid:
            # Stateless servers may not return one; fall back to a random ID.
            sid = uuid.uuid4().hex
        self._session_id = sid
        # initialized notification
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> tuple[Dict[str, Any], int]:
        """Returns (parsed_inner_payload, response_size_bytes).

        FastMCP wraps tool results as result.content[0].text where text is a
        JSON-encoded string. We parse that into a dict (or list).
        """
        rpc_id = uuid.uuid4().int & 0x7FFF_FFFF
        status, _, body = self._post({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        if status != 200:
            raise MCPError(f"tools/call HTTP {status}: {body[:300]!r}")
        envelope = json.loads(body.decode("utf-8"))
        if envelope.get("error"):
            raise MCPError(f"tools/call JSON-RPC error: {envelope['error']}")
        result = envelope.get("result") or {}
        content = result.get("content") or []
        if not content:
            raise MCPError(f"tools/call returned empty content: {envelope}")
        first = content[0]
        if first.get("type") != "text" or "text" not in first:
            raise MCPError(f"unexpected content shape: {first}")
        try:
            inner = json.loads(first["text"])
        except json.JSONDecodeError:
            inner = {"_raw_text": first["text"]}
        return inner, len(body)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def load_dotenv(path: Path) -> None:
    """Tiny .env loader (no python-dotenv dep)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def build_url(direct: bool) -> tuple[str, Optional[str]]:
    if direct:
        url = os.environ.get("BASECAMP_MCP_DIRECT_URL")
        if not url:
            raise SystemExit("--direct requires BASECAMP_MCP_DIRECT_URL")
        return url, None  # direct mode: no bearer (server uses oauth-proxy session)
    base = os.environ.get("OBOT_BASE_URL", "https://obot.satva.xyz").rstrip("/")
    mcp_id = os.environ.get("OBOT_MCP_ID")
    if not mcp_id:
        raise SystemExit("OBOT_MCP_ID env var required (catalog entry id)")
    api_key = os.environ.get("OBOT_API_KEY")
    if not api_key:
        raise SystemExit("OBOT_API_KEY env var required")
    return f"{base}/mcp-connect/{mcp_id}/", api_key


def excerpt(obj: Any, limit: int = 800) -> str:
    try:
        text = json.dumps(obj, indent=2, default=str)
    except Exception:
        text = repr(obj)
    if len(text) > limit:
        text = text[:limit] + f"... [+{len(text) - limit} chars]"
    return text


def run_case(client: MCPClient, case: TestCase) -> CaseResult:
    started = time.perf_counter()
    response_bytes = 0
    try:
        inner, response_bytes = client.call_tool(case.tool, case.args)
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return CaseResult(
            name=case.name, tool=case.tool, args=case.args,
            status="error", elapsed_ms=elapsed_ms, budget_ms=case.budget_ms,
            response_bytes=response_bytes,
            expected="no transport error",
            actual=f"{type(e).__name__}: {e}",
            raw_excerpt="",
        )
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    # Built-in latency + size assertions.
    if elapsed_ms > case.budget_ms:
        return CaseResult(
            name=case.name, tool=case.tool, args=case.args,
            status="fail", elapsed_ms=elapsed_ms, budget_ms=case.budget_ms,
            response_bytes=response_bytes,
            expected=f"elapsed_ms <= {case.budget_ms}",
            actual=f"elapsed_ms = {elapsed_ms}",
            raw_excerpt=excerpt(inner),
        )
    if case.max_bytes is not None and response_bytes > case.max_bytes:
        return CaseResult(
            name=case.name, tool=case.tool, args=case.args,
            status="fail", elapsed_ms=elapsed_ms, budget_ms=case.budget_ms,
            response_bytes=response_bytes,
            expected=f"response_bytes <= {case.max_bytes}",
            actual=f"response_bytes = {response_bytes}",
            raw_excerpt=excerpt(inner),
        )

    # Tool-error envelope check (FastMCP returns isError flag in result).
    if isinstance(inner, dict) and inner.get("error"):
        return CaseResult(
            name=case.name, tool=case.tool, args=case.args,
            status="fail", elapsed_ms=elapsed_ms, budget_ms=case.budget_ms,
            response_bytes=response_bytes,
            expected="no tool error",
            actual=f"error={inner.get('error')!r} message={inner.get('message')!r}",
            raw_excerpt=excerpt(inner),
        )

    # Custom assertions.
    for label, predicate in case.assertions():
        try:
            ok, actual = predicate(inner)
        except Exception as e:
            return CaseResult(
                name=case.name, tool=case.tool, args=case.args,
                status="error", elapsed_ms=elapsed_ms, budget_ms=case.budget_ms,
                response_bytes=response_bytes,
                expected=label,
                actual=f"{type(e).__name__}: {e}",
                raw_excerpt=excerpt(inner),
            )
        if not ok:
            return CaseResult(
                name=case.name, tool=case.tool, args=case.args,
                status="fail", elapsed_ms=elapsed_ms, budget_ms=case.budget_ms,
                response_bytes=response_bytes,
                expected=label,
                actual=actual,
                raw_excerpt=excerpt(inner),
            )

    return CaseResult(
        name=case.name, tool=case.tool, args=case.args,
        status="pass", elapsed_ms=elapsed_ms, budget_ms=case.budget_ms,
        response_bytes=response_bytes,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Basecamp MCP performance + QA harness")
    ap.add_argument("--cases", default="basecamp", help="case module name under tests/cases (default: basecamp)")
    ap.add_argument("--tool", default=None, help="filter cases by tool name")
    ap.add_argument("--name", default=None, help="filter cases by case-name substring")
    ap.add_argument("--report", default=None, help="path to write JSON report (default: tests/reports/<cases>-<ts>.json)")
    ap.add_argument("--direct", action="store_true", help="bypass O-Bot, hit subdomain directly")
    ap.add_argument("--no-color", action="store_true", help="plain text output")
    args = ap.parse_args()

    load_dotenv(REPO_ROOT / ".env")

    url, api_key = build_url(args.direct)

    cases_module = importlib.import_module(f"cases.{args.cases}")
    cases: List[TestCase] = list(cases_module.CASES)
    if args.tool:
        cases = [c for c in cases if c.tool == args.tool]
    if args.name:
        cases = [c for c in cases if args.name in c.name]
    if not cases:
        print("no cases matched filter")
        return 1

    client = MCPClient(url, api_key=api_key)
    try:
        client.initialize()
    except Exception as e:
        print(f"INITIALIZE FAILED: {e}", file=sys.stderr)
        return 1

    started_at = datetime.now(timezone.utc).isoformat()
    suite_started = time.perf_counter()
    results: List[CaseResult] = []

    GREEN = "" if args.no_color else "\033[32m"
    RED = "" if args.no_color else "\033[31m"
    YEL = "" if args.no_color else "\033[33m"
    DIM = "" if args.no_color else "\033[2m"
    RESET = "" if args.no_color else "\033[0m"

    for c in cases:
        r = run_case(client, c)
        results.append(r)
        marker = {"pass": GREEN + "PASS", "fail": RED + "FAIL", "error": YEL + "ERR "}[r.status] + RESET
        print(f"{marker}  {r.name:<48} {r.elapsed_ms:>5}ms  {r.response_bytes:>7}B")
        if r.status != "pass":
            print(f"      {DIM}expected:{RESET} {r.expected}")
            print(f"      {DIM}actual:  {RESET} {r.actual}")

    duration_ms = int((time.perf_counter() - suite_started) * 1000)
    passed = sum(1 for r in results if r.status == "pass")
    failed = sum(1 for r in results if r.status == "fail")
    errored = sum(1 for r in results if r.status == "error")

    report = {
        "started_at": started_at,
        "mcp": args.cases,
        "via": "direct" if args.direct else f"obot:{os.environ.get('OBOT_MCP_ID')}",
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "errored": errored,
            "duration_ms": duration_ms,
        },
        "results": [asdict(r) for r in results],
    }

    report_path = Path(args.report) if args.report else (
        REPO_ROOT / "tests" / "reports" / f"{args.cases}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Symlink/copy "latest" for AI-driven fix loop.
    latest = report_path.parent / f"{args.cases}-latest.json"
    latest.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print()
    print(f"=== {passed}/{len(results)} passed, {failed} failed, {errored} errored — {duration_ms}ms ===")
    print(f"report: {report_path}")
    print(f"latest: {latest}")

    return 0 if (failed == 0 and errored == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
