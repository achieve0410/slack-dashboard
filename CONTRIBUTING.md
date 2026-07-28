# Contributing

By participating, you agree to follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## Development setup

See the Quickstart section of [README.md](README.md) for configuration and local servers. Install reproducible dependencies with:

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements-lock.txt -r requirements-mcp-lock.txt

cd frontend
npm ci
cd ..
```

## Running tests

```bash
# Backend
python backend/manage.py check
python backend/manage.py test dashboard
python -m unittest integrations.test_dashboard_platform_mcp
python backend/deploy/test-inventory-lock-runner.py

# Frontend
cd frontend
npm audit --audit-level=high
npm run test:unit
npm run lint
npm run typecheck
npm run build
```

CI (`.github/workflows/ci.yml`) runs all of the above on every push and pull request.

## Updating dependencies

Edit the source constraints in `requirements.txt` or `requirements-mcp.txt`, then regenerate and test both lock files with the pinned Python target:

```bash
uv pip compile requirements.txt --python-version 3.12 --universal --output-file requirements-lock.txt
uv pip compile requirements-mcp.txt --python-version 3.12 --universal --output-file requirements-mcp-lock.txt
```

Keep the optional `mysqlclient` pin in `requirements-mysql.txt` current as well. For frontend dependencies, edit `frontend/package.json` and update `frontend/package-lock.json` with npm 10.9.8, the package manager version declared by the project. Commit source manifests and lock files together.

## Legacy naming

A few internal names predate this project's public release and were kept to minimize churn:

- The `CronJob` model represents a **content source** — one row per Slack channel configured via `SLACK_KNOWLEDGE_CHANNELS`. The name reflects an earlier design where each row was a scheduled job.
- `KnowledgeItem.source_type` uses the stored value `"cron"` for the same reason. It's referenced by the frontend's filter values and many tests, so it wasn't renamed.

Neither is worth a breaking rename on its own; if you're touching either code path anyway, feel free to leave a clarifying comment rather than renaming.

## Pull requests

Keep changes focused. If you're fixing a bug, add a test that reproduces it first. If you're adding a feature, explain the use case in the PR description. Complete the pull request template, document user-visible changes, and list the exact verification commands you ran.

Unless explicitly stated otherwise, contributions submitted to this repository are licensed under the same MIT License as the project.
