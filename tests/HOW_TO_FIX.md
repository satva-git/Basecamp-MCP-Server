# Fixing failures from `tests/perf_qa.py`

This file is the **operating manual for an AI agent** consuming a failing
`perf_qa` run. It assumes you (the agent) have file-edit, shell, and Coolify
API access. The goal: turn `tests/reports/<mcp>-latest.json` into a green
run on the deployed MCP, autonomously.

## 0. Inputs you'll have

- `tests/reports/<mcp>-latest.json` — machine-readable report from the most
  recent harness run. Always overwritten after every run.
- `D:\oBot\forks\basecamp-mcp-server\` — checkout of the Basecamp MCP source
  (or the equivalent `forks/<mcp>` for other MCPs).
- `D:\oBot\.env` — `COOLIFY_API_KEY`, `OBOT_API_KEY`, `OBOT_BASE_URL`.
- The relevant Coolify app UUID (Basecamp: `mo08gg0coow440o4w4s4sw4k`).
- The connector ID (`OBOT_MCP_ID`) for that MCP in O-Bot
  (Basecamp: `default-satva-basecamp-4059cf41`).

## 1. Read the report

```python
import json, pathlib
report = json.loads(pathlib.Path("tests/reports/basecamp-latest.json").read_text())
fails = [r for r in report["results"] if r["status"] != "pass"]
```

Each failing entry contains:

| Field | What it tells you |
|---|---|
| `name` | The case identifier — pick the failing tool's source from this |
| `tool` | The MCP tool name → grep `basecamp_fastmcp.py` for `^async def <tool>` |
| `args` | The exact arguments that failed |
| `expected` / `actual` | Why the predicate failed |
| `elapsed_ms` / `budget_ms` | Time over budget if `expected` mentions latency |
| `response_bytes` / `max_bytes` | Size budget if `expected` mentions size |
| `raw_excerpt` | First ~800 chars of the parsed response (for diagnosis) |

## 2. Classify the failure

| `expected` says... | Classification | Likely root cause |
|---|---|---|
| `elapsed_ms <= N` | **Latency regression** | Tool fan-out, missing pagination cap, sync I/O blocking event loop |
| `response_bytes <= N` | **Size regression** | Slim helper missed; raw payload leaking through; new bulky field upstream |
| `no bookmark_url leaked` / `compact keys only` | **Compact contract regression** | Tool stopped routing through `_maybe_slim` / `slim_*` |
| `verbose has bookmark_url` | **Verbose mode broken** | `verbose=True` not threaded into `_maybe_slim` |
| `tools map populated` | **Slim helper bug** | `slim_project` mis-handling the dock array |
| `status == success` failed with `error=...` | **Tool-level exception** | Underlying API error or upstream Basecamp change |
| `no transport error` failed | **Transport / OAuth / hang** | Re-run smoke first; possibly OAuth token expired or proxy down |
| Custom case label | Read the predicate in `tests/cases/<mcp>.py` |

## 3. Standard fix loop

```
1. Locate tool in source:
     grep -n "^async def <tool>" basecamp_fastmcp.py
2. Read enough surrounding context to understand the current shape.
3. Make the SMALLEST change that addresses the root cause.
4. Syntax check:
     python -c "import ast; ast.parse(open('basecamp_fastmcp.py').read())"
5. Commit:
     git add basecamp_fastmcp.py
     git commit -m "fix(<tool>): <one-line root cause>"
     git push origin main
6. Trigger Coolify redeploy:
     curl -H "Authorization: Bearer $COOLIFY_API_KEY" \
       "$COOLIFY_BASE_URL/api/v1/deploy?uuid=mo08gg0coow440o4w4s4sw4k&force=false"
7. Poll deployment until status=finished.
8. Rerun harness:
     python tests/perf_qa.py --tool <tool>     # narrow first
     python tests/perf_qa.py                   # then full suite
9. If still failing, treat the new report as the next iteration's input.
   STOP after 3 failed attempts on the same tool — escalate to human; this
   indicates an architectural problem, not a fix to apply.
```

## 4. Patterns we've already seen (don't re-discover)

| Failure | Root cause | Fix shape |
|---|---|---|
| `search_basecamp` hung > 60s, surfaced as "Error occurred during tool execution" | `search_basecamp` without `project_id` fanned out across every project sequentially fetching todolists, todos, and per-message details | Restrict the default path to `search_projects` (filter cached project list); keep `global_search` for users who want the slow path. Commit `2cdd20a` |
| `get_projects` response too large for LLM context | Raw payload leaked `bookmark_url`, full `dock`, and other internal fields | Route through `_maybe_slim` + `slim_project` by default; opt back in via `verbose=True`. Commit `a4f3570` |

If a new failure rhymes with any of the above, suspect the same shape in
the offending tool before looking elsewhere.

## 5. When NOT to fix

- **Smoke fail across every tool**: the deployment itself is broken. Check
  `GET /api/v1/applications/<uuid>` status, then container logs. Don't
  patch tool code.
- **OAuth-expired error in `actual`**: a user's token expired. Not a code
  bug — re-auth is a user action.
- **Latency budget seems wrong, not the tool**: rerun 3× to rule out
  cold-cache. Only adjust budgets in `tests/cases/<mcp>.py` after
  confirming the upstream API itself can't go faster.

## 6. Adding new MCPs to the harness

To wire e.g. Gmail:

1. Drop `tests/cases/gmail.py` mirroring `basecamp.py` shape.
2. Set `OBOT_MCP_ID` to Gmail's connector id and run
   `python tests/perf_qa.py --cases gmail`.

The runner is MCP-agnostic; only the cases file changes.
