"""Dispatch flow for creating Devin sessions from curated webhooks, and
polling to discover the PR each session eventually opens."""

import asyncio
import logging
from devin import create_session, get_session, DevinError
from gh import add_issue_label, get_pull_request_head_sha, GitHubError
from store import (
    claim_session,
    complete_dispatch,
    fail_dispatch,
    list_pollable_sessions,
    update_polling_state,
    record_pr_discovered,
)

logger = logging.getLogger("flow")

POLL_INTERVAL_SECONDS = 15


def build_prompt(issue_number: int, github_repo: str, advisory: dict) -> str:
    """Build a trusted, issue-specific prompt from curated advisory data.

    The prompt uses only trusted values from advisories.json, never issue
    body/title/comments.
    """
    advisory_id = advisory.get("advisory_id", "")
    package = advisory.get("package", "")
    ecosystem = advisory.get("ecosystem", "")
    advisory_url = advisory.get("advisory_url", "")
    manifest_path = advisory.get("manifest_path", "")
    lockfile_path = advisory.get("lockfile_path", "")
    test_command = advisory.get("test_command", "")

    current_versions = advisory.get("current_versions", [])
    affected_ranges = advisory.get("affected_ranges", [])
    fixed_versions = advisory.get("fixed_versions", {})

    current_str = ", ".join(str(v) for v in current_versions) if current_versions else "unknown"
    affected_str = ", ".join(affected_ranges) if affected_ranges else "unknown"
    fixed_str = str(fixed_versions) if fixed_versions else "check advisory"

    prompt = f"""Remediate {advisory_id} in {github_repo} (issue #{issue_number}).

Repository: https://github.com/{github_repo}
Advisory: {advisory_url}

Package: {package}
Ecosystem: {ecosystem}
Current versions: {current_str}
Affected ranges: {affected_str}
Fixed versions: {fixed_str}

Manifest: {manifest_path}
Lockfile: {lockfile_path}
Test: {test_command}

Use the attached remediation Playbook for the engineering procedure.
Open a draft PR against the fork's default branch, never apache/superset.
Include "Fixes #{issue_number}" in the PR description.
Do not modify .github/ or scripts/ directories.
"""
    return prompt


async def dispatch(issue_number: int, advisory: dict, config: dict) -> None:
    """Dispatch a Devin session for the given issue and advisory.

    Processing order:
    1. Claim session (idempotency boundary)
    2. If already claimed, return without calling external APIs
    3. Build prompt
    4. Create Devin session
    5. Complete dispatch (store session details)
    6. Add devin-in-progress label

    Args:
        issue_number: GitHub issue number
        advisory: Advisory entry from advisories.json
        config: Dict with keys: github_repo, github_token, devin_api_key,
                devin_org_id, devin_playbook_id, devin_max_acu_limit
    """
    github_repo = config.get("github_repo")
    github_token = config.get("github_token")
    devin_api_key = config.get("devin_api_key")
    devin_org_id = config.get("devin_org_id")
    devin_playbook_id = config.get("devin_playbook_id")
    devin_max_acu_limit = config.get("devin_max_acu_limit")

    advisory_id = advisory.get("advisory_id", "")
    package = advisory.get("package", "")

    try:
        claimed = claim_session(issue_number, advisory_id, package)
    except Exception as exc:
        logger.error("failed to claim session for issue %d: %s", issue_number, exc)
        return

    if not claimed:
        logger.info("issue %d already has a session, skipping dispatch", issue_number)
        return

    logger.info("claimed session for issue %d", issue_number)

    prompt = build_prompt(issue_number, github_repo, advisory)
    title = f"Remediate {advisory_id} in {github_repo} (issue #{issue_number})"
    tags = ["remediation", f"issue:{issue_number}"]

    try:
        session_id, session_url = await create_session(
            api_key=devin_api_key,
            org_id=devin_org_id,
            playbook_id=devin_playbook_id,
            prompt=prompt,
            title=title,
            tags=tags,
            max_acu_limit=devin_max_acu_limit,
        )
    except DevinError as exc:
        logger.error("Devin creation failed for issue %d: %s", issue_number, exc)
        try:
            fail_dispatch(issue_number, str(exc))
        except Exception as inner_exc:
            logger.error("failed to record dispatch failure for issue %d: %s", issue_number, inner_exc)
        return

    try:
        complete_dispatch(issue_number, session_id, session_url)
    except Exception as exc:
        logger.error("failed to complete dispatch for issue %d: %s", issue_number, exc)
        return

    logger.info("completed dispatch for issue %d, session %s", issue_number, session_id)

    try:
        await add_issue_label(github_repo, issue_number, "devin-in-progress", github_token)
    except GitHubError as exc:
        logger.warning("failed to add label to issue %d: %s", issue_number, exc)


