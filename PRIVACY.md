# Privacy and Data Handling

Slack Dashboard is self-hosted software, not a hosted service. The person or organization running an instance is the data controller and is responsible for workspace authorization, user notice, retention, access control, backups, and deletion.

## Data the application handles

Depending on configuration and use, an instance may store:

- Slack messages and thread replies from configured channels;
- Slack channel IDs, user IDs, timestamps, and workspace links;
- derived knowledge items, classifications, tags, quiz material, schedules, and review state;
- local dashboard accounts, session data, API-token metadata, tasks, approvals, and audit events;
- immutable platform artifacts created through the Platform API or MCP server.

The default SQLite database is `db/dashboard.sqlite3`. Platform token files and artifacts default to `db/platform-tokens/` and `db/platform-artifacts/`. MySQL deployments store database content wherever the operator configures MySQL. These runtime paths are excluded from Git.

## External services and data transfers

- The application calls Slack's API to read content from channels explicitly configured by the operator.
- Classification, tagging, and quiz-generation commands send the content needed for those operations to the configured Anthropic or OpenAI API. Review the selected provider's terms and data controls before enabling these commands.
- The project does not implement analytics or telemetry and does not require a third-party font or asset CDN at runtime.

The application does not post answers to Slack. The optional free-question integration imports qualifying question threads and existing bot replies so they can be reviewed in the dashboard.

## Secrets

Slack tokens, LLM API keys, Django secrets, database credentials, and Platform API tokens must remain in local environment variables or ignored token files. Never commit them, paste them into issues, or include them in logs. Platform API clients should receive a token file path rather than the raw token value.

## Retention and deletion

There is no global retention policy or automated privacy-request workflow. Operators must define their own retention period and remove data from the dashboard database, artifact storage, backups, and logs when required. Deleting a source message in Slack does not guarantee that an already imported copy or a derived artifact is deleted locally.

## Operator responsibilities

Before importing a channel or sending content to an LLM provider, confirm that you have authority to process that data. Restrict channel scopes and API-token scopes to the minimum required, expose the service only to trusted users, use TLS outside loopback development, and comply with applicable law and the policies of Slack and the selected LLM provider.

This project is independent software and is not affiliated with, endorsed by, or sponsored by Slack Technologies, Anthropic, or OpenAI.
