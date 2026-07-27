# Dashboard Platform Workflow Guide

## Base states

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

Not every task has to pass through every state. The approval and external-execution ordering, however, cannot be skipped.

## Allowed transitions

| Current | Next |
|---|---|
| `collected` | `analyzing`, `draft`, `needs_review`, `failed` |
| `analyzing` | `draft`, `needs_review`, `failed` |
| `draft` | `needs_review`, `revision_requested`, `failed` |
| `needs_review` | `approved`, `rejected`, `revision_requested`, `failed` |
| `approved` | `queued`, `completed`, `revision_requested` |
| `rejected` | `revision_requested` |
| `revision_requested` | `analyzing`, `draft`, `needs_review`, `failed` |
| `queued` | `executing`, `failed` |
| `executing` | `completed`, `failed` |
| `failed` | `queued`, `analyzing` |
| `completed` | none |

## Approval rules

- An approval request targets exactly one immutable artifact version.
- At request time, its SHA-256 is copied and bound to the approval target.
- The actual file hash is re-checked immediately before a decision is recorded.
- An `approved`, `rejected`, or `revision_requested` decision is recorded exactly once.
- To change the outcome, create a new artifact and a new approval request.

## Artifact revisions

- `series_id`: the logical identity of a result across revisions.
- `version`: an immutable version number, starting at 1 and increasing.
- `artifact_id`: the unique identifier of one specific version.

An agent handling a revision request keeps the existing `series_id` and creates a new version. A new approval request must always reference a new `artifact_id`.

## Audit events

These events are recorded by default:

- `inbox.collected`
- `task.created`
- `task.updated`
- `task.status_changed`
- `artifact.created`
- `approval.requested`
- `approval.approved`
- `approval.rejected`
- `approval.revision_requested`

Events are never edited. A mistaken change is corrected with a new status-change event, not by altering history.

## External execution gate

The v1 API never performs a real publish or external order itself. Any future `actions` API must verify all of the following first:

1. The task is `approved` or `queued`.
2. The approval status is `approved`.
3. The target artifact's ID and hash match the approval record.
4. The executing service's token has the minimum required scope.
5. The same idempotency key hasn't already triggered this execution.
