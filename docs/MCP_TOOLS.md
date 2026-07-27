# Dashboard Platform MCP A–Z Guide

This single document covers installing and connecting the Dashboard Platform MCP server, then everything from creating a task to saving results, requesting approval, checking history, and recovering from errors. Any agent connecting for the first time should call `read_mcp_guide` once before calling any other tool.

## 1. What the MCP server does

The Dashboard MCP server never touches the database or files directly. Every request is translated into an authenticated call to the Dashboard Platform API.

```text
Agent
  → local stdio MCP server
  → Dashboard Platform API (HTTP or HTTPS)
  → DB / immutable artifact storage / audit events
```

So the MCP server inherits the exact same Bearer-token scopes, state transitions, idempotency, approval verification, and audit trail as the API itself.

## 2. Address and transport

| Use | Address |
|---|---|
| Default local API | `http://localhost:8000/api/v1` |
| Local API by IP | `http://127.0.0.1:8000/api/v1` |

Transport is local `stdio` — your agent CLI runs the Python MCP process directly, and that process calls the HTTP(S) API above. There is no separately exposed MCP network port.

Files involved (paths relative to wherever you cloned this repo):

```text
MCP server        integrations/dashboard_platform_mcp.py
Guide documents   docs/
Token files       wherever you choose to store them, e.g. ~/.dashboard/tokens/*.token
```

## 3. Preconditions

Check all of these before registering the MCP server:

```bash
curl --fail --silent --show-error http://localhost:8000/api/health/

test -x "$(command -v python3)"
test -f /path/to/slack-dashboard/integrations/dashboard_platform_mcp.py
test -f "$HOME/.dashboard/tokens/default.token"
```

The health response should be `{"status":"ok"}`. Keep the token file at `0600` and its parent directory at `0700`.

If you're running behind a reverse proxy with TLS, set `DASHBOARD_API_URL` to the `https://` address and `DASHBOARD_API_CA_CERT` to your CA bundle if it's not in the system trust store.

## 4. Issuing tokens and scopes

An admin issues/rotates/revokes tokens from the dashboard's `/api-tokens` page (`http://localhost:8000/api-tokens`), or via the CLI:

```bash
python backend/manage.py issue_platform_token \
  --agent-key research-agent \
  --agent-name "Research Agent" \
  --token-name dashboard-platform \
  --scopes platform:read,tasks:write,artifacts:write,approvals:request \
  --capabilities research,analysis \
  --output "$HOME/.dashboard/tokens/research-agent.token"
```

| Scope | Allows |
|---|---|
| `platform:read` | read tasks, artifacts, approvals, events, agents, search |
| `inbox:write` | ingest external source material |
| `tasks:write` | create/update tasks, transition status |
| `artifacts:write` | create immutable artifact revisions |
| `approvals:request` | request approval of an artifact |
| `approvals:decide` | approve/reject/request changes |

Don't grant `approvals:decide` to a regular working agent — only a supervising/admin agent should decide approvals. Configuration should reference the token *file path*, never the token value itself.

## 5. Required environment variables

| Variable | Required | Value |
|---|---|---|
| `DASHBOARD_API_URL` | yes | e.g. `http://localhost:8000/api/v1` |
| `DASHBOARD_API_TOKEN_FILE` | yes | path to that agent's `0600` token file |
| `DASHBOARD_API_CA_CERT` | only if using a private CA over HTTPS | path to your CA bundle |
| `DASHBOARD_API_TIMEOUT` | no | API timeout in seconds, default 60 |

## 6. Registering with an agent CLI (example: Codex)

Check whether it's already registered:

```bash
codex mcp get dashboard_platform
```

If not registered:

```bash
codex mcp add dashboard_platform \
  --env DASHBOARD_API_URL=http://localhost:8000/api/v1 \
  --env DASHBOARD_API_TOKEN_FILE="$HOME/.dashboard/tokens/default.token" \
  --env DASHBOARD_API_TIMEOUT=60 \
  -- python3 \
  /path/to/slack-dashboard/integrations/dashboard_platform_mcp.py
```

To change a value, remove the exact registration and re-add it:

```bash
codex mcp remove dashboard_platform
```

Verify:

```bash
codex mcp get dashboard_platform
codex mcp list
```

