# devin-remediation-orchestrator

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with real values, then export them into your shell (or use a tool
that sources `.env` for you), for example:

```bash
set -a
source .env
set +a
```

## Running

```bash
uvicorn app:app --reload
```

Check the service is up:

```bash
curl http://127.0.0.1:8000/health
```

## Webhook intake and dispatch (Steps 7–8)

`POST /webhook/github` authenticates GitHub webhook deliveries and
schedules a Devin session for curated issues:

- `WEBHOOK_SECRET` must match the secret configured on the GitHub webhook;
  requests are verified with an HMAC-SHA256 signature over the raw body.
- `GITHUB_REPO` is the only repository (`owner/name`) accepted; all other
  repositories are rejected.
- `APPROVED_ACTORS` is a comma-separated allowlist of GitHub usernames
  permitted to trigger remediation.

On a valid `devin-remediate` label event for a curated issue:
1. The issue is reserved in SQLite (`sessions.issue_number` UNIQUE).
2. A Devin v3 session is created with the advisory-specific prompt.
3. The session ID and URL are stored.
4. The `devin-in-progress` label is added to the GitHub issue.

Curated issues are defined in `advisories.json`, keyed by issue number,
containing only trusted advisory metadata — never issue title, body, or
comments. This prevents prompt injection and ensures consistent remediation
across issues.

Dispatch is idempotent at the issue level: two label events for the same
issue create only one Devin session, regardless of delivery replay or manual
re-triggering.

## PR discovery polling (Step 9)

While a session is `dispatched` or `working`, a background task polls Devin
every 15 seconds to check whether it has opened a pull request. Polling
resumes automatically from SQLite after a service restart — any row still
in `dispatched` or `working` state is picked back up on the next poll.

Once a PR appears, the orchestrator:
1. Fetches the PR's current head commit SHA from GitHub.
2. Stores `pr_url` and `head_sha` on the session row.
3. Sets `outcome=awaiting_ci` and `phase=pr_discovered`.

A row with `outcome=awaiting_ci` is no longer polled — Devin finishing a
session is never treated as verification. Step 9 stops at `awaiting_ci`;
only Step 10's independent CI check can mark a remediation `verified`.

Polling requires `DEVIN_API_KEY`, `DEVIN_ORG_ID`, `GITHUB_TOKEN`, and
`GITHUB_REPO`. If any are missing, polling is disabled with a log message
and the rest of the service continues to run normally.

CI verification and the repair cycle are deferred to Step 10.

Run the tests:

```bash
python -m unittest -v test_webhook.py test_dispatch.py test_polling.py
```
