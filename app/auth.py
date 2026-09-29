"""API authentication.

Two ways to authenticate, both enforced on every /api/v1 route:

* ``X-API-Key: <API_KEY>``  - simple shared key, used by the dashboard.
* HMAC request signing      - used by service callers (e.g. a coordinator).
  Headers: ``X-Timestamp`` (unix seconds) and ``X-Signature`` (hex HMAC-SHA256).
  The signed string is::

      {timestamp}\\n{METHOD}\\n{path[?query]}\\n{sha256_hex(body)}

  Requests older/newer than AUTH_MAX_SKEW_SECONDS are rejected and a signature
  can only be used once inside that window (replay protection).
"""
import hashlib
import hmac
import threading
import time

from fastapi import HTTPException, Request

from app.config import settings

_seen: dict[str, float] = {}
_seen_lock = threading.Lock()


def canonical_string(timestamp: int | str, method: str, path_qs: str, body: bytes) -> str:
    return f"{timestamp}\n{method.upper()}\n{path_qs}\n{hashlib.sha256(body).hexdigest()}"


def sign(secret: str, timestamp: int | str, method: str, path_qs: str, body: bytes = b"") -> str:
    msg = canonical_string(timestamp, method, path_qs, body).encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def signed_headers(secret: str, method: str, path_qs: str, body: bytes = b"", timestamp: int | None = None) -> dict:
    ts = int(time.time()) if timestamp is None else timestamp
    return {"X-Timestamp": str(ts), "X-Signature": sign(secret, ts, method, path_qs, body)}


def validate_auth_config() -> None:
    """Fail closed: refuse to start with auth on but no credentials configured."""
    if settings.auth_enabled and not (settings.api_key or settings.hmac_secret):
        raise RuntimeError(
            "AUTH_ENABLED is true but neither API_KEY nor HMAC_SECRET is set. "
            "Set one of them (or AUTH_ENABLED=false for local hacking)."
        )


def _remember(signature: str) -> bool:
    """Record a signature; False if it was already used (replay)."""
    now = time.time()
    with _seen_lock:
        for key in [k for k, exp in _seen.items() if exp < now]:
            del _seen[key]
        if signature in _seen:
            return False
        _seen[signature] = now + settings.auth_max_skew_seconds * 2
        return True


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(401, detail, headers={"WWW-Authenticate": "ApiKey, HMAC"})


async def require_auth(request: Request) -> None:
    if not settings.auth_enabled:
        return

    api_key = request.headers.get("x-api-key")
    if api_key is not None:
        if settings.api_key and hmac.compare_digest(api_key.encode(), settings.api_key.encode()):
            return
        raise _unauthorized("Invalid API key")

    signature = request.headers.get("x-signature")
    timestamp = request.headers.get("x-timestamp")
    if signature and timestamp and settings.hmac_secret:
        try:
            ts = int(timestamp)
        except ValueError:
            raise _unauthorized("Invalid timestamp")
        if abs(time.time() - ts) > settings.auth_max_skew_seconds:
            raise _unauthorized("Stale or future timestamp")
        path_qs = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        expected = sign(settings.hmac_secret, ts, request.method, path_qs, await request.body())
        if not hmac.compare_digest(expected.encode(), signature.encode()):
            raise _unauthorized("Invalid signature")
        if not _remember(signature):
            raise _unauthorized("Replayed request")
        return

    raise _unauthorized("Authentication required")
