# Dashboard Platform Agent Guide

## Mandatory rules

1. Read and write dashboard-owned data only through the `dashboard_platform` MCP server or `/api/v1`.
2. Never modify the dashboard's database or `platform-artifacts` directory directly.
3. Save long analyses, answers, and drafts with `create_artifact` — not as chat text.
4. Never overwrite an existing artifact; create a new revision under the same `series_id`.
5. Never trigger an external side effect (publishing, sending, placing an order, etc.) without an approved artifact.
6. Never print a raw token, or include one in a prompt or a result file.

## Starting work

To find existing context:

1. `search_context`
2. If a related task exists, `get_task_context`
3. Otherwise, `collect_item` or `create_task`

When creating a task, state the title, expected outcome, and assigned agent. Use the `key` values returned by `list_agents` — never an arbitrary name.

## Doing the work

1. `update_task_status(task_id, "analyzing")`
2. Gather and analyze whatever material is needed.
3. Save the full result with `create_artifact` or `submit_analysis`.
4. For a follow-up revision, pass the existing response's `series_id`.
5. Call `request_approval` if review is required.

Keep Slack (or any chat surface) to a brief status + task/artifact ID; the full result should be read from the dashboard.

## Admin agents

An admin should review in this order:

1. Read the source, every revision, and the event log via `get_task_context`.
2. Verify the target artifact's content and SHA-256.
3. Decide `revision_requested` if it needs more work, `rejected` if it's not acceptable, or `approved` if it's sufficient.
4. For work that requires external execution, hand the executor the approved artifact's exact ID and hash.

An admin should never overwrite a worker's answer directly — even when making the fix themselves, create a new artifact revision.

## Error handling

- `401 invalid_token`: check the token file and whether it's expired or revoked.
- `403 insufficient_scope`: ask an admin for a token with the right scope, or delegate the work.
- `409 invalid_transition`: check the current status and allowed transitions via `get_workflow_history`.
- `409 approval_target_changed`: don't approve a changed file — submit a new artifact instead.
- `409 idempotency_conflict`: don't reuse the same key — retry explicitly with a new one.

## Completion criteria

- The result is saved as an artifact.
- Any required approval decision is recorded.
- The task status matches reality.
- The event history alone is enough to reconstruct who did what.
