"""Channel-independent agent loop: `Agent.run()` yields a stream of events."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import anthropic

from .config import Config
from .llm import LLM, describe_error
from .prompts import CHANNELS, context_block, system_blocks
from .store import Store
from .tools import CONFIRM, Registry, ToolError, server_tools, validate_input
from .tools.base import OutcomeUnknown, readable

log = logging.getLogger("jarvis.agent")

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Thinking blocks are bound to system + tools + earlier messages. When the configuration changes
# (new integration, updated prompt) old blocks no longer match; drop them instead of failing.
BINDING_BETA = "thinking-binding-controls-2026-08-01"
TOOL_TIMEOUT = 90.0
PENDING_TEXT = "wartet auf Freigabe durch den Nutzer"
CANCELLED_TEXT = "vom Nutzer abgebrochen"
UNKNOWN_TEXT = ("Ergebnis unbekannt: Zeitüberschreitung, nachdem die Aktion gestartet wurde. Sie kann "
                "stattgefunden haben. Nicht erneut senden, erst nachsehen.")
SERVER_TOOL_LABELS = {"web_search": "Suche im Web …", "web_fetch": "Lese eine Webseite …"}
MAX_CONTEXT_NOTES = 30
MAX_CONTEXT_NOTE_CHARS = 4000


class Cancelled(Exception):
    pass


def event(_type: str, **data: Any) -> dict:
    return {"type": _type, "data": data}


def block_to_dict(block: Any) -> dict:
    if isinstance(block, dict):
        return dict(block)
    return block.to_dict(mode="json", exclude_none=True)


def history_content(blocks: list[dict]) -> list[dict]:
    """Content to echo back. After a mid-output fallback, model-internal blocks before the
    last `fallback` block are omitted; the fallback block stays where it was."""
    boundary = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=None)
    if boundary is None:
        return blocks
    result_ids = {b.get("tool_use_id") for b in blocks[:boundary] if str(b.get("type", "")).endswith("_tool_result")}
    out = []
    for i, b in enumerate(blocks):
        t = str(b.get("type", ""))
        if i < boundary:
            if t == "server_tool_use" and b.get("id") not in result_ids:
                continue
            if t not in ("text", "fallback", "server_tool_use") and not t.endswith("_tool_result"):
                continue
        out.append(b)
    return out


def storable(content: list[dict]) -> bool:
    """An assistant message may be stored only if it carries text or a tool call, does not end
    with a thinking block and has no server tool call without its result."""
    if not any(b.get("type") in ("text", "tool_use") for b in content):
        return False
    if content[-1].get("type") in ("thinking", "redacted_thinking"):
        return False
    uses = {b.get("id") for b in content if b.get("type") == "server_tool_use"}
    results = {b.get("tool_use_id") for b in content if str(b.get("type", "")).endswith("_tool_result")}
    return uses <= results


def _tool_result(tool_use_id: str, content: Any, is_error: bool = False) -> dict:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}
    if is_error:
        block["is_error"] = True
    return block


async def _until_cancelled(agen: AsyncIterator[dict], cancel: asyncio.Event) -> AsyncIterator[dict]:
    """Iterates `agen` but stops (closing it) as soon as `cancel` is set."""
    it = agen.__aiter__()
    try:
        while True:
            if cancel.is_set():
                raise Cancelled()
            nxt = asyncio.ensure_future(it.__anext__())
            waiter = asyncio.ensure_future(cancel.wait())
            done, _ = await asyncio.wait({nxt, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if nxt in done:
                waiter.cancel()
                try:
                    item = nxt.result()
                except StopAsyncIteration:
                    return
                yield item
            else:
                nxt.cancel()
                try:
                    await nxt
                except (asyncio.CancelledError, StopAsyncIteration, Exception):  # noqa: BLE001
                    pass
                raise Cancelled()
    finally:
        aclose = getattr(it, "aclose", None)
        if aclose:
            try:
                await aclose()
            except Exception:  # noqa: BLE001
                pass


class Agent:
    def __init__(self, config: Config, store: Store, registry: Registry, llm: LLM):
        self.config = config
        self.store = store
        self.registry = registry
        self.llm = llm
        self._locks: dict[str, asyncio.Lock] = {}
        self._cancel: dict[str, asyncio.Event] = {}

    # Request -----------------------------------------------------------

    def _params(self, channel: str, messages: list[dict]) -> dict:
        cfg = self.config
        params: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "system": system_blocks(channel, cfg.user_name),
            "tools": self.registry.api_definitions() + server_tools(cfg),
            "messages": messages,
            "thinking": {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
            "output_config": {"effort": cfg.effort},
            "cache_control": {"type": "ephemeral"},
            "betas": [BINDING_BETA],
        }
        if cfg.fallbacks:
            params["betas"].append(FALLBACK_BETA)
            params["fallbacks"] = "default"
        return params

    # Running state -------------------------------------------------------

    def is_running(self, cid: str) -> bool:
        lock = self._locks.get(cid)
        return bool(lock and lock.locked())

    def cancel(self, cid: str) -> bool:
        """Asks a running request to stop. Returns whether one was running."""
        ev = self._cancel.get(cid)
        if not self.is_running(cid) or ev is None:
            return False
        ev.set()
        return True

    # Main loop -----------------------------------------------------------

    async def run(self, conversation_id: str | None, user_text: str, channel: str = "web") -> AsyncIterator[dict]:
        if channel not in CHANNELS:
            channel = "web"
        if conversation_id:
            if not self.store.get_conversation(conversation_id):
                yield event("error", message="Gespräch nicht gefunden.")
                yield event("done")
                return
        else:
            conversation_id = self.store.create_conversation(user_text, channel)

        lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        if lock.locked():
            yield event("error", message="In diesem Gespräch läuft gerade schon eine Anfrage.")
            yield event("done")
            return
        async with lock:
            cancel = asyncio.Event()
            self._cancel[conversation_id] = cancel
            reply: list[str] = []
            try:
                yield event("start", conversation_id=conversation_id)
                self.store.add_message(conversation_id, "user", user_text)
                try:
                    async for ev in self._loop(conversation_id, user_text, channel, reply, cancel):
                        yield ev
                except Cancelled:
                    yield event("error", message="Abgebrochen.", cancelled=True)
                except anthropic.APIError as exc:
                    log.warning("Claude API error: %s", type(exc).__name__)
                    yield event("error", message=describe_error(exc))
                except Exception as exc:  # noqa: BLE001 - every failure must end as an error event
                    if isinstance(exc, TypeError) and "authentication" in str(exc).lower():
                        log.warning("no Claude credentials configured")
                    else:
                        log.exception("agent run failed")
                    yield event("error", message=describe_error(exc))
                yield event("done")
            finally:
                text = "".join(reply).strip()
                if text:
                    self.store.add_message(conversation_id, "assistant", text)
                self._cancel.pop(conversation_id, None)

    async def _loop(self, cid: str, user_text: str, channel: str, reply: list[str],
                    cancel: asyncio.Event) -> AsyncIterator[dict]:
        history = self.store.get_api_messages(cid)
        notes = self._context_notes() if not history else None
        resolved = self.store.take_unreported(cid)
        context = context_block(self.config.timezone, notes=notes, resolved=resolved)
        user_msg = {"role": "user", "content": [
            {"type": "text", "text": context},
            {"type": "text", "text": user_text},
        ]}
        self.store.append_api_messages(cid, [user_msg])
        messages = history + [user_msg]

        json_retries = 0
        # Content of a turn that ended with pause_turn. It is sent back as the last message so the
        # server can resume, but only stored once the turn completes, merged with its
        # continuation; on cancel, error or step limit it is dropped, never left dangling.
        paused: list[dict] = []
        segment_start = len(reply)
        for _step in range(max(1, self.config.max_steps)):
            if cancel.is_set():
                self._store_partial(cid, messages, reply, segment_start)
                raise Cancelled()
            request = messages + ([{"role": "assistant", "content": paused}] if paused else [])
            try:
                content, stop_reason = None, None
                async for item in _until_cancelled(self._stream_turn(channel, request, reply), cancel):
                    if item["type"] == "_final":
                        content, stop_reason = item["content"], item["stop_reason"]
                    else:
                        yield item
            except ValueError:
                # Tool input JSON the SDK could not parse at all; re-issue the turn (bounded).
                json_retries += 1
                if json_retries > 2:
                    yield event("error", message="Claude hat ungültige Werkzeugdaten geliefert.")
                    return
                continue
            except Cancelled:
                self._store_partial(cid, messages, reply, segment_start)
                raise
            json_retries = 0
            content = [b for b in history_content(content or [])
                       if not (b.get("type") == "text" and not str(b.get("text", "")).strip())]
            combined = paused + content

            if stop_reason == "refusal":
                yield event("error", message="Claude hat diese Anfrage abgelehnt.")
                return

            tool_uses = [b for b in combined if b.get("type") == "tool_use"]
            if stop_reason == "max_tokens":
                if not tool_uses and storable(combined):
                    self._append(cid, messages, {"role": "assistant", "content": combined})
                yield event("error", message="Die Antwort wurde wegen der Längenbegrenzung abgeschnitten.")
                return

            if stop_reason == "pause_turn":
                if not content:
                    yield event("error", message="Claude hat die Arbeit unterbrochen und keine Antwort geliefert.")
                    return
                paused = combined
                continue

            if stop_reason == "tool_use" and tool_uses:
                assistant = {"role": "assistant", "content": combined}
                paused = []
                results = []
                stopped = False
                for block in tool_uses:
                    if stopped or cancel.is_set():
                        stopped = True
                        results.append(_tool_result(block.get("id", ""), CANCELLED_TEXT, True))
                        self.store.log(tool=block.get("name", ""), risk="-", tool_input=block.get("input"),
                                       result=CANCELLED_TEXT, status="cancelled", conversation_id=cid)
                        continue
                    async for item in self._execute(cid, block):
                        if item["type"] == "_result":
                            results.append(item["block"])
                        else:
                            yield item
                self._append(cid, messages, assistant, {"role": "user", "content": results})
                segment_start = len(reply)
                if stopped or cancel.is_set():
                    raise Cancelled()
                continue

            if not storable(combined):
                # e.g. only thinking: storing it would leave an invalid history.
                yield event("error", message="Claude hat keine Antwort geliefert. Bitte noch einmal versuchen.")
                return
            self._append(cid, messages, {"role": "assistant", "content": combined})
            return

        yield event("error", message="Abgebrochen: zu viele Arbeitsschritte in einer Anfrage.")

    def _store_partial(self, cid: str, messages: list[dict], reply: list[str], start: int) -> None:
        """Keeps text already shown to the user in the history, marked as cancelled."""
        text = "".join(reply[start:]).strip()
        if not text:
            return
        reply.append(" [abgebrochen]")
        last = messages[-1] if messages else None
        if last is None or last["role"] != "user":
            return  # an assistant turn cannot follow another; display still shows the text
        self._append(cid, messages, {"role": "assistant",
                                     "content": [{"type": "text", "text": text + " [abgebrochen]"}]})

    def _context_notes(self) -> list[dict]:
        notes, total = [], 0
        for n in self.store.search_notes(limit=MAX_CONTEXT_NOTES):
            total += len(n["text"])
            if total > MAX_CONTEXT_NOTE_CHARS:
                break
            notes.append(n)
        return notes

    def _append(self, cid: str, messages: list[dict], *new: dict) -> None:
        messages.extend(new)
        self.store.append_api_messages(cid, list(new))

    async def _stream_turn(self, channel: str, messages: list[dict], reply: list[str]) -> AsyncIterator[dict]:
        last_btype = None
        async with self.llm.stream(**self._params(channel, messages)) as stream:
            async for ev in stream:
                etype = getattr(ev, "type", None)
                if etype == "text":
                    reply.append(ev.text)
                    yield event("text", delta=ev.text)
                elif etype == "content_block_start":
                    block = ev.content_block
                    btype = getattr(block, "type", None)
                    prev, last_btype = last_btype, btype
                    if btype == "server_tool_use":
                        name = getattr(block, "name", "web_search")
                        yield event("tool", name=name, label=SERVER_TOOL_LABELS.get(name, "Arbeite …"),
                                    status="running")
                    elif btype in ("web_search_tool_result", "web_fetch_tool_result"):
                        name = "web_search" if btype == "web_search_tool_result" else "web_fetch"
                        failed = not isinstance(getattr(block, "content", None), list) and btype == "web_search_tool_result"
                        yield event("tool", name=name, label=SERVER_TOOL_LABELS[name],
                                    status="error" if failed else "done")
                    elif btype == "text" and prev != "text" and reply and not reply[-1].endswith("\n"):
                        # Separate text that follows tool work; cited text blocks stay joined.
                        reply.append("\n\n")
                        yield event("text", delta="\n\n")
            final = await stream.get_final_message()
        yield {
            "type": "_final",
            "content": [block_to_dict(b) for b in final.content],
            "stop_reason": final.stop_reason,
        }

    # Tools -----------------------------------------------------------------

    async def _execute(self, cid: str, block: dict) -> AsyncIterator[dict]:
        name, tool_input, tuid = block.get("name", ""), block.get("input"), block.get("id", "")
        tool = self.registry.get(name)
        label = tool.label if tool and tool.label else name
        yield event("tool", name=name, label=label, status="running")

        if tool is None:
            yield event("tool", name=name, label=label, status="error")
            yield {"type": "_result", "block": _tool_result(tuid, f"Unbekanntes Werkzeug: {name}", True)}
            return
        errors = validate_input(tool.input_schema, tool_input)
        if not errors and tool.precheck:
            try:
                tool.precheck(tool_input)
            except ToolError as exc:
                errors = [str(exc)]
        if errors:
            self.store.log(tool=name, risk=tool.risk_for(tool_input if isinstance(tool_input, dict) else {}),
                           tool_input=tool_input, result="; ".join(errors), status="invalid",
                           conversation_id=cid)
            yield event("tool", name=name, label=label, status="error")
            payload = {"INVALID_INPUT": errors, "input": json.dumps(tool_input, ensure_ascii=False)}
            yield {"type": "_result", "block": _tool_result(tuid, payload, True)}
            return

        risk = tool.risk_for(tool_input)
        if risk == CONFIRM:
            kind, summary, details = tool.pending_info(tool_input)
            pending = self.store.create_pending(
                conversation_id=cid, tool=name, kind=kind, summary=summary,
                details=details, tool_input=tool_input,
            )
            self.store.log(tool=name, risk=risk, tool_input=tool_input,
                           result=f"offene Aktion {pending['id']}", status="pending", conversation_id=cid)
            yield event("tool", name=name, label=label, status="done")
            yield event("pending", id=pending["id"], kind=kind, summary=summary, details=details,
                        created_at=pending["created_at"])
            yield {"type": "_result", "block": _tool_result(tuid, {
                "status": PENDING_TEXT,
                "action_id": pending["id"],
                "summary": summary,
                "hinweis": "Nicht ausgeführt. Der Nutzer muss die Aktion in der Oberfläche freigeben.",
            })}
            return

        status, result = await self._run_handler(tool, tool_input)
        ok = status == "ok"
        self.store.log(tool=name, risk=risk, tool_input=tool_input, result=_text(result),
                       status=status, conversation_id=cid)
        yield event("tool", name=name, label=label, status="done" if ok else "error")
        yield {"type": "_result", "block": _tool_result(tuid, result, not ok)}

    async def _run_handler(self, tool, tool_input: dict) -> tuple[str, Any]:
        """(status, result) with status `ok`, `error` or `timeout` (= outcome unknown)."""
        try:
            return "ok", await asyncio.wait_for(tool.handler(tool_input), timeout=TOOL_TIMEOUT)
        except OutcomeUnknown as exc:
            return "timeout", str(exc)
        except ToolError as exc:
            return "error", str(exc)
        except asyncio.TimeoutError:
            return "timeout", UNKNOWN_TEXT
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s failed", tool.name)
            return "error", f"Werkzeugfehler: {type(exc).__name__}: {exc}"

    # Pending actions -------------------------------------------------------

    async def approve(self, pending_id: str) -> tuple[str, str] | None:
        """Runs an approved action once. Returns (status, text) with status `done`, `failed` or
        `unknown`; None if no such pending action exists (or it was resolved already)."""
        action = self.store.get_pending(pending_id)
        if not action or not self.store.claim_pending(pending_id, "executing"):
            return None
        tool = self.registry.get(action["tool"])
        if tool is None:
            status, result = "error", "Werkzeug ist nicht mehr eingerichtet."
        else:
            status, result = await self._run_handler(tool, action["input"])
        text = readable(result)
        final = {"ok": "done", "error": "failed", "timeout": "unknown"}[status]
        self.store.finish_pending(pending_id, final, text)
        self.store.log(tool=action["tool"], risk=CONFIRM, tool_input=action["input"], result=text,
                       status={"ok": "approved", "error": "approved_failed", "timeout": "approved_unknown"}[status],
                       conversation_id=action["conversation_id"])
        return final, text

    def reject(self, pending_id: str) -> bool:
        action = self.store.get_pending(pending_id)
        if not action or not self.store.claim_pending(pending_id, "rejected"):
            return False
        self.store.finish_pending(pending_id, "rejected", "")
        self.store.log(tool=action["tool"], risk=CONFIRM, tool_input=action["input"], result="verworfen",
                       status="rejected", conversation_id=action["conversation_id"])
        return True


def _text(result: Any) -> str:
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
