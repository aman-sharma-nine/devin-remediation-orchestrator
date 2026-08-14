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

While a session is `dispatched`, `working`, or `repairing`, a background
task polls every 15 seconds to check on it. Polling resumes automatically
from SQLite after a service restart — any row still in one of those states
is picked back up on the next poll.

For `dispatched`/`working` rows, polling calls Devin's session status. Once
a PR appears, the orchestrator:
1. Fetches the PR's current head commit SHA from GitHub.
2. Stores `pr_url` and `head_sha` on the session row.
3. Sets `outcome=awaiting_ci` and `phase=pr_discovered`.

For `repairing` rows (see Step 10 below), polling instead compares GitHub's
live PR head SHA against the row's stored (failed) `head_sha`. If they
still match, Devin hasn't pushed a fix yet and the row is left unchanged.
Once they differ, the new SHA is stored and the row re-enters the CI path
with `outcome=awaiting_ci` and `phase=repair_pushed`.

A row with `outcome=awaiting_ci` is no longer polled — Devin finishing a
session is never treated as verification. Only Step 10's independent CI
check can mark a remediation `verified`.

Polling requires `DEVIN_API_KEY`, `DEVIN_ORG_ID`, `GITHUB_TOKEN`, and
`GITHUB_REPO`. If any are missing, polling is disabled with a log message
and the rest of the service continues to run normally.

## CI verification and repair (Step 10)

`POST /webhook/github` also receives `workflow_run` events on the same
endpoint. Only events where the workflow name matches `VERIFY_WORKFLOW_NAME`
and `action=completed` are processed. The run's `head_sha` is used to look
up the matching session row — but only among rows with `outcome=awaiting_ci`,
so a duplicate or stale event for an old failed SHA is ignored once a repair
has started (see below).

- **Success** — the PR's changed files are checked against a protected list
  (`.github/workflows/verify-remediation.yml`, `scripts/assert_remediated.py`).
  If neither was touched, the row is marked `outcome=verified`,
  `phase=verified`, `check_state=green`, with `verified_at` set. If either
  was touched, a green check is not trusted — the row is marked
  `needs_human` instead.
- **First failure** — the row is atomically marked `outcome=repairing` with
  `repair_attempts=1`, and one message (PR URL, run URL, failed SHA) is sent
  to the *same* Devin session. `head_sha` is intentionally **kept** (the
  failed commit's SHA), not cleared — the Step 9 poller uses it to detect
  when Devin pushes a different commit, at which point the row automatically
  re-enters `awaiting_ci` with `phase=repair_pushed`. No new Devin session is
  ever created for a repair.
- If sending that repair message fails, the row is marked `needs_human` with
  `phase=repair_message_failed` rather than being left stuck in `repairing`
  forever.
- **Second failure** — the row is marked `needs_human`. There is no second
  repair attempt and no infinite retry loop.

`workflow_run` events skip the actor allowlist (GitHub Actions is the
sender, not a human), but still require a valid signature and the
configured repository.

## Dashboard (Step 11)

- **Dashboard:** [http://localhost:8000/](http://localhost:8000/)
- **Metrics endpoint:** [http://localhost:8000/api/metrics](http://localhost:8000/api/metrics)

The page fetches `/api/metrics` once on load and every 5 seconds after,
updating the totals, table, and "Last updated" time in place — no page
reload. It shows three totals (**Dispatched**, **PRs opened**, **CI
verified**) and one table with five columns: **Issue**, **Devin session**,
**Pull request**, **Verification**, **Repair attempts**. Verification is
shown as a readable badge (Verified / Awaiting CI / Repairing / Needs
human / Pending); a missing link renders as an em dash.

The dashboard is entirely read-only: it reports exactly what is already
stored by Steps 7–10 and never writes to the database, creates a session,
or triggers a webhook.

Run the tests:

```bash
python -m unittest -v test_webhook.py test_dispatch.py test_polling.py test_verification.py test_dashboard.py
```
