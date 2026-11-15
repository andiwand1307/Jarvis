"""Channel-independent agent loop: `Agent.run()` yields a stream of events."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from .config import Config
from .llm import LLM, describe_error
from .prompts import CHANNELS, context_block, system_blocks
from .store import Store
from .tools import CONFIRM, Registry, ToolError, server_tools, validate_input

log = logging.getLogger("jarvis.agent")

FALLBACK_BETA = "server-side-fallback-2026-07-01"
TOOL_TIMEOUT = 90.0
PENDING_TEXT = "wartet auf Freigabe durch den Nutzer"
SERVER_TOOL_LABELS = {"web_search": "Suche im Web …", "web_fetch": "Lese eine Webseite …"}


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


def _tool_result(tool_use_id: str, content: Any, is_error: bool = False) -> dict:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": text}
    if is_error:
        block["is_error"] = True
    return block


class Agent:
    def __init__(self, config: Config, store: Store, registry: Registry, llm: LLM):
        self.config = config
        self.store = store
        self.registry = registry
        self.llm = llm
        self._locks: dict[str, asyncio.Lock] = {}

    # Request -----------------------------------------------------------

    def _params(self, channel: str, messages: list[dict]) -> dict:
        cfg = self.config
        params: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "system": system_blocks(channel, cfg.user_name),
            "tools": self.registry.api_definitions() + server_tools(cfg),
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": cfg.effort},
            "cache_control": {"type": "ephemeral"},
        }
        if cfg.fallbacks:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        return params

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
            yield event("start", conversation_id=conversation_id)
            self.store.add_message(conversation_id, "user", user_text)
            reply: list[str] = []
            try:
                async for ev in self._loop(conversation_id, user_text, channel, reply):
                    yield ev
            except Exception as exc:  # noqa: BLE001 - every failure must end as an error event
                log.exception("agent run failed")
                yield event("error", message=describe_error(exc))
            finally:
                text = "".join(reply).strip()
                if text:
                    self.store.add_message(conversation_id, "assistant", text)
            yield event("done")

    async def _loop(self, cid: str, user_text: str, channel: str, reply: list[str]) -> AsyncIterator[dict]:
        history = self.store.get_api_messages(cid)
        notes = self.store.search_notes(limit=30) if not history else None
        resolved = self.store.take_unreported(cid)
        context = context_block(self.config.timezone, notes=notes, resolved=resolved)
        user_msg = {"role": "user", "content": [
            {"type": "text", "text": context},
            {"type": "text", "text": user_text},
        ]}
        self.store.append_api_messages(cid, [user_msg])
        messages = history + [user_msg]

        json_retries = 0
        for _step in range(max(1, self.config.max_steps)):
            try:
                content, stop_reason = None, None
                async for item in self._stream_turn(channel, messages, reply):
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
            json_retries = 0
            content = history_content(content or [])

            if stop_reason == "refusal":
                yield event("error", message="Claude hat diese Anfrage abgelehnt.")
                return

            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            if stop_reason == "max_tokens":
                if not tool_uses:
                    self._append(cid, messages, {"role": "assistant", "content": content})
                yield event("error", message="Die Antwort wurde wegen der Längenbegrenzung abgeschnitten.")
                return

            assistant = {"role": "assistant", "content": content}
            if stop_reason == "pause_turn":
                self._append(cid, messages, assistant)
                continue

            if stop_reason == "tool_use" and tool_uses:
                results = []
                for block in tool_uses:
                    async for item in self._execute(cid, block):
                        if item["type"] == "_result":
                            results.append(item["block"])
                        else:
                            yield item
                self._append(cid, messages, assistant, {"role": "user", "content": results})
                continue

            self._append(cid, messages, assistant)
            return

        yield event("error", message="Abgebrochen: zu viele Arbeitsschritte in einer Anfrage.")

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
            yield event("pending", id=pending["id"], kind=kind, summary=summary, details=details)
            yield {"type": "_result", "block": _tool_result(tuid, {
                "status": PENDING_TEXT,
                "action_id": pending["id"],
                "summary": summary,
                "hinweis": "Nicht ausgeführt. Der Nutzer muss die Aktion in der Oberfläche freigeben.",
            })}
            return

        ok, result = await self._run_handler(tool, tool_input)
        self.store.log(tool=name, risk=risk, tool_input=tool_input, result=_text(result),
                       status="ok" if ok else "error", conversation_id=cid)
        yield event("tool", name=name, label=label, status="done" if ok else "error")
        yield {"type": "_result", "block": _tool_result(tuid, result, not ok)}

    async def _run_handler(self, tool, tool_input: dict) -> tuple[bool, Any]:
        try:
            return True, await asyncio.wait_for(tool.handler(tool_input), timeout=TOOL_TIMEOUT)
        except ToolError as exc:
            return False, str(exc)
        except asyncio.TimeoutError:
            return False, "Zeitüberschreitung beim Werkzeug."
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s failed", tool.name)
            return False, f"Werkzeugfehler: {type(exc).__name__}: {exc}"

    # Pending actions -------------------------------------------------------

    async def approve(self, pending_id: str) -> tuple[bool, str] | None:
        """Runs an approved action. None if no such pending action exists (or it was resolved)."""
        action = self.store.get_pending(pending_id)
        if not action or not self.store.claim_pending(pending_id, "executing"):
            return None
        tool = self.registry.get(action["tool"])
        if tool is None:
            ok, result = False, "Werkzeug ist nicht mehr eingerichtet."
        else:
            ok, result = await self._run_handler(tool, action["input"])
        text = _text(result)
        self.store.finish_pending(pending_id, "done" if ok else "failed", text)
        self.store.log(tool=action["tool"], risk=CONFIRM, tool_input=action["input"], result=text,
                       status="approved" if ok else "approved_failed",
                       conversation_id=action["conversation_id"])
        return ok, text

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
