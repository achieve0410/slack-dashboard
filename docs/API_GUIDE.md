# Dashboard Platform API Guide

## Basics

- Official API: `/api/v1/`
- Local address: `http://localhost:8000/api/v1/`
- Spec: [`openapi.yaml`](openapi.yaml)
- Legacy `/api/*`: used by the dashboard UI for backward compatibility; don't build new integrations against it.

Every `/api/v1/*` request uses a Bearer token. Unauthenticated calls are rejected. Put a TLS-terminating reverse proxy in front of the app if you're exposing it beyond localhost.

## Dashboard UI and the legacy compatibility API

- The browser UI and the legacy `/api/*` authenticate with the owner's Django session.
- When the session expires, the UI redirects to `/accounts/login/`.
- Only `/api/health/` and `/api/csrf/` are unauthenticated.
- Token issuance/rotation/revocation is staff-only.
- The Bearer-scope contract on `/api/v1/*` is unaffected by any of this.

If a local script must call the legacy `/api/*`, use the internal token generated at deploy time (`DASHBOARD_INTERNAL_API_TOKEN`) instead of a session. This token has owner-level access to the entire legacy API, so prefer scoped `/api/v1/*` tokens for anything new.

```bash
curl --fail --silent --show-error \
  --header "Authorization: Bearer $DASHBOARD_INTERNAL_API_TOKEN" \
  http://localhost:8000/api/summary/
```

Never put this token in a URL, command-line argument, log, prompt, or repository.

## Issuing and storing tokens

The plaintext token is written to a private file once, at issuance time. Only a SHA-256 hash and a lookup prefix are stored in the database.

```bash
python backend/manage.py issue_platform_token \
  --agent-key research-agent \
  --agent-name "Research Agent" \
  --token-name dashboard-platform \
  --scopes platform:read,tasks:write,artifacts:write,approvals:request \
  --capabilities research,analysis \
  --output ~/.dashboard/tokens/research-agent.token
```

- The token file and its parent directory are created with permissions `0600` and `0700`.
- Reissuing under the same agent + token name immediately revokes the previous token.
- Never put a token in a prompt, Slack message, log, git repository, or error message.
- MCP clients should be pointed at `DASHBOARD_API_TOKEN_FILE` (a file path) rather than passed the token value directly.

## Scopes

| Scope | Allows |
|---|---|
| `platform:read` | read tasks, artifacts, approvals, events, agents, search |
| `inbox:write` | ingest external source material |
| `tasks:write` | create/update tasks, transition status |
| `artifacts:write` | create immutable artifact revisions |
| `approvals:request` | request approval of an artifact |
| `approvals:decide` | approve/reject/request changes |

Don't grant `approvals:decide` to a working agent — reserve it for a supervising/admin agent.

## Authenticated calls

```bash
curl --fail --silent --show-error \
  --header "Authorization: Bearer $(cat ~/.dashboard/tokens/research-agent.token)" \
  http://localhost:8000/api/v1/tasks/
```

For automation, prefer the MCP server (`integrations/dashboard_platform_mcp.py`) over shelling out to `curl` — it never exposes the token as a command-line argument.

## Mutations and idempotency

Every `POST` requires an `Idempotency-Key` of at most 128 characters.

```http
Idempotency-Key: example-request-0001
```

- Repeating the same token + path + key + body replays the original response.
- Replayed responses include `Idempotency-Replayed: true`.
- The same key with a different body returns `409 idempotency_conflict`.
- The MCP server generates a UUID key automatically for every mutation.

## Response shapes

Single result:

```json
{"data": {"id": "..."}}
```

List:

```json
{
  "data": [],
  "pagination": {"count": 0, "limit": 50, "offset": 0, "next_offset": null}
}
```

Error:

```json
{
  "error": {
    "code": "insufficient_scope",
    "message": "missing required scope: approvals:decide"
  }
}
```

## Typical call sequence

1. Optionally, `POST /inbox/` to ingest external source material.
2. `POST /tasks/` to create a task.
3. `PATCH /tasks/{id}/` to record status (e.g. `analyzing`).
4. `POST /artifacts/` to save a result as a new revision.
5. `POST /approvals/` to request review of a specific artifact hash.
6. An admin-scoped token calls `POST /approvals/{id}/decision/`.
7. `GET /tasks/{id}/context/` or `/workflows/{id}/` for the full history.

## Artifact storage policy

- Files are written to storage managed by the API.
- Clients get back `artifact://<series_id>/<version>`, never an absolute path.
- Existing revisions are immutable — a revision creates a new version under the same `series_id`.
- Approval requests are bound to a specific `artifact_id` + `sha256`.
- If the file hash changes before a decision is made, the approval is rejected.

## Pagination and search

- Default list `limit=50`, max `100`.
- `offset` is 0 or greater.
- `/search/?q=<query>&limit=20` searches platform tasks and the knowledge base together.

## Not implemented yet

- `/sources`, `/ideas`, `/actions`
- External publishing / order execution
- Webhooks and event streams

These will be added once the approval gates and external-service adapters for them are settled.