def _map_devin_status(status: str, status_detail: str | None) -> tuple[str, str]:
    """Map a Devin v3 status/status_detail pair to (outcome, phase) when no
    PR is present yet. See Step 9 mapping table.
    """
    if status in ("new", "claimed", "resuming"):
        return "working", "working"

    if status == "running":
        if status_detail == "working":
            return "working", "working"
        if status_detail == "waiting_for_user":
            return "blocked", "blocked"
        if status_detail == "waiting_for_approval":
            return "needs_human", "needs_human"
        if status_detail == "finished":
            return "needs_human", "needs_human"
        return "working", "working"

    if status in ("exit", "error", "suspended"):
        return "needs_human", "needs_human"

    return "working", "working"


async def poll_once(config: dict) -> None:
    """Poll every pollable session's Devin status exactly once.

    Rows already at outcome=awaiting_ci (or beyond) are excluded by
    list_pollable_sessions(), so a row that just discovered its PR is
    naturally skipped on the next call without any extra bookkeeping.
    """
    devin_api_key = config.get("devin_api_key")
    devin_org_id = config.get("devin_org_id")
    github_repo = config.get("github_repo")
    github_token = config.get("github_token")

    try:
        rows = list_pollable_sessions()
    except Exception as exc:
        logger.error("failed to list pollable sessions: %s", exc)
        return

    for row in rows:
        issue_number = row["issue_number"]
        session_id = row["session_id"]

        try:
            session = await get_session(devin_api_key, devin_org_id, session_id)
        except DevinError as exc:
            logger.error("polling failed for issue %d: %s", issue_number, exc)
            continue
        except Exception as exc:
            logger.error("unexpected polling failure for issue %d: %s", issue_number, exc)
            continue

        acus_consumed = session.get("acus_consumed")
        pull_requests = session.get("pull_requests", [])

        pr_url = None
        for pr in pull_requests:
            candidate = pr.get("pr_url")
            if candidate:
                pr_url = candidate
                break

        if pr_url:
            try:
                head_sha = await get_pull_request_head_sha(github_repo, pr_url, github_token)
            except GitHubError as exc:
                logger.error("failed to fetch PR head sha for issue %d: %s", issue_number, exc)
                continue
            except Exception as exc:
                logger.error("unexpected error fetching PR head sha for issue %d: %s", issue_number, exc)
                continue

            try:
                updated = record_pr_discovered(issue_number, pr_url, head_sha, acus_consumed)
            except Exception as exc:
                logger.error("failed to record PR discovery for issue %d: %s", issue_number, exc)
                continue

            if updated:
                logger.info("PR discovered for issue %d: %s", issue_number, pr_url)
            continue

        status = session.get("status")
        status_detail = session.get("status_detail")
        outcome, phase = _map_devin_status(status, status_detail)

        try:
            updated = update_polling_state(issue_number, outcome, phase, acus_consumed)
        except Exception as exc:
            logger.error("failed to update polling state for issue %d: %s", issue_number, exc)
            continue

        if updated:
            logger.info("issue %d polling update: outcome=%s phase=%s", issue_number, outcome, phase)


async def poll_loop(config: dict) -> None:
    """Poll all pollable sessions every POLL_INTERVAL_SECONDS until cancelled."""
    try:
        while True:
            await poll_once(config)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("poll loop cancelled")
        raise
