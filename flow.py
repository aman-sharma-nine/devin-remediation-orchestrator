"""Dispatch flow for creating Devin sessions from curated webhooks."""

import logging
from devin import create_session, DevinError
from gh import add_issue_label, GitHubError
from store import claim_session, complete_dispatch, fail_dispatch

logger = logging.getLogger("flow")


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
