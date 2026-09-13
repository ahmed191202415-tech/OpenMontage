from __future__ import annotations

import hashlib
import hmac
import os
import time
from urllib.parse import urlencode


def _secret() -> bytes:
    value = os.environ.get("GPT_GATEWAY_SIGNING_SECRET")
    if not value:
        raise RuntimeError("GPT_GATEWAY_SIGNING_SECRET is not configured")
    return value.encode("utf-8")


def sign_render(project_id: str, expires: int) -> str:
    payload = f"render:{project_id}:{expires}".encode("utf-8")
    return hmac.new(_secret(), payload, hashlib.sha256).hexdigest()


def verify_render(project_id: str, expires: int, signature: str) -> bool:
    if expires < int(time.time()):
        return False
    expected = sign_render(project_id, expires)
    return hmac.compare_digest(expected, signature)


def make_render_url(project_id: str, ttl_seconds: int = 900) -> dict[str, object]:
    expires = int(time.time()) + max(60, min(ttl_seconds, 86400))
    signature = sign_render(project_id, expires)
    base = os.environ.get("GPT_GATEWAY_PUBLIC_BASE_URL", "").rstrip("/")
    path = f"/api/v1/public/renders/{project_id}?" + urlencode({"expires": expires, "sig": signature})
    return {
        "url": f"{base}{path}" if base else path,
        "expires": expires,
        "ttl_seconds": expires - int(time.time()),
    }
