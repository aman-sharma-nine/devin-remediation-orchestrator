"""FastAPI foundation for the remediation orchestrator, with secure GitHub webhook intake."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from flow import dispatch, poll_loop
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


def _build_polling_config() -> dict | None:
    """Return a polling config dict if all required env vars are present,
    else None. Polling only needs Devin/GitHub read access, not the full
    dispatch configuration (no playbook_id or ACU limit required).
    """
    devin_api_key = os.environ.get("DEVIN_API_KEY", "")
    devin_org_id = os.environ.get("DEVIN_ORG_ID", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")
    github_repo = os.environ.get("GITHUB_REPO", "")

    if not devin_api_key or not devin_org_id or not github_token or not github_repo:
        return None

    return {
        "devin_api_key": devin_api_key,
        "devin_org_id": devin_org_id,
        "github_token": github_token,
        "github_repo": github_repo,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.advisories = _load_advisories()
    init_db()

    polling_config = _build_polling_config()
    poll_task = None
    if polling_config is not None:
        poll_task = asyncio.create_task(poll_loop(polling_config))
        logger.info("polling task started")
    else:
        logger.warning("polling disabled: missing DEVIN_API_KEY, DEVIN_ORG_ID, GITHUB_TOKEN, or GITHUB_REPO")

    try:
        yield
    finally:
        if poll_task is not None:
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass
        close_connection()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


def _ignored(reason: str):
    return JSONResponse(status_code=202, content={"status": "ignored", "reason": reason})


def _parse_actors(raw: str) -> list[str]:
    return [actor.strip() for actor in raw.split(",") if actor.strip()]


def _validate_dispatch_config() -> tuple[dict, str | None]:
    """Validate dispatch configuration. Returns (config_dict, error_message)."""
    config = {}

    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        return {}, "GITHUB_TOKEN not configured"

    devin_api_key = os.environ.get("DEVIN_API_KEY", "")
    if not devin_api_key:
        return {}, "DEVIN_API_KEY not configured"

    devin_org_id = os.environ.get("DEVIN_ORG_ID", "")
    if not devin_org_id:
        return {}, "DEVIN_ORG_ID not configured"

    devin_playbook_id = os.environ.get("DEVIN_PLAYBOOK_ID", "")
    if not devin_playbook_id:
        return {}, "DEVIN_PLAYBOOK_ID not configured"

    devin_max_acu_limit_raw = os.environ.get("DEVIN_MAX_ACU_LIMIT", "")
    if not devin_max_acu_limit_raw:
        return {}, "DEVIN_MAX_ACU_LIMIT not configured"

    try:
        devin_max_acu_limit = int(devin_max_acu_limit_raw)
        if devin_max_acu_limit <= 0:
            return {}, "DEVIN_MAX_ACU_LIMIT must be positive"
    except ValueError:
        return {}, "DEVIN_MAX_ACU_LIMIT must be a valid integer"

    config = {
        "github_repo": os.environ.get("GITHUB_REPO", ""),
        "github_token": github_token,
        "devin_api_key": devin_api_key,
        "devin_org_id": devin_org_id,
        "devin_playbook_id": devin_playbook_id,
        "devin_max_acu_limit": devin_max_acu_limit,
    }
    return config, None


@app.post("/webhook/github")
async def webhook_github(request: Request, background_tasks: BackgroundTasks):
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

    config, error = _validate_dispatch_config()
    if error:
        logger.warning("rejected: dispatch configuration invalid: %s", error)
        return JSONResponse(status_code=503, content={"detail": "server not configured"})

    advisory = advisories[str(issue_number)]
    background_tasks.add_task(dispatch, issue_number, advisory, config)

    logger.info("accepted curated issue %s, scheduled dispatch", issue_number)
    return JSONResponse(status_code=202, content={"status": "accepted", "issue_number": issue_number})
