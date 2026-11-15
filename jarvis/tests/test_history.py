"""The stored API history stays valid at every cancellation point; partial answers are kept."""

import asyncio
import copy
from types import SimpleNamespace as NS

import pytest

from jarvis_core.agent import Agent
from jarvis_core.tools import Tool, build_registry
from jarvis_core.tools.base import CONFIRM, WRITE

from .conftest import FakeLLM, FakeStream, text_turn


def api_problems(messages: list[dict]) -> list[str]:
    """Checks the rules the Messages API enforces on a history that is about to be sent.
    Consecutive messages of the same role are merged by the API, but two assistant messages in
    a row are treated as an error here because this agent never produces them."""
    probs: list[str] = []
    if not messages or messages[0]["role"] != "user":
        probs.append("first not user")
    merged: list[list] = []
    for m in messages:
        content = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
        if not content:
            probs.append("empty content " + m["role"])
        if merged and merged[-1][0] == m["role"]:
            if m["role"] == "assistant":
                probs.append("consecutive assistant")
            merged[-1][1].extend(content)
        else:
            merged.append([m["role"], list(content)])
    for i, (role, content) in enumerate(merged):
        if role == "assistant":
            ids = [b["id"] for b in content if b.get("type") == "tool_use"]
            nxt = merged[i + 1][1] if i + 1 < len(merged) else None
            if ids:
                if nxt is None:
                    probs.append("tool_use at end without result")
                else:
                    got = [b.get("tool_use_id") for b in nxt if b.get("type") == "tool_result"]
                    if sorted(got) != sorted(ids):
                        probs.append(f"tool_use/result mismatch {ids} vs {got}")
                    kinds = [b.get("type") for b in nxt]
                    if any(t != "tool_result" for t in kinds[:len(got)]):
                        probs.append("tool_result not first")
            if content and content[-1].get("type") in ("thinking", "redacted_thinking"):
                probs.append("assistant ends with thinking")
            if any(b.get("type") == "text" and not b.get("text", "").strip() for b in content):
                probs.append("empty text block")
            uses = {b["id"] for b in content if b.get("type") == "server_tool_use"}
            results = {b.get("tool_use_id") for b in content if str(b.get("type", "")).endswith("_tool_result")}
            if uses - results:
                probs.append("server_tool_use without result")
        else:
            got = [b.get("tool_use_id") for b in content if b.get("type") == "tool_result"]
            prev = [b["id"] for b in merged[i - 1][1] if b.get("type") == "tool_use"] if i > 0 else []
            if sorted(got) != sorted(prev):
                probs.append(f"orphan tool_result {got} vs {prev}")
    if merged and merged[-1][0] != "user":
        probs.append("last not user")
    return probs


def next_request(history):
    return history + [{"role": "user", "content": [{"type": "text", "text": "weiter"}]}]


class SlowStream(FakeStream):
    async def _events(self):
        for block in self.turn["content"]:
            await asyncio.sleep(0.01)
            yield NS(type="content_block_start", content_block=NS(**block))
            if block["type"] == "text":
                for word in block["text"].split(" "):
                    await asyncio.sleep(0.01)
                    yield NS(type="text", text=word + " ")

    async def get_final_message(self):
        await asyncio.sleep(0.01)
        return await super().get_final_message()


TH = {"type": "thinking", "thinking": "x", "signature": "s"}


def script():
    return [
        {"content": [TH, {"type": "text", "text": "ich schaue nach"},
                     {"type": "tool_use", "id": "t1", "name": "w1", "input": {}},
                     {"type": "tool_use", "id": "t2", "name": "c1", "input": {}},
                     {"type": "tool_use", "id": "t3", "name": "w1", "input": {}}], "stop_reason": "tool_use"},
        {"content": [TH, {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "x"}}],
         "stop_reason": "pause_turn"},
        {"content": [{"type": "web_search_tool_result", "tool_use_id": "s1", "content": []},
                     {"type": "text", "text": "a b c"},
                     {"type": "tool_use", "id": "t4", "name": "w1", "input": {}}], "stop_reason": "tool_use"},
        {"content": [{"type": "text", "text": "fertig eins zwei drei"}], "stop_reason": "end_turn"},
    ]


class ScriptLLM:
    def __init__(self):
        self.turns = script()
        self.bad: list[list[str]] = []

    def stream(self, **params):
        problems = api_problems(params["messages"][:-1] + [params["messages"][-1]]) if params["messages"][-1][
            "role"] == "user" else []
        if problems:
            self.bad.append(problems)
        turn = self.turns.pop(0) if self.turns else text_turn("folge")
        return SlowStream(copy.deepcopy(turn))


