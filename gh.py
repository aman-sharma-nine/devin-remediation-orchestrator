"""GitHub webhook signature verification and API operations."""

import hashlib
import hmac
import re
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


_PR_URL_RE = re.compile(r"^https://github\.com/([^/]+/[^/]+)/pull/(\d+)/?$")


def _resolve_pr_number(github_repo: str, pr_url: str) -> int:
    """Parse a GitHub PR URL and confirm it belongs to github_repo.

    Raises GitHubError on a malformed URL or repository mismatch.
    """
    match = _PR_URL_RE.match(pr_url.strip())
    if not match:
        raise GitHubError("pr_url is not a well-formed GitHub pull request URL")

    pr_repo, pr_number_str = match.group(1), match.group(2)
    if pr_repo != github_repo:
        raise GitHubError("pr_url does not belong to the configured repository")

    return int(pr_number_str)


async def get_pull_request_head_sha(
    github_repo: str,
    pr_url: str,
    token: str,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Fetch the current head commit SHA of a GitHub pull request.

    Args:
        github_repo: The expected "owner/repo" the PR must belong to
        pr_url: Full GitHub PR URL, e.g. https://github.com/owner/repo/pull/5
        token: GitHub API token (used as Bearer token)
        client: Optional httpx.AsyncClient for testing; not closed by this function

    Returns:
        The PR's current head commit SHA.

    Raises:
        GitHubError: On a malformed URL, repository mismatch, API failure,
            missing SHA, or validation failure.
    """
    if not github_repo or not github_repo.strip():
        raise GitHubError("github_repo must be non-empty")
    if not pr_url or not pr_url.strip():
        raise GitHubError("pr_url must be non-empty")
    if not token or not token.strip():
        raise GitHubError("token must be non-empty")

    pr_number = _resolve_pr_number(github_repo, pr_url)

    url = f"https://api.github.com/repos/{github_repo}/pulls/{pr_number}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    should_close = False
    if client is None:
        client = httpx.AsyncClient(timeout=30.0)
        should_close = True

    try:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise GitHubError(f"GitHub API error: HTTP {exc.response.status_code}") from exc
    except (httpx.RequestError, httpx.TimeoutException) as exc:
        raise GitHubError(f"GitHub API request failed: {type(exc).__name__}") from exc
    finally:
        if should_close:
            await client.aclose()

    try:
        data = response.json()
    except ValueError as exc:
        raise GitHubError("GitHub API response was not valid JSON") from exc

    if not isinstance(data, dict):
        raise GitHubError("GitHub API response was not a JSON object")

    head = data.get("head")
    if not isinstance(head, dict):
        raise GitHubError("GitHub API response missing head object")

    sha = head.get("sha")
    if not isinstance(sha, str) or not sha.strip():
        raise GitHubError("GitHub API response missing head.sha")

    return sha


async def get_pull_request_files(
    github_repo: str,
    pr_url: str,
    token: str,
    client: httpx.AsyncClient | None = None,
) -> list[str]:
    """Fetch the list of changed file paths in a pull request.

    Args:
        github_repo: The expected "owner/repo" the PR must belong to
        pr_url: Full GitHub PR URL, e.g. https://github.com/owner/repo/pull/5
        token: GitHub API token (used as Bearer token)
        client: Optional httpx.AsyncClient for testing; not closed by this function

    Returns:
        List of changed file paths (first page only).

    Raises:
        GitHubError: On a malformed URL, repository mismatch, API failure,
            or validation failure.
    """
    if not github_repo or not github_repo.strip():
        raise GitHubError("github_repo must be non-empty")
    if not pr_url or not pr_url.strip():
        raise GitHubError("pr_url must be non-empty")
    if not token or not token.strip():
        raise GitHubError("token must be non-empty")

    pr_number = _resolve_pr_number(github_repo, pr_url)

    url = f"https://api.github.com/repos/{github_repo}/pulls/{pr_number}/files"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    should_close = False
    if client is None:
        client = httpx.AsyncClient(timeout=30.0)
        should_close = True

    try:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise GitHubError(f"GitHub API error: HTTP {exc.response.status_code}") from exc
    except (httpx.RequestError, httpx.TimeoutException) as exc:
        raise GitHubError(f"GitHub API request failed: {type(exc).__name__}") from exc
    finally:
        if should_close:
            await client.aclose()

    try:
        data = response.json()
    except ValueError as exc:
        raise GitHubError("GitHub API response was not valid JSON") from exc

    if not isinstance(data, list):
        raise GitHubError("GitHub API response was not a JSON array")

    filenames = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        filename = entry.get("filename")
        if isinstance(filename, str) and filename:
            filenames.append(filename)

    return filenames
