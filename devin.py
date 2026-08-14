"""Devin v3 API client for creating remediation sessions."""

import httpx
import logging

logger = logging.getLogger("devin")


class DevinError(Exception):
    """Safe exception for Devin API failures, stripped of credentials."""
    pass


async def create_session(
    api_key: str,
    org_id: str,
    playbook_id: str,
    prompt: str,
    title: str,
    tags: list[str],
    max_acu_limit: int,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, str]:
    """Create a Devin v3 session and return (session_id, url).

    Args:
        api_key: Devin API key (Bearer token)
        org_id: Devin organization ID
        playbook_id: ID of the remediation playbook
        prompt: Issue-specific prompt
        title: Human-readable session title
        tags: List of tags, e.g. ["remediation", "issue:1"]
        max_acu_limit: Maximum ACU budget for this session
        client: Optional httpx.AsyncClient for testing; not closed by this function

    Returns:
        Tuple of (session_id, url)

    Raises:
        DevinError: On API failure, network error, or validation failure
    """
    if not api_key:
        raise DevinError("api_key is required")
    if not org_id:
        raise DevinError("org_id is required")
    if not prompt or not prompt.strip():
        raise DevinError("prompt must be non-empty")
    if not title or not title.strip():
        raise DevinError("title must be non-empty")
    if not playbook_id or not playbook_id.strip():
        raise DevinError("playbook_id must be non-empty")
    if not isinstance(tags, list) or len(tags) == 0:
        raise DevinError("tags must be a non-empty list")
    if not isinstance(max_acu_limit, int) or max_acu_limit <= 0:
        raise DevinError("max_acu_limit must be a positive integer")

    url = f"https://api.devin.ai/v3/organizations/{org_id}/sessions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    request_body = {
        "prompt": prompt,
        "title": title,
        "playbook_id": playbook_id,
        "tags": tags,
        "max_acu_limit": max_acu_limit,
    }

    should_close = False
    if client is None:
        client = httpx.AsyncClient(timeout=30.0)
        should_close = True

    try:
        response = await client.post(url, headers=headers, json=request_body)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise DevinError(f"Devin API error: HTTP {exc.response.status_code}") from exc
    except (httpx.RequestError, httpx.TimeoutException) as exc:
        raise DevinError(f"Devin API request failed: {type(exc).__name__}") from exc
    finally:
        if should_close:
            await client.aclose()

    try:
        data = response.json()
    except ValueError as exc:
        raise DevinError("Devin API response was not valid JSON") from exc

    session_id = data.get("session_id")
    session_url = data.get("url")

    if not isinstance(session_id, str) or not session_id.strip():
        raise DevinError("Devin response missing or empty session_id")
    if not isinstance(session_url, str) or not session_url.strip():
        raise DevinError("Devin response missing or empty url")

    logger.info("created Devin session %s", session_id)
    return session_id, session_url
