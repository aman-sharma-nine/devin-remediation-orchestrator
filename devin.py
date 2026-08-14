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


async def get_session(
    api_key: str,
    org_id: str,
    session_id: str,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Fetch a Devin v3 session's current status and pull requests.

    Args:
        api_key: Devin API key (Bearer token)
        org_id: Devin organization ID
        session_id: Devin session ID, exactly as returned by create_session
        client: Optional httpx.AsyncClient for testing; not closed by this function

    Returns:
        Dict with keys: status, status_detail, pull_requests, acus_consumed.
        pull_requests is a list of {"pr_url": str, "pr_state": str}.

    Raises:
        DevinError: On API failure, network error, or validation failure
    """
    if not api_key:
        raise DevinError("api_key is required")
    if not org_id:
        raise DevinError("org_id is required")
    if not session_id or not session_id.strip():
        raise DevinError("session_id must be non-empty")

    url = f"https://api.devin.ai/v3/organizations/{org_id}/sessions/{session_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }

    should_close = False
    if client is None:
        client = httpx.AsyncClient(timeout=30.0)
        should_close = True

    try:
        response = await client.get(url, headers=headers)
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

    if not isinstance(data, dict):
        raise DevinError("Devin API response was not a JSON object")

    status = data.get("status")
    if not isinstance(status, str) or not status.strip():
        raise DevinError("Devin response missing or empty status")

    status_detail = data.get("status_detail")
    if status_detail is not None and not isinstance(status_detail, str):
        raise DevinError("Devin response status_detail must be a string or null")

    raw_pull_requests = data.get("pull_requests", [])
    if not isinstance(raw_pull_requests, list):
        raise DevinError("Devin response pull_requests must be a list")

    pull_requests = []
    for pr in raw_pull_requests:
        if not isinstance(pr, dict):
            raise DevinError("Devin response pull_requests entry must be an object")
        pr_url = pr.get("pr_url")
        pr_state = pr.get("pr_state")
        if not isinstance(pr_url, str) or not pr_url.strip():
            continue
        pull_requests.append({"pr_url": pr_url, "pr_state": pr_state})

    acus_consumed = data.get("acus_consumed")
    if acus_consumed is not None and not isinstance(acus_consumed, (int, float)):
        raise DevinError("Devin response acus_consumed must be numeric or null")

    return {
        "status": status,
        "status_detail": status_detail,
        "pull_requests": pull_requests,
        "acus_consumed": acus_consumed,
    }


async def send_message(
    api_key: str,
    org_id: str,
    session_id: str,
    message: str,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Send a follow-up message to an existing Devin v3 session.

    Args:
        api_key: Devin API key (Bearer token)
        org_id: Devin organization ID
        session_id: Devin session ID, exactly as returned by create_session
        message: Message text to send to the session
        client: Optional httpx.AsyncClient for testing; not closed by this function

    Raises:
        DevinError: On API failure, network error, or validation failure
    """
    if not api_key:
        raise DevinError("api_key is required")
    if not org_id:
        raise DevinError("org_id is required")
    if not session_id or not session_id.strip():
        raise DevinError("session_id must be non-empty")
    if not message or not message.strip():
        raise DevinError("message must be non-empty")

    url = f"https://api.devin.ai/v3/organizations/{org_id}/sessions/{session_id}/messages"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    request_body = {"message": message}

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

    logger.info("sent message to Devin session %s", session_id)
