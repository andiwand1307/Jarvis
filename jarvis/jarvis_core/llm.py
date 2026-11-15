"""Thin wrapper around the Anthropic SDK so the agent can be tested with a fake client."""

from __future__ import annotations

from typing import Any, AsyncContextManager, Protocol

import anthropic


class LLM(Protocol):
    def stream(self, **params: Any) -> AsyncContextManager[Any]:
        """Async context manager yielding an object that is async-iterable over stream
        events and has `await get_final_message()`."""


class AnthropicLLM:
    def __init__(self, api_key: str = ""):
        self._api_key = api_key
        self._client: anthropic.AsyncAnthropic | None = None

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            kwargs = {"api_key": self._api_key} if self._api_key else {}
            self._client = anthropic.AsyncAnthropic(max_retries=2, **kwargs)
        return self._client

    def stream(self, **params: Any) -> AsyncContextManager[Any]:
        return self.client.beta.messages.stream(**params)


def api_message(exc: BaseException) -> str:
    """The API's own error message (from the JSON body), shortened; never the raw dump."""
    body = getattr(exc, "body", None)
    msg = ""
    if isinstance(body, dict):
        err = body.get("error")
        msg = err.get("message", "") if isinstance(err, dict) else str(body.get("message", ""))
    msg = " ".join(str(msg).split())
    return msg if len(msg) <= 200 else msg[:199] + "…"


def describe_error(exc: BaseException) -> str:
    """German, user-facing description of an SDK error (no secrets, no raw payloads)."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "Claude lehnt den Zugang ab. Bitte den API-Schlüssel prüfen."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "Der API-Schlüssel hat keine Berechtigung für diese Anfrage."
    if isinstance(exc, anthropic.NotFoundError):
        return "Modell oder Endpunkt nicht gefunden. Bitte JARVIS_MODEL prüfen."
    if isinstance(exc, anthropic.RateLimitError):
        return "Claude ist gerade ausgelastet (Ratenlimit). Bitte gleich noch einmal versuchen."
    if isinstance(exc, anthropic.BadRequestError):
        detail = api_message(exc)
        low = detail.lower()
        if "thinking" in low or "signature" in low or "bound to a different conversation" in low:
            return ("Dieses Gespräch lässt sich nach einer Änderung der Einstellungen nicht fortsetzen. "
                    "Bitte ein neues Gespräch beginnen.")
        if "prompt is too long" in low or "too many tokens" in low or "context" in low and "limit" in low:
            return "Das Gespräch ist zu lang geworden. Bitte ein neues Gespräch beginnen."
        return "Claude hat die Anfrage als ungültig abgelehnt" + (f" ({detail})." if detail else ".")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code == 529 or exc.status_code >= 500:
            return "Claude ist gerade überlastet oder nicht verfügbar. Bitte gleich noch einmal versuchen."
        return f"Fehler von Claude (Status {exc.status_code})."
    if isinstance(exc, anthropic.APITimeoutError):
        return "Claude hat nicht rechtzeitig geantwortet. Bitte noch einmal versuchen."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Keine Verbindung zu Claude. Bitte Netzwerk prüfen."
    if isinstance(exc, TypeError) and "authentication" in str(exc).lower():
        # Raised by the SDK when no credential could be resolved.
        return ("Kein Claude-Zugang eingerichtet. Bitte ANTHROPIC_API_KEY setzen "
                "(im Add-on: Option anthropic_api_key).")
    if isinstance(exc, anthropic.AnthropicError):
        return "Claude ist nicht eingerichtet oder nicht erreichbar."
    return f"Unerwarteter Fehler ({type(exc).__name__})."
