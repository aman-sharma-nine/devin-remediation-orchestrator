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

## Webhook intake (Step 7)

`POST /webhook/github` now authenticates and records GitHub webhook
deliveries:

- `WEBHOOK_SECRET` must match the secret configured on the GitHub webhook;
  requests are verified with an HMAC-SHA256 signature over the raw body.
- `GITHUB_REPO` is the only repository (`owner/name`) accepted; all other
  repositories are rejected.
- `APPROVED_ACTORS` is a comma-separated allowlist of GitHub usernames
  permitted to trigger remediation.

The endpoint validates the request, deduplicates the delivery, and records a
curated `devin-remediate` label event — it does not yet create a Devin
session or call any external API.

Run the tests:

```bash
python -m unittest -v test_webhook.py
```
