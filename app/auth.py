from __future__ import annotations

import hmac
import logging
from urllib.parse import urlparse

from fastapi import Header, HTTPException, Request, WebSocket

from .config import Settings

LOGGER = logging.getLogger(__name__)
ADMIN_HEADER = "X-Admin-Token"


def tokens_match(provided: str | None, expected: str) -> bool:
    if not provided:
        return False
    left = provided.encode("utf-8")
    right = expected.encode("utf-8")
    if len(left) != len(right):
        hmac.compare_digest(right, right)
        return False
    return hmac.compare_digest(left, right)


async def require_admin(
    request: Request,
    x_admin_token: str | None = Header(default=None, alias=ADMIN_HEADER),
) -> None:
    settings: Settings = request.app.state.settings
    expected = settings.admin_token
    if not expected:
        return
    if tokens_match(x_admin_token, expected):
        return
    raise HTTPException(status_code=401, detail="Admin token required")


def websocket_origin_allowed(websocket: WebSocket, settings: Settings) -> bool:
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    return origin_is_allowed(origin, websocket.headers.get("host"), settings)


def origin_is_allowed(origin: str, host: str | None, settings: Settings) -> bool:
    allowed = {item.rstrip("/") for item in settings.cors_origins}
    if host:
        allowed.add(f"http://{host}")
        allowed.add(f"https://{host}")
    parsed = urlparse(origin)
    normalized = origin.rstrip("/")
    if normalized in allowed:
        return True
    if parsed.hostname in {"127.0.0.1", "localhost"} and host:
        host_name = host.split(":", 1)[0]
        return host_name in {"127.0.0.1", "localhost"}
    return False


def log_insecure_admin_config(settings: Settings) -> None:
    if not settings.admin_token:
        LOGGER.warning(
            "AIRMON_ADMIN_TOKEN is empty; mutating API routes are open to any client "
            "that can reach the backend. Set a token before binding beyond loopback."
        )
