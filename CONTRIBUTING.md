# Contributing

## Development setup

See the Quickstart section of [README.md](README.md) for installing dependencies and running the app locally.

## Running tests

```bash
# Backend
python backend/manage.py test dashboard

# Frontend
cd frontend
npm run test:unit
npm run typecheck
npm run build
```

CI (`.github/workflows/ci.yml`) runs all of the above on every push and pull request.

## Legacy naming

A few internal names predate this project's public release and were kept to minimize churn:

- The `CronJob` model represents a **content source** — one row per Slack channel configured via `SLACK_KNOWLEDGE_CHANNELS`. The name reflects an earlier design where each row was a scheduled job.
- `KnowledgeItem.source_type` uses the stored value `"cron"` for the same reason. It's referenced by the frontend's filter values and many tests, so it wasn't renamed.

Neither is worth a breaking rename on its own; if you're touching either code path anyway, feel free to leave a clarifying comment rather than renaming.

## Pull requests

Keep changes focused. If you're fixing a bug, add a test that reproduces it first. If you're adding a feature, explain the use case in the PR description.
