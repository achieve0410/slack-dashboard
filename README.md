# Slack Dashboard

A self-hosted knowledge dashboard that imports content from Slack channels, classifies it with an LLM, and gives you a searchable library, a quiz generator, a schedule/TODO tracker, and an API other agents can use.

Built with Django (backend/API) and Nuxt (frontend SPA).

## Features

- **Slack ingestion** — imports messages from Slack channels you configure into a knowledge base. Supports a free-question channel (mention the bot, get an answer back in-thread) and a schedule channel (parse `2026-07-20 14:00~15:00 | title | notes` style messages into a calendar).
- **LLM classification** — an Anthropic or OpenAI model classifies each imported item into a category tree it builds up over time, with confidence thresholds and a manual-review fallback.
- **Knowledge library** — browse, search, tag, bookmark, and archive everything that's been imported and classified.
- **Quiz generation** — generates multiple-choice/multi-select quiz questions from knowledge items classified under three built-in domains (English, Japanese, AWS certification study — see [Limitations](#limitations)), with spaced-repetition review.
- **Schedule / TODO** — a calendar view backed by both the web UI and a Slack channel, with keyword-based auto-categorization.
- **Platform API** (`/api/v1/*`) — a scoped, Bearer-token-authenticated API plus an MCP server (`integrations/dashboard_platform_mcp.py`) so other agents/tools can read the knowledge base and create tasks/artifacts/approvals. See [docs/API_GUIDE.md](docs/API_GUIDE.md) and [docs/MCP_TOOLS.md](docs/MCP_TOOLS.md).

## Architecture

```
Slack workspace
     │  (Bot token, conversations.history/replies)
     ▼
sync_slack management command
     │
     ▼
Django + SQLite (or MySQL)  ──▶  classify_knowledge / tag_knowledge / generate_quiz_questions
     │                                  │  (Anthropic or OpenAI API)
     ▼                                  ▼
/api/*  (session auth, legacy)     Category tree, tags, quiz questions
/api/v1/*  (Bearer token + scopes) ──▶ integrations/dashboard_platform_mcp.py (MCP server)
     │
     ▼
Nuxt SPA  (served by WhiteNoise once built)
```

Everything runs as one Django process (`gunicorn` in production, `runserver` in dev) with a Nuxt-built static SPA served alongside it. There's no required external service beyond Slack and your LLM provider — MySQL is optional (SQLite is the default).

## Quickstart

Requirements: Python ≥3.12, Node ≥22.11.

```bash
git clone <this-repo>
cd slack-dashboard

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt        # add mysqlclient via requirements-mysql.txt if you want MySQL

cd frontend && npm install && cd ..

cp .env.example .env
# edit .env: at minimum set SLACK_BOT_TOKEN, SLACK_KNOWLEDGE_CHANNELS, and an LLM API key

python backend/manage.py migrate
python backend/manage.py createsuperuser

# two terminals:
./backend/deploy/dev-backend.sh    # Django on http://127.0.0.1:8000
./backend/deploy/dev-frontend.sh   # Nuxt dev server on http://localhost:3000, proxying /api to the backend
```

Open `http://localhost:3000`, log in with the superuser you created, and you'll see an empty dashboard. Run a sync to pull in Slack content:

```bash
python backend/manage.py sync_slack
```

### Slack app setup

Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps) (or use an existing one) and add these **Bot Token Scopes**:

| Scope | Why |
|---|---|
| `channels:history` | read public channels you configure as knowledge sources |
| `groups:history` | only needed if any source channel is private |
| `channels:read` | recommended — lets the dashboard show real channel names instead of raw IDs |

Install the app to your workspace, copy the `xoxb-...` Bot User OAuth Token into `SLACK_BOT_TOKEN`, and **invite the bot to every channel** you list in `SLACK_KNOWLEDGE_CHANNELS` (and the free-question/schedule channels, if used). To get a channel's ID, right-click it in Slack → View channel details → copy the ID at the bottom.

### LLM setup

Classification, tagging, and quiz generation all go through `backend/dashboard/llm.py`, a small adapter over the Anthropic and OpenAI SDKs (no other provider is wired up). Set in `.env`:

```
LLM_PROVIDER=anthropic          # or "openai"
LLM_MODEL=                      # required for openai; anthropic defaults to claude-sonnet-5
ANTHROPIC_API_KEY=...
# OPENAI_API_KEY=...
```

Recommended Anthropic models: `claude-opus-5` (highest quality), `claude-sonnet-5` (default, good balance), `claude-haiku-4-5` (cheapest, fine for classification).

### Running the pipelines

These are plain management commands — run them manually, or on a schedule (cron, systemd timer, launchd, whatever you already use):

```bash
python backend/manage.py sync_slack                 # pull new Slack content
python backend/manage.py classify_knowledge --limit 50   # classify pending items
python backend/manage.py tag_knowledge --publish     # regenerate the tag snapshot
python backend/manage.py generate_quiz_questions --publish  # generate quiz questions
```

`backend/deploy/run-sync.sh`, `run-classify.sh`, `run-tagging.sh`, and `run-quiz.sh` wrap these with a shared file lock (so overlapping runs don't collide) — point your scheduler at those instead of calling `manage.py` directly. Example crontab:

```
*/15 * * * * /path/to/slack-dashboard/backend/deploy/run-sync.sh >> /path/to/slack-dashboard/backend/run/sync.log 2>&1
30 2 * * *   /path/to/slack-dashboard/backend/deploy/run-tagging.sh >> /path/to/slack-dashboard/backend/run/tagging.log 2>&1
30 3 * * *   /path/to/slack-dashboard/backend/deploy/run-classify.sh --limit 50 >> /path/to/slack-dashboard/backend/run/classify.log 2>&1
```

## Production notes

1. Build the frontend once and let Django/WhiteNoise serve it:
   ```bash
   cd frontend && npm run build && cd ..
   python backend/manage.py collectstatic --noinput
   ```
2. Set `DJANGO_DEBUG=0`, a real `DJANGO_SECRET_KEY` (the app refuses to start without one when debug is off), and `DJANGO_ALLOWED_HOSTS`/`DJANGO_CSRF_TRUSTED_ORIGINS` for your domain.
3. Run `python -m gunicorn --config backend/deploy/gunicorn.conf.py config.wsgi:application` (or `backend/deploy/run-gunicorn.sh`), bound to `127.0.0.1:8000` by default (override with `GUNICORN_BIND`).
4. Put a reverse proxy in front of it for TLS. `backend/deploy/nginx.conf.example` is a minimal starting point if you use nginx — any reverse proxy works.
5. For MySQL instead of SQLite: `pip install -r requirements-mysql.txt`, `db/script.sh start` (spins up MySQL via Docker Compose, generates credentials into `db/slack_dashboard_db/.env`), then set `DJANGO_DB_ENGINE=mysql` plus the `DJANGO_DB_*` variables in `.env`.

## Security posture

- The legacy `/api/*` surface is gated by Django session auth (or an optional shared-secret Bearer token via `DASHBOARD_INTERNAL_API_TOKEN`) once `DASHBOARD_AUTH_REQUIRED=1`; the SPA itself always requires a logged-in session.
- The `/api/v1/*` Platform API uses per-agent Bearer tokens with explicit scopes (`platform:read`, `inbox:write`, `tasks:write`, `artifacts:write`, `approvals:request`, `approvals:decide`) — see [docs/API_GUIDE.md](docs/API_GUIDE.md).
- This is a single-operator tool: there's no multi-tenant user model. Don't expose it to untrusted users.
- No rate limiting or cost caps on the LLM-calling endpoints — if you expose classification/tagging/quiz generation to anyone but yourself, add your own.

## Limitations

- The UI is Korean-only (this wasn't translated for the initial public release).
- Quiz generation only targets three fixed domains: English, Japanese, and AWS certification, keyed on the classification category paths `학습/언어/영어`, `학습/언어/일본어`, `학습/자격증/AWS`. Content that doesn't get classified there won't produce quiz questions.
- `sync_slack` re-fetches full channel history on every run; there's no incremental/`--oldest` mode yet, so very high-volume channels will be slow to sync.
- Single-operator design — no per-user data isolation.

## License

MIT — see [LICENSE](LICENSE).
