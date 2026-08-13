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

Webhook processing (`POST /webhook/github`) is intentionally deferred to
Step 7 — it currently returns HTTP 501.
