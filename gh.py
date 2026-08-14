"""GitHub webhook signature verification and API operations."""

import hashlib
import hmac
import httpx
import logging

logger = logging.getLogger("github")

_PREFIX = "sha256="


def verify_signature(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    if not secret:
        return False
    if not signature_header:
        return False
    if not signature_header.startswith(_PREFIX):
        return False

    provided_digest = signature_header[len(_PREFIX):]
    expected_digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    return hmac.compare_digest(provided_digest, expected_digest)


class GitHubError(Exception):
    """Safe exception for GitHub API failures, stripped of credentials."""
    pass


async def add_issue_label(
    repo: str,
    issue_number: int,
    label: str,
    token: str,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Add a label to a GitHub issue.

    Args:
        repo: Repository in format "owner/repo"
        issue_number: Issue number
        label: Label name to add
        token: GitHub API token (used as Bearer token)
        client: Optional httpx.AsyncClient for testing; not closed by this function

    Raises:
        GitHubError: On API failure or validation failure
    """
    if not repo or not repo.strip():
        raise GitHubError("repo must be non-empty")
    if not isinstance(issue_number, int) or issue_number <= 0:
        raise GitHubError("issue_number must be a positive integer")
    if not label or not label.strip():
        raise GitHubError("label must be non-empty")
    if not token or not token.strip():
        raise GitHubError("token must be non-empty")

    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/labels"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    request_body = {"labels": [label]}

    should_close = False
    if client is None:
        client = httpx.AsyncClient(timeout=30.0)
        should_close = True

    try:
        response = await client.post(url, headers=headers, json=request_body)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise GitHubError(f"GitHub API error: HTTP {exc.response.status_code}") from exc
    except (httpx.RequestError, httpx.TimeoutException) as exc:
        raise GitHubError(f"GitHub API request failed: {type(exc).__name__}") from exc
    finally:
        if should_close:
            await client.aclose()

    logger.info("added label %s to %s#%d", label, repo, issue_number)