After registering, open a new thread/session in your agent CLI so it picks up the new server. Tool names are typically exposed as `mcp__dashboard_platform__<tool_name>`. The same pattern (env vars, stdio command) applies to any other MCP-capable agent CLI (e.g. Claude Code's `claude mcp add`).

## 7. First-connection sequence

An agent should start in this order:

1. Call `read_mcp_guide()` to read the currently deployed version of this document.
2. Call `list_agents()` to see which agent keys are available.
3. Call `list_tasks(limit=5)` to confirm read access and the response shape.
4. Before starting real work, call `search_context(query)` to find existing context and avoid duplicate work.

Don't call a write tool until the read checks above have succeeded.

## 8. Full tool list

| Tool | Required scope | Key inputs | Result / side effect |
|---|---|---|---|
| `read_mcp_guide` | none | none | returns the currently deployed version of this guide |
| `search_context` | `platform:read` | `query`, `limit=20` | searches tasks and the knowledge base together |
| `list_agents` | `platform:read` | `limit=100` | lists active agent keys and capabilities |
| `list_tasks` | `platform:read` | `status`, `assigned_agent`, `limit`, `offset` | lists tasks |
| `collect_item` | `inbox:write` | `title`, `content`, `source_type`, optional `external_id`, `source_url` | ingests external source material |
| `create_task` | `tasks:write` | `title`, optional `description`, `inbox_item_id`, `assigned_agents`, `priority` | creates a task |
| `update_task_status` | `tasks:write` | `task_id`, `status` | records an allowed status transition |
| `get_task_context` | `platform:read` | `task_id` | returns source, revisions, approvals, and events together |
| `create_artifact` | `artifacts:write` | `task_id`, `kind`, `title`, `content`, optional `series_id`, `mime_type` | creates an immutable result revision |
| `submit_analysis` | `artifacts:write`, `tasks:write` | `task_id`, `title`, `content`, optional `series_id` | saves analysis and transitions to `needs_review` |
| `request_approval` | `approvals:request` | `task_id`, `artifact_id`, optional `note` | requests approval bound to a specific artifact hash |
| `decide_approval` | `approvals:decide` | `approval_id`, `decision`, optional `note` | records an admin approval decision |
| `get_workflow_history` | `platform:read` | `task_id` | returns current status and audit events |

`limit` defaults to 50, max 100. Use whatever `priority` values your own workflow policy defines. Assign work using the agent keys `list_agents` returns — not arbitrary strings.

## 9. Basic workflow A–Z

### 9.1 Check existing context

```text
search_context(query="key terms from the user's request", limit=20)
```

If a related task exists, read its full context with `get_task_context(task_id)` instead of creating a duplicate.

### 9.2 Ingest source material, if needed

```text
collect_item(
  title="Source title",
  content="Original text or a lossless summary",
  source_type="web",
  external_id="source-system-id",
  source_url="https://..."
)
```

Use a stable `external_id` when re-ingesting the same source repeatedly.

### 9.3 Create a task

```text
create_task(
  title="A task title that states its completion criteria",
  description="Expected outcome, constraints, and how to verify it",
  inbox_item_id="<optional>",
  assigned_agents=["research-agent"],
  priority="normal"
)
```

Use the response's `data.id` as `task_id` from here on.

### 9.4 Record progress

```text
update_task_status(task_id="<task-id>", status="analyzing")
```

Main status flow:

```text
collected
→ analyzing
→ draft
→ needs_review
→ approved | rejected | revision_requested
→ queued
→ executing
→ completed | failed
```

If you're unsure of the current status or which transitions are allowed, call `get_workflow_history` first.

### 9.5 Save results

Save long analyses, drafts, and deliverables as artifacts rather than leaving them only in chat.

```text
create_artifact(
  task_id="<task-id>",
  kind="analysis",
  title="Analysis result",
  content="<full markdown>",
  mime_type="text/markdown"
)
```

Keep the first response's `data.series_id` and `data.id`. A revision passes the existing `series_id` to create a new version — it never overwrites an existing one.

To save analysis and move to review in one call:

```text
submit_analysis(
  task_id="<task-id>",
  title="Analysis result",
  content="<full markdown>",
  series_id="<only when revising>"
)
```

### 9.6 Request approval

```text
request_approval(
  task_id="<task-id>",
  artifact_id="<artifact-id>",
  note="Review criteria and risks to check"
)
```

The approval request is bound to the exact artifact version and its SHA-256 hash.

### 9.7 Admin decision

An admin should first call `get_task_context` to review the source, every revision, and the event history.

```text
decide_approval(
  approval_id="<approval-id>",
  decision="approved",
  note="Basis for the decision"
)
```

`decision` is one of `approved`, `rejected`, `revision_requested`. A regular agent should not call this tool.

### 9.8 Confirm completion

```text
get_task_context(task_id="<task-id>")
get_workflow_history(task_id="<task-id>")
```

Completion criteria:

- The full result is saved as an artifact.
- Any required approval decision is recorded.
- The task status matches reality.
- The event log alone is enough to reconstruct who did what.

## 10. Handling a revision request

1. Read the approval note and the target artifact from `get_task_context`.
2. Record the transition with `update_task_status(task_id, "analyzing")`.
3. Call `create_artifact` or `submit_analysis` again, keeping the existing `series_id`.
4. Create a new `request_approval` pointing at the new `artifact_id`.
5. Never modify past artifacts or approval records.

## 11. Safety rules

- Never modify the dashboard's database or `platform-artifacts` storage directly.
- Never put a raw token in output, a prompt, Slack, git, logs, or a URL.
- Never trigger an external side effect (publishing, sending, placing an order, etc.) without an approved, exact artifact ID and hash.
- Save full results as artifacts; keep chat to a summary of task/artifact IDs and status.
- Only grant `approvals:decide` to admin-scoped tokens.
- The MCP server generates a UUID idempotency key automatically for every `POST`.

## 12. Guide resources

| URI | Content |
|---|---|
| `dashboard://guides/mcp` | this A–Z document |
| `dashboard://guides/agent` | mandatory agent operating rules |
| `dashboard://guides/api` | REST API authentication and contract |
| `dashboard://guides/workflow` | state transitions, revisions, and approval policy |

Clients without resource-reading support should use the `read_mcp_guide` tool instead.

## 13. Errors and recovery

| Symptom | Cause | Fix |
|---|---|---|
| MCP server missing from the list | client not registered, or an existing session predates registration | register, then start a new agent session |
| Exits immediately after starting | wrong Python, server file, or env var path | check the files in section 3, then re-register with absolute paths |
| "cannot connect to the Dashboard API" | service down, wrong URL, or CA mismatch | check `/api/health/`, `DASHBOARD_API_URL`, `DASHBOARD_API_CA_CERT` |
| `401 invalid_token` | file mismatch, expired, rotated, or revoked | check status on `/api-tokens`, then point at the correct file |
| `403 insufficient_scope` | the tool needs a scope this token lacks | swap in a token with the right scope, or delegate to an admin |
| `404` | wrong task, artifact, or approval ID | re-check with `search_context`, `list_tasks`, `get_task_context` |
| `409 invalid_transition` | disallowed status transition | check the current status via `get_workflow_history` |
| `409 approval_target_changed` | the approval target's file hash no longer matches | save the change as a new artifact and request approval again |
| `409 idempotency_conflict` | same key, different body | retry explicitly with a new request |
| timeout | slow API operation or network | check status, then raise `DASHBOARD_API_TIMEOUT` only if actually needed |

Token files are read when the MCP process starts. After rotating a token, restart the corresponding MCP session (or any long-running gateway process) in your agent CLI.

## 14. Operational health check

```bash
codex mcp get dashboard_platform
codex mcp list

curl --fail --silent --show-error http://localhost:8000/api/health/
```

Healthy state looks like:

- `dashboard_platform` is enabled in your agent CLI.
- The MCP connection test discovers tools and resources.
- `read_mcp_guide`, `list_agents`, `list_tasks` all succeed.
- The raw token never appears in configuration, output, or logs.

## 15. Minimal instructions for an agent

An agent can get started from just this:

```text
Only use the dashboard_platform MCP server for Dashboard Platform work.
Start by reading read_mcp_guide, then check context with list_agents and
search_context. Save results as immutable artifacts, and never trigger an
external side effect before the exact artifact has been approved. Never
access the raw token or the dashboard's storage directly.
```
