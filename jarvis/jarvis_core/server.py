"""FastAPI app: HTTP interface and SSE (architecture doc, section 5)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator
from urllib.parse import parse_qsl, urlencode

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import Agent
from .config import Config
from .llm import LLM, AnthropicLLM
from .security import COOKIE_NAME, cookie_value, is_allowed, token_matches
from .store import Store
from .tools import Registry, build_registry

log = logging.getLogger("jarvis.server")
_BACKGROUND: set[asyncio.Task] = set()


class ChatRequest(BaseModel):
    message: str = Field(..., max_length=20000)
    conversation_id: str | None = None
    channel: str = "web"


def sse(kind: str, data: dict) -> bytes:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


class AccessMiddleware:
    def __init__(self, app, config: Config):
        self.app = app
        self.config = config

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        pairs = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        query = dict(pairs)
        client = scope.get("client")
        path = scope.get("path", "")
        allowed, status = is_allowed(self.config, client[0] if client else None, path, headers, query,
                                     scope.get("method", "GET"))
        if not allowed:
            message = "Anmeldung erforderlich." if status == 401 else "Zugriff verweigert."
            extra = {"WWW-Authenticate": "Bearer"} if status == 401 else None
            response = JSONResponse({"ok": False, "error": message}, status_code=status, headers=extra)
            return await response(scope, receive, send)
        if (self.config.access_mode == "token" and not path.lstrip("/").startswith("api/")
                and token_matches(self.config, query.get("token", ""))):
            # Opening the UI once with ?token=… stores a derived value in a cookie and
            # redirects to the same address without the token.
            https = scope.get("scheme") == "https" or headers.get("x-forwarded-proto", "").lower() == "https"
            cookie = (f"{COOKIE_NAME}={cookie_value(self.config)}; Path=/; HttpOnly; SameSite=Strict; "
                      f"Max-Age=31536000" + ("; Secure" if https else ""))
            rest = urlencode([(k, v) for k, v in pairs if k != "token"])
            target = (path.rsplit("/", 1)[-1] or "./") + (f"?{rest}" if rest else "")
            response = HTMLResponse("", status_code=303,
                                    headers={"Location": target, "Set-Cookie": cookie, "Cache-Control": "no-store"})
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)


def create_app(
    config: Config | None = None,
    store: Store | None = None,
    llm: LLM | None = None,
    registry: Registry | None = None,
) -> FastAPI:
    config = config or Config.from_env()
    store = store or Store(config.db_path)
    registry = registry or build_registry(config, store)
    llm = llm or AnthropicLLM(config.anthropic_api_key)
    agent = Agent(config, store, registry, llm)
    for action in store.recover_interrupted():
        log.warning("action %s was interrupted by a restart; marked unknown", action["id"])

    app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config, app.state.store, app.state.agent = config, store, agent
    app.add_middleware(AccessMiddleware, config=config)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        parts = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
            parts.append(f"{loc}: {err.get('msg', 'ungültig')}" if loc else str(err.get("msg", "ungültig")))
        return JSONResponse({"ok": False, "detail": "Ungültige Anfrage – " + "; ".join(parts)}, status_code=422)

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/api/status")
    async def status() -> dict:
        return {
            "model": config.model,
            "tools": registry.status(),
            "api_key_configured": bool(config.anthropic_api_key),
        }

    @app.post("/api/chat")
    async def chat(req: ChatRequest) -> StreamingResponse:
        text = req.message.strip()
        if not text:
            raise HTTPException(status_code=400, detail="Leere Nachricht.")
        channel = req.channel if req.channel in ("web", "phone", "event") else "web"
        return StreamingResponse(
            _stream(agent, req.conversation_id, text, channel),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/conversations")
    async def conversations() -> list[dict]:
        return store.list_conversations()

    @app.get("/api/conversations/{cid}")
    async def conversation(cid: str) -> dict:
        if not store.get_conversation(cid):
            raise HTTPException(status_code=404, detail="Gespräch nicht gefunden.")
        return {"id": cid, "running": agent.is_running(cid), "messages": store.get_messages(cid)}

    @app.post("/api/conversations/{cid}/cancel")
    async def cancel(cid: str) -> dict:
        if not store.get_conversation(cid):
            raise HTTPException(status_code=404, detail="Gespräch nicht gefunden.")
        return {"ok": True, "was_running": agent.cancel(cid)}

    @app.get("/api/pending")
    async def pending() -> list[dict]:
        return store.list_pending()

    @app.post("/api/pending/{pid}/approve")
    async def approve(pid: str) -> JSONResponse:
        outcome = await agent.approve(pid)
        if outcome is None:
            return JSONResponse({"ok": False, "result": "Keine offene Aktion mit dieser ID."}, status_code=404)
        ok, result = outcome
        return JSONResponse({"ok": ok, "result": result})

    @app.post("/api/pending/{pid}/reject")
    async def reject(pid: str) -> JSONResponse:
        if not agent.reject(pid):
            return JSONResponse({"ok": False, "error": "Keine offene Aktion mit dieser ID."}, status_code=404)
        return JSONResponse({"ok": True})

    @app.get("/api/log")
    async def get_log(limit: int = Query(50, ge=1, le=500)) -> list[dict]:
        return store.get_log(limit)

    if config.web_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(config.web_dir), html=True), name="web")
    else:
        log.warning("web directory %s not found; serving placeholder", config.web_dir)

        @app.get("/", response_class=HTMLResponse)
        async def placeholder() -> str:
            return "<!doctype html><meta charset=utf-8><title>Jarvis</title><p>Jarvis läuft. Die Oberfläche fehlt.</p>"

    return app


async def _stream(agent: Agent, cid: str | None, text: str, channel: str) -> AsyncIterator[bytes]:
    """Runs the agent in its own task. If the client goes away, the run is cancelled at the
    next safe point (before the next model call or tool), keeping the history valid."""
    queue: asyncio.Queue[Any] = asyncio.Queue()
    sentinel = object()
    started: dict[str, str] = {}

    async def produce() -> None:
        try:
            async for ev in agent.run(cid, text, channel):
                if ev["type"] == "start":
                    started["cid"] = ev["data"]["conversation_id"]
                await queue.put(ev)
        except Exception:  # noqa: BLE001
            log.exception("chat stream failed")
            await queue.put({"type": "error", "data": {"message": "Interner Fehler."}})
            await queue.put({"type": "done", "data": {}})
        finally:
            await queue.put(sentinel)

    task = asyncio.create_task(produce())
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    finished = False
    try:
        while True:
            ev = await queue.get()
            if ev is sentinel:
                finished = True
                break
            yield sse(ev["type"], ev["data"])
    finally:
        if not finished and started.get("cid"):
            agent.cancel(started["cid"])
