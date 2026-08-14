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

Polling, PR detection, CI verification, and repair cycles are deferred to
Step 9.

Run the tests:

```bash
python -m unittest -v test_webhook.py test_dispatch.py
```
