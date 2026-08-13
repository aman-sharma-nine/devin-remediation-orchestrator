"""FastAPI foundation for the remediation orchestrator, with secure GitHub webhook intake."""

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from gh import verify_signature
from store import close_connection, init_db, record_delivery

logger = logging.getLogger("orchestrator")

ADVISORIES_PATH = Path(__file__).resolve().parent / "advisories.json"

CURATED_LABEL = "devin-remediate"


def _load_advisories() -> dict:
    try:
        with open(ADVISORIES_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to load advisories.json: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError("advisories.json root must be a JSON object")

    return data


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.advisories = _load_advisories()
    init_db()
    try:
        yield
    finally:
        close_connection()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


def _ignored(reason: str):
    return JSONResponse(status_code=202, content={"status": "ignored", "reason": reason})


def _parse_actors(raw: str) -> list[str]:
    return [actor.strip() for actor in raw.split(",") if actor.strip()]


@app.post("/webhook/github")
async def webhook_github(request: Request):
    webhook_secret = os.environ.get("WEBHOOK_SECRET", "")
    github_repo = os.environ.get("GITHUB_REPO", "")
    approved_actors_raw = os.environ.get("APPROVED_ACTORS", "")
    approved_actors = _parse_actors(approved_actors_raw)

    if not webhook_secret or not github_repo or not approved_actors:
        logger.warning("rejected: server configuration missing")
        return JSONResponse(status_code=503, content={"detail": "server not configured"})

    delivery_id = request.headers.get("X-GitHub-Delivery")
    event_name = request.headers.get("X-GitHub-Event")

    if not delivery_id or not event_name:
        logger.warning("rejected: missing required headers")
        return JSONResponse(status_code=400, content={"detail": "missing required headers"})

    raw_body = await request.body()
    signature_header = request.headers.get("X-Hub-Signature-256")

    if not verify_signature(raw_body, signature_header, webhook_secret):
        logger.warning("rejected delivery %s: invalid signature", delivery_id)
        return JSONResponse(status_code=401, content={"detail": "invalid signature"})

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.warning("rejected delivery %s: malformed json", delivery_id)
        return JSONResponse(status_code=400, content={"detail": "malformed json"})

    if not isinstance(payload, dict):
        logger.warning("rejected delivery %s: payload root is not an object", delivery_id)
        return JSONResponse(status_code=400, content={"detail": "malformed json"})

    repo_full_name = payload.get("repository", {}).get("full_name") if isinstance(payload.get("repository"), dict) else None
    if repo_full_name != github_repo:
        logger.warning("rejected delivery %s: repository not allowed", delivery_id)
        return JSONResponse(status_code=403, content={"detail": "repository not allowed"})

    sender_login = payload.get("sender", {}).get("login") if isinstance(payload.get("sender"), dict) else None
    if sender_login not in approved_actors:
        logger.warning("rejected delivery %s: actor not allowed", delivery_id)
        return JSONResponse(status_code=403, content={"detail": "actor not allowed"})

    inserted = record_delivery(delivery_id)
    if not inserted:
        logger.info("duplicate delivery %s", delivery_id)
        return _ignored("duplicate_delivery")

    logger.info("accepted delivery %s", delivery_id)

    if event_name != "issues":
        return _ignored("unsupported_event")

    if payload.get("action") != "labeled":
        return _ignored("unsupported_action")

    label = payload.get("label")
    label_name = label.get("name") if isinstance(label, dict) else None
    if label_name != CURATED_LABEL:
        return _ignored("irrelevant_label")

    issue = payload.get("issue")
    issue_number = issue.get("number") if isinstance(issue, dict) else None
    if not isinstance(issue_number, int) or isinstance(issue_number, bool):
        logger.warning("rejected delivery %s: missing or invalid issue number", delivery_id)
        return JSONResponse(status_code=400, content={"detail": "missing or invalid issue number"})

    advisories = request.app.state.advisories
    if str(issue_number) not in advisories:
        logger.info("uncurated issue %s", issue_number)
        return _ignored("uncurated_issue")

    logger.info("accepted curated issue %s", issue_number)
    return JSONResponse(status_code=202, content={"status": "accepted", "issue_number": issue_number})
