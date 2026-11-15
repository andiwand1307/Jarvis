"""Access control (architecture doc, section 4)."""

from __future__ import annotations

import hmac

from .config import INGRESS_GATEWAY, Config

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
PUBLIC_PATHS = frozenset({"api/health"})


def _bearer(headers) -> str:
    auth = headers.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def is_allowed(config: Config, client_host: str | None, path: str, headers, query) -> tuple[bool, int]:
    """Returns (allowed, status code to use when denied)."""
    path = path.lstrip("/")
    host = client_host or ""
    mode = config.access_mode

    if mode == "ingress":
        if host == INGRESS_GATEWAY:
            return True, 200
        if host in LOOPBACK and path in PUBLIC_PATHS:
            return True, 200
        return False, 403

    if mode == "local":
        return (True, 200) if host in LOOPBACK else (False, 403)

    # Token mode: the API needs the bearer token; static UI files and health are public.
    if not path.startswith("api/") or path in PUBLIC_PATHS:
        return True, 200
    for supplied in (_bearer(headers), query.get("token", ""), cookie_token(headers)):
        if token_matches(config, supplied):
            return True, 200
    return False, 401


COOKIE_NAME = "jarvis_token"


def token_matches(config: Config, supplied: str) -> bool:
    return bool(supplied and config.token) and hmac.compare_digest(supplied.encode(), config.token.encode())


def cookie_token(headers) -> str:
    for part in headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME:
            return value
    return ""
