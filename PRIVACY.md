# Privacy and Data Handling

Slack Dashboard is self-hosted software, not a hosted service. The person or organization running an instance is the data controller and is responsible for workspace authorization, user notice, retention, access control, backups, and deletion.

## Data the application handles

Depending on configuration and use, an instance may store:

- Slack messages and thread replies from configured channels;
- Slack channel IDs, user IDs, timestamps, and workspace links;
- derived knowledge items, classifications, tags, verification/feedback records, cited-Ask history, quiz material, schedules, and review state;
- local dashboard accounts, session data, API-token metadata, tasks, approvals, and audit events;
- immutable platform artifacts created through the Platform API or MCP server.

The default SQLite database is `db/dashboard.sqlite3`. Dashboard backups created by `backup_dashboard` default to `db/backups/`; Platform token files and artifacts default to `db/platform-tokens/` and `db/platform-artifacts/`. MySQL deployments store database content wherever the operator configures MySQL. These runtime paths are excluded from Git.

## External services and data transfers

- The application calls Slack's API to read content from channels explicitly configured by the operator.
- Classification, tagging, quiz generation, and cited Ask send the content needed for those operations to the configured Anthropic or OpenAI API. Ask sends a bounded set of candidate knowledge excerpts and stores the resulting answer and citation snapshots. Review the selected provider's terms and data controls before enabling these features.
- If `SLACK_DASHBOARD_DIGEST_CHANNEL` is configured and the operator schedules `send_slack_digest`, the application posts operational counts and up to three pending-question titles to that Slack channel. It does not post answers.
- The project does not implement analytics or telemetry and does not require a third-party font or asset CDN at runtime.

The application does not post answers to Slack. The optional free-question integration imports qualifying question threads and existing bot replies so they can be reviewed in the dashboard.

## Secrets

Slack tokens, LLM API keys, Django secrets, database credentials, and Platform API tokens must remain in local environment variables or ignored token files. Never commit them, paste them into issues, or include them in logs. Platform API clients should receive a token file path rather than the raw token value.

## Retention and deletion

The software provides operator-run retention, source disconnect/purge, and JSON backup commands; it does not schedule them or implement an automated privacy-request workflow. `prune_dashboard_data` hides expired Slack-derived records by default and requires `--hard-delete --confirm` for irreversible deletion. `disconnect_slack_source` retains data by default and requires `--purge --confirm` to delete a source and its derived knowledge/quiz records.

Incremental sync cannot discover a source-side deletion until `sync_slack --full-rescan` runs. A full rescan hides a deleted source record locally; it does not hard-delete it. Operators must still remove applicable data from the dashboard database, platform artifact storage, backups, and logs when required. Backup fixtures may contain Slack content, account records, classifications, feedback, and Ask history, so protect and expire them like the primary database.

## Operator responsibilities

Before importing a channel or sending content to an LLM provider, confirm that you have authority to process that data. Restrict channel scopes and API-token scopes to the minimum required, expose the service only to trusted users, use TLS outside loopback development, and comply with applicable law and the policies of Slack and the selected LLM provider.

This project is independent software and is not affiliated with, endorsed by, or sponsored by Slack Technologies, Anthropic, or OpenAI.
