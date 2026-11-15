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


def describe_error(exc: BaseException) -> str:
    """German, user-facing description of an SDK error (no secrets)."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "Claude lehnt den Zugang ab. Bitte den API-Schlüssel prüfen."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "Der API-Schlüssel hat keine Berechtigung für diese Anfrage."
    if isinstance(exc, anthropic.NotFoundError):
        return "Modell oder Endpunkt nicht gefunden. Bitte JARVIS_MODEL prüfen."
    if isinstance(exc, anthropic.RateLimitError):
        return "Claude ist gerade ausgelastet (Ratenlimit). Bitte gleich noch einmal versuchen."
    if isinstance(exc, anthropic.BadRequestError):
        return f"Claude hat die Anfrage abgelehnt: {getattr(exc, 'message', exc)}"
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500:
            return f"Claude ist gerade nicht verfügbar (Fehler {exc.status_code}). Bitte später erneut versuchen."
        return f"Fehler von Claude ({exc.status_code}): {getattr(exc, 'message', exc)}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Keine Verbindung zu Claude. Bitte Netzwerk prüfen."
    if isinstance(exc, TypeError) and "authentication" in str(exc).lower():
        # Raised by the SDK when no credential could be resolved.
        return ("Kein Claude-Zugang eingerichtet. Bitte ANTHROPIC_API_KEY setzen "
                "(im Add-on: Option anthropic_api_key).")
    if isinstance(exc, anthropic.AnthropicError):
        return f"Claude ist nicht eingerichtet oder nicht erreichbar: {exc}"
    return f"Unerwarteter Fehler: {type(exc).__name__}"
