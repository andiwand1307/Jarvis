"""Shared fixtures: a scripted fake Claude client, temporary store, app factory."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from jarvis_core.config import Config
from jarvis_core.server import create_app
from jarvis_core.store import Store
from jarvis_core.tools import Registry, Tool, build_registry
from jarvis_core.tools.base import CONFIRM, READ, WRITE


def text_turn(text: str, stop_reason: str = "end_turn") -> dict:
    return {"content": [{"type": "text", "text": text}], "stop_reason": stop_reason}


def tool_turn(name: str, tool_input: dict, tool_id: str = "toolu_1", text: str = "", thinking: bool = True) -> dict:
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": "", "signature": "sig-" + tool_id})
    if text:
        content.append({"type": "text", "text": text})
    content.append({"type": "tool_use", "id": tool_id, "name": name, "input": tool_input})
    return {"content": content, "stop_reason": "tool_use"}


class FakeStream:
    def __init__(self, turn: dict):
        self.turn = turn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._events()

    async def _events(self):
        if self.turn.get("raise"):
            raise self.turn["raise"]
        for block in self.turn["content"]:
            yield NS(type="content_block_start", content_block=NS(**block))
            if block["type"] == "text":
                yield NS(type="text", text=block["text"])

    async def get_final_message(self):
        return NS(content=copy.deepcopy(self.turn["content"]), stop_reason=self.turn["stop_reason"])


class FakeLLM:
    """Replays scripted turns and records every request's parameters."""

    def __init__(self, turns: list[dict] | None = None):
        self.turns = list(turns or [])
        self.calls: list[dict] = []

    def add(self, *turns: dict) -> None:
        self.turns.extend(turns)

    def stream(self, **params):
        self.calls.append(copy.deepcopy(params))
        if not self.turns:
            raise AssertionError("FakeLLM: no scripted turn left")
        return FakeStream(self.turns.pop(0))


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for chunk in body.strip().split("\n\n"):
        lines = chunk.split("\n")
        assert lines[0].startswith("event: "), chunk
        assert lines[1].startswith("data: "), chunk
        assert len(lines) == 2, chunk
        events.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
    return events


@pytest.fixture
def config(tmp_path: Path) -> Config:
    cfg = Config.from_env({"JARVIS_DATA_DIR": str(tmp_path), "JARVIS_WEB_DIR": str(tmp_path / "noweb")})
    return cfg


@pytest.fixture
def store(config: Config) -> Store:
    s = Store(config.db_path)
    yield s
    s.close()


class Calls:
    def __init__(self):
        self.items: list[dict] = []


def add_test_tools(registry: Registry, calls: Calls) -> None:
    async def handler(inp):
        calls.items.append(inp)
        return {"done": inp}

    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"],
              "additionalProperties": False}
    registry.register(Tool(name="t_read", area="test", description="read", input_schema=schema,
                           handler=handler, risk=READ, label="Lese …"))
    registry.register(Tool(name="t_write", area="test", description="write", input_schema=schema,
                           handler=handler, risk=WRITE, label="Schreibe …"))
    registry.register(Tool(name="t_confirm", area="test", description="confirm", input_schema=schema,
                           handler=handler, risk=CONFIRM, label="Heikel …", kind="test_kind",
                           summarize=lambda i: (f"Heikle Aktion {i['x']}", {"x": i["x"]})))


@pytest.fixture
def calls() -> Calls:
    return Calls()


@pytest.fixture
def make_client(config: Config, store: Store, calls: Calls):
    def factory(llm=None, cfg: Config | None = None, client_host: str = "127.0.0.1", with_test_tools=True):
        cfg = cfg or config
        registry = build_registry(cfg, store)
        if with_test_tools:
            add_test_tools(registry, calls)
        app = create_app(cfg, store=store, llm=llm or FakeLLM(), registry=registry)
        return TestClient(app, client=(client_host, 50000))
    return factory
