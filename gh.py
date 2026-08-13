"""GitHub webhook signature verification."""

import hashlib
import hmac

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
