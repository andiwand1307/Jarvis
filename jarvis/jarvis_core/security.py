"""Access control (architecture doc, section 4)."""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlsplit

from .config import INGRESS_GATEWAY, Config

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
PUBLIC_PATHS = frozenset({"api/health"})
COOKIE_NAME = "jarvis_token"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _bearer(headers) -> str:
    auth = headers.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def token_matches(config: Config, supplied: str) -> bool:
    return bool(supplied and config.token) and hmac.compare_digest(supplied.encode(), config.token.encode())


def cookie_value(config: Config) -> str:
    """Value stored in the cookie: derived from the token, never the token itself."""
    return hmac.new(config.token.encode(), b"jarvis-cookie-v1", hashlib.sha256).hexdigest()


def cookie_token(headers) -> str:
    for part in headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME:
            return value
    return ""


def _host_name(value: str) -> str:
    """Host header to bare host name (port and IPv6 brackets removed)."""
    return (urlsplit("//" + value.strip()).hostname or "").lower()


def _local_request_ok(method: str, headers) -> bool:
    """DNS-rebinding guard for loopback-only mode: Host and (for writes) Origin must be loopback."""
    if _host_name(headers.get("host", "")) not in LOOPBACK_NAMES:
        return False
    if method in UNSAFE_METHODS and "origin" in headers:
        origin = headers["origin"]
        if origin == "null" or (urlsplit(origin).hostname or "").lower() not in LOOPBACK_NAMES:
            return False
    return True


def is_allowed(config: Config, client_host: str | None, path: str, headers, query,
               method: str = "GET") -> tuple[bool, int]:
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
        if host not in LOOPBACK or not _local_request_ok(method.upper(), headers):
            return False, 403
        return True, 200

    # Token mode: the API needs the token; static UI files and health are public.
    if not path.startswith("api/") or path in PUBLIC_PATHS:
        return True, 200
    if token_matches(config, _bearer(headers)) or token_matches(config, query.get("token", "")):
        return True, 200
    supplied = cookie_token(headers)
    if supplied and hmac.compare_digest(supplied.encode(), cookie_value(config).encode()):
        return True, 200
    return False, 401
