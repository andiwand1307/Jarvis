"""Access control (architecture doc, section 4)."""

from __future__ import annotations

import hashlib
import hmac
import re

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


_LOCAL_HOST = re.compile(r"(127\.0\.0\.1|localhost|\[::1\])(:[0-9]{1,5})?")
_LOCAL_ORIGIN = re.compile(r"https?://(127\.0\.0\.1|localhost|\[::1\])(:[0-9]{1,5})?")
START_PAGES = frozenset({"", "index.html"})


def _origin_ok(method: str, headers, allowed) -> bool:
    """For state-changing requests a present Origin must match exactly."""
    if method not in UNSAFE_METHODS or "origin" not in headers:
        return True
    return allowed(headers["origin"])


def _local_request_ok(method: str, headers) -> bool:
    """DNS-rebinding guard for loopback-only mode: exact Host match, and Origin for writes."""
    if not _LOCAL_HOST.fullmatch(headers.get("host", "")):
        return False
    return _origin_ok(method, headers, lambda o: bool(_LOCAL_ORIGIN.fullmatch(o)))


def same_origin(headers, origin: str) -> bool:
    host = headers.get("host", "")
    return bool(host) and origin in (f"http://{host}", f"https://{host}")


def is_start_page(path: str) -> bool:
    return path.lstrip("/") in START_PAGES


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
    if not _origin_ok(method.upper(), headers, lambda o: same_origin(headers, o)):
        return False, 403
    if not path.startswith("api/") or path in PUBLIC_PATHS:
        return True, 200
    if token_matches(config, _bearer(headers)):
        return True, 200
    supplied = cookie_token(headers)
    if supplied and hmac.compare_digest(supplied.encode(), cookie_value(config).encode()):
        return True, 200
    return False, 401