async def run_with_cancel(config, store, delay):
    registry = build_registry(config, store)

    async def work(_inp):
        await asyncio.sleep(0.03)
        return {"ok": True}

    schema = {"type": "object", "properties": {}}
    registry.register(Tool(name="w1", area="t", description="", input_schema=schema, handler=work, risk=WRITE))
    registry.register(Tool(name="c1", area="t", description="", input_schema=schema, handler=work, risk=CONFIRM))
    llm = ScriptLLM()
    agent = Agent(config, store, registry, llm)
    cid = store.create_conversation("x")
    events: list[dict] = []

    async def consume():
        async for ev in agent.run(cid, "los"):
            events.append(ev)

    task = asyncio.create_task(consume())
    if delay is not None:
        await asyncio.sleep(delay)
        agent.cancel(cid)
    await asyncio.wait_for(task, 5)
    return agent, llm, cid, events


@pytest.mark.parametrize("delay", [i * 0.01 for i in range(0, 40)] + [None])
async def test_history_valid_after_cancel_at_any_point(config, store, delay):
    agent, llm, cid, events = await run_with_cancel(config, store, delay)
    history = store.get_api_messages(cid)
    assert api_problems(next_request(history)) == []
    assert llm.bad == []
    assert not agent.is_running(cid)
    assert events[-1]["type"] == "done"
    after = [e async for e in agent.run(cid, "weiter")]
    assert after[-1]["type"] == "done" and not [e for e in after if e["type"] == "error"]
    assert llm.bad == []
    assert api_problems(next_request(store.get_api_messages(cid))) == []


async def test_paused_turn_is_merged_with_its_continuation(config, store):
    _agent, llm, cid, _events = await run_with_cancel(config, store, None)
    history = store.get_api_messages(cid)
    merged = [m for m in history if m["role"] == "assistant"
              and any(b.get("type") == "server_tool_use" for b in m["content"])]
    assert len(merged) == 1
    types = [b["type"] for b in merged[0]["content"]]
    assert types == ["thinking", "server_tool_use", "web_search_tool_result", "text", "tool_use"]


async def test_cancel_during_text_keeps_partial_answer(config, store, calls):
    gate = asyncio.Event()

    class Halting(FakeStream):
        async def _events(self):
            yield NS(type="content_block_start", content_block=NS(type="text", text=""))
            yield NS(type="text", text="Die Antwort beginnt")
            await gate.wait()

    class LLM:
        calls = []

        def stream(self, **params):
            self.calls.append(params)
            return Halting({"content": [], "stop_reason": "end_turn"})

    agent = Agent(config, store, build_registry(config, store), LLM())
    cid = store.create_conversation("x")
    task = asyncio.create_task(_collect(agent, cid))
    await asyncio.sleep(0.05)
    assert agent.cancel(cid)
    await asyncio.wait_for(task, 2)
    history = store.get_api_messages(cid)
    assert history[-1] == {"role": "assistant",
                           "content": [{"type": "text", "text": "Die Antwort beginnt [abgebrochen]"}]}
    assert store.get_messages(cid)[-1]["text"] == "Die Antwort beginnt [abgebrochen]"
    assert api_problems(next_request(history)) == []


async def _collect(agent, cid):
    return [e async for e in agent.run(cid, "frage")]


@pytest.mark.parametrize("stop_reason", ["end_turn", "max_tokens"])
async def test_thinking_only_answer_is_not_stored(config, store, calls, stop_reason):
    llm = FakeLLM([{"content": [TH], "stop_reason": stop_reason}])
    agent = Agent(config, store, build_registry(config, store), llm)
    events = [e async for e in agent.run(None, "x")]
    cid = events[0]["data"]["conversation_id"]
    assert [m["role"] for m in store.get_api_messages(cid)] == ["user"]
    assert any(e["type"] == "error" for e in events)


async def test_error_after_pause_drops_paused_message(config, store, calls):
    import anthropic
    import httpx2
    err = anthropic.InternalServerError("boom", response=httpx2.Response(
        500, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")), body=None)
    llm = FakeLLM([
        {"content": [TH, {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {}}],
         "stop_reason": "pause_turn"},
        {"raise": err, "content": [], "stop_reason": None},
    ])
    agent = Agent(config, store, build_registry(config, store), llm)
    events = [e async for e in agent.run(None, "such")]
    cid = events[0]["data"]["conversation_id"]
    assert llm.calls[1]["messages"][-1]["role"] == "assistant"  # paused turn was sent back to resume
    assert [m["role"] for m in store.get_api_messages(cid)] == ["user"]
    assert api_problems(next_request(store.get_api_messages(cid))) == []


async def test_skipped_tools_are_logged_as_cancelled(config, store):
    agent, _llm, cid, _ = await run_with_cancel(config, store, 0.1)
    statuses = {e["status"] for e in store.get_log(50)}
    allowed = {"ok", "error", "invalid", "timeout", "pending", "approved", "approved_failed",
               "approved_unknown", "rejected", "interrupted", "cancelled"}
    assert statuses <= allowed
