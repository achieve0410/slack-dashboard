# Dashboard-Centered Platform Vision

> Status: long-term target architecture, current state reflects the v1 platform API
> Written: 2026-07-19

## Scope

This document isn't a design for exposing the current dashboard database as-is. It defines the platform contract meant to stay stable even as the domains and implementations behind it change.

### Current features (AS-IS)

- Slack question/answer knowledge items
- Slack-sourced content and its classification
- Schedule/TODO items

The legacy `/api/*` stays as a compatibility API for the dashboard UI. External services and agents should use the newer `/api/v1/*`.

### v1 platform API (MVP)

- Scoped Bearer tokens with agent identity
- Inbox source ingestion
- A shared Task model with status transitions
- File-backed immutable Artifact revisions
- Approve/reject/request-changes bound to an artifact hash
- An audit Event log for every change
- Search across both platform data and the existing knowledge base
- An MCP adapter built entirely on top of the above API

### Possible future extensions

- A Source registration/sync policy
- A dedicated Idea model and content-editing flow
- An Action executor for publishing/sending/ordering
- Webhook/event subscriptions
- Domain-specific UIs, and migrating the existing dashboard UI onto `/api/v1`
- Finer-grained service accounts and automatic token rotation

## Goal

Treat this dashboard as more than a read-only viewer — as a personal operations hub for reviewing, approving, and tracking the full history of agent work and any other external inputs you wire into it.

## Basic shape

```text
External data & services
  Slack · other sources you add · collectors · executors
                         |
                         v
                  Dashboard API
                         |
   collect → organize → analyze → review → approve → execute → verify
                         |
                         v
                data · files · history
                         ^
                         |
                Dashboard UI · MCP
```

## Design principles

1. Treat the Dashboard API as the official interface and the contract between systems.
2. The UI and any other service should go through the same API rather than touching the database or shared folders directly, wherever possible.
3. Keep MCP as an agent-facing adapter built on top of the Dashboard API — not a second way in.
4. Record every state change (collection, analysis, approval, execution, failure, retry) as an auditable event.
5. Keep long content in files or artifact storage; store identifiers, paths, versions, and metadata in the database.
6. Every task and external execution should be idempotent and run under the minimum scope it needs.
7. Anything with an external side effect (publishing, sending, ordering) must pass through an explicit approval step.
8. Record the approved version/hash alongside the decision, so content can't silently change after approval.

## Shared workflow

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

Example domain-specific flows this is meant to generalize:

- Content drafting: idea → draft → review → publish approval → publish → verify result
- Research: gather sources → classify and summarize → surface ideas → knowledge or content draft

## Draft API surface

```text
/api/v1/inbox       ingested source material
/api/v1/sources     where content comes from
/api/v1/artifacts   files, analyses, drafts, results, and their versions
/api/v1/ideas       ideas
/api/v1/tasks       agent tasks
/api/v1/workflows   process flow and status
/api/v1/approvals   approve / reject / request changes
/api/v1/actions     execution requests (publish, order, etc.)
/api/v1/events      change and audit history
/api/v1/agents      agents, their capabilities and permissions
/api/v1/search      unified search
```

The v1 implementation ships `inbox`, `artifacts`, `tasks`, `workflows`, `approvals`, `events`, `agents`, and `search`. `sources`, `ideas`, and `actions` will be added once a shared execution contract and external adapters for them exist.

The API uses OpenAPI 3.1, `/api/v1` versioning, auth + scopes, idempotency keys, pagination, filtered search, and is designed to eventually support webhooks/event subscriptions.

## Draft MCP surface

MCP tools are scoped to agent-sized units of work rather than mirroring the API 1:1:

- `search_context`
- `get_task_context`
- `collect_item`
- `create_artifact`
- `submit_analysis`
- `create_draft`
- `request_approval`
- `revise_artifact`
- `execute_approved_action`
- `report_execution_result`
- `get_workflow_history`

`get_task_context` is meant to return, in one call, the original request, related material, prior analysis and revision history, current status, approval state, allowed tools, and the expected result format.

## Related documents

- `openapi.yaml` — machine-readable API spec
- `API_GUIDE.md` — auth, errors, pagination, and call examples
- `AGENT_GUIDE.md` — rules from reading a task through submitting results
- `MCP_TOOLS.md` — a single A–Z guide covering install through operations and recovery
- `WORKFLOW_GUIDE.md` — state transitions and approval policy
- `examples/` — worked examples

All v1 implementation docs live in this directory and are versioned alongside the API. Update docs in the same change that changes API/MCP input or output.

## Suggested implementation order

1. Settle the shared task/event/artifact/approval data shapes.
2. Build the internal API and its OpenAPI spec.
3. Treat the dashboard UI as the first client of that same API.
4. Connect any agent CLIs you use.
5. Connect additional source types as you need them.
6. Wire up an external publish/order executor behind the approval gate.
7. Expand search, audit history, stats, and other operational features.

## Compatibility and migration policy

1. Run the legacy `/api/*` and `/api/v1/*` side by side.
2. New external integrations should use only `/api/v1/*` or the Dashboard MCP server.
3. External services modifying the database or `platform-artifacts` files directly is not supported.
4. Bring existing Slack-derived data into the platform model via adapters or backfill jobs as needed.
5. Make breaking changes to `/api/v1` only in a new API version.
