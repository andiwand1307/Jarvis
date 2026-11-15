"""Risk levels and the approval mechanism: a `confirm` tool never runs without approve."""

import json

import pytest

from jarvis_core.agent import PENDING_TEXT, Agent
from jarvis_core.config import Config, MailConfig
from jarvis_core.tools import Registry, Tool, build_registry, validate_input
from jarvis_core.tools import mail as mail_tools
from jarvis_core.tools.base import CONFIRM, READ, WRITE

from .conftest import FakeLLM, add_test_tools, text_turn, tool_turn


async def collect(agent, cid, text):
    return [e async for e in agent.run(cid, text, "web")]


def make_agent(config, store, calls, turns):
    registry = build_registry(config, store)
    add_test_tools(registry, calls)
    llm = FakeLLM(turns)
    return Agent(config, store, registry, llm), llm, registry


def test_risk_validation():
    reg = Registry()
    with pytest.raises(ValueError):
        reg.register(Tool(name="x", area="a", description="", input_schema={}, handler=None, risk="danger"))
    dyn = Tool(name="y", area="a", description="", input_schema={}, handler=None, risk=lambda i: "weird")
    assert dyn.risk_for({}) == CONFIRM  # unknown levels fall back to the strictest


def test_validate_input():
    schema = {"type": "object", "properties": {"n": {"type": "integer", "enum": [1, 2]},
                                               "s": {"type": "string"}},
              "required": ["s"], "additionalProperties": False}
    assert validate_input(schema, {"s": "a", "n": 1}) == []
    assert validate_input(schema, {"n": 3})  # missing s, bad enum
    assert validate_input(schema, {"s": "a", "zz": 1})
    assert validate_input(schema, "nope")
    assert validate_input(schema, {"s": "a", "n": True})  # bool is not an integer


async def test_read_and_write_run_immediately_and_are_logged(config, store, calls):
    agent, llm, _ = make_agent(config, store, calls, [
        tool_turn("t_read", {"x": "r"}, "toolu_r"),
        tool_turn("t_write", {"x": "w"}, "toolu_w"),
        text_turn("Fertig."),
    ])
    events = await collect(agent, None, "los")
    assert [c["x"] for c in calls.items] == ["r", "w"]
    log = store.get_log(10)
    assert {(e["tool"], e["risk"], e["status"]) for e in log} == {("t_read", "read", "ok"), ("t_write", "write", "ok")}
    tool_events = [e["data"] for e in events if e["type"] == "tool"]
    assert {"name": "t_read", "label": "Lese …", "status": "running"} in tool_events
    assert {"name": "t_write", "label": "Schreibe …", "status": "done"} in tool_events


async def test_confirm_never_runs_without_approve(config, store, calls):
    agent, llm, _ = make_agent(config, store, calls, [
        tool_turn("t_confirm", {"x": "boom"}),
        text_turn("Ich habe es vorbereitet; bitte in der Oberfläche freigeben."),
    ])
    events = await collect(agent, None, "mach das Heikle")
    assert calls.items == []  # handler not executed

    pending = [e["data"] for e in events if e["type"] == "pending"]
    assert len(pending) == 1
    p = pending[0]
    assert p["kind"] == "test_kind" and p["summary"] == "Heikle Aktion boom" and p["details"] == {"x": "boom"}

    # The model was told the action awaits approval.
    tool_result = llm.calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and not tool_result.get("is_error")
    assert PENDING_TEXT in tool_result["content"]
    assert json.loads(tool_result["content"])["action_id"] == p["id"]

    assert store.list_pending()[0]["id"] == p["id"]
    assert store.get_log(5)[0]["status"] == "pending"

    # Approval runs it exactly once.
    ok, result = await agent.approve(p["id"])
    assert ok and calls.items == [{"x": "boom"}]
    assert await agent.approve(p["id"]) is None
    assert not agent.reject(p["id"])
    assert calls.items == [{"x": "boom"}]
    assert store.list_pending() == []
    assert store.get_log(1)[0]["status"] == "approved"


async def test_model_repeating_the_call_creates_another_pending_not_an_execution(config, store, calls):
    agent, _, _ = make_agent(config, store, calls, [
        tool_turn("t_confirm", {"x": "a"}, "toolu_1"),
        tool_turn("t_confirm", {"x": "a"}, "toolu_2", text="Freigegeben, ich führe es jetzt aus."),
        text_turn("ok"),
    ])
    await collect(agent, None, "jetzt sofort")
    assert calls.items == []
    assert len(store.list_pending()) == 2
    # No tool exists through which the model could approve anything.
    assert not any("approve" in name or "freigab" in name for name in agent.registry.tools)


async def test_reject_then_report_in_next_turn(config, store, calls):
    agent, llm, _ = make_agent(config, store, calls, [
        tool_turn("t_confirm", {"x": "a"}),
        text_turn("Wartet auf Freigabe."),
        text_turn("Verstanden."),
    ])
    events = await collect(agent, None, "eins")
    cid = events[0]["data"]["conversation_id"]
    pid = store.list_pending()[0]["id"]
    assert agent.reject(pid)
    assert await agent.approve(pid) is None
    assert calls.items == []
    await collect(agent, cid, "und?")
    context = llm.calls[-1]["messages"][-1]["content"][0]["text"]
    assert "verworfen" in context and "Heikle Aktion a" in context


async def test_invalid_input_is_not_executed(config, store, calls):
    agent, llm, _ = make_agent(config, store, calls, [
        tool_turn("t_write", {"y": 1}),
        text_turn("ok"),
    ])
    await collect(agent, None, "x")
    assert calls.items == []
    result = llm.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "INVALID_INPUT" in result["content"]


async def test_unknown_tool_gives_error_result(config, store, calls):
    agent, llm, _ = make_agent(config, store, calls, [tool_turn("nope", {}), text_turn("ok")])
    await collect(agent, None, "x")
    assert llm.calls[1]["messages"][-1]["content"][0]["is_error"] is True


class FakeMail:
    def __init__(self):
        self.sent = []

    def build(self, *args):
        return args

    def send(self, msg):
        self.sent.append(msg)


async def test_mail_send_is_confirm_and_only_sent_after_approve(config, store, calls):
    cfg = MailConfig(imap_host="imap.x", smtp_host="smtp.x", user="u@x", password="p", sender="u@x")
    registry = Registry()
    fake = FakeMail()
    mail_tools.register(registry, cfg, client=fake)
    assert registry.get("mail_send").risk_for({}) == CONFIRM
    assert registry.get("mail_search").risk == READ
    assert registry.get("mail_create_draft").risk == WRITE

    llm = FakeLLM([tool_turn("mail_send", {"to": "a@b.de", "subject": "Hallo", "body": "Text"}), text_turn("ok")])
    agent = Agent(config, store, registry, llm)
    events = await collect(agent, None, "schick die Mail")
    pending = [e["data"] for e in events if e["type"] == "pending"][0]
    assert pending["kind"] == "mail_send"
    assert pending["details"] == {"to": "a@b.de", "subject": "Hallo", "body": "Text"}
    assert fake.sent == []
    ok, _ = await agent.approve(pending["id"])
    assert ok and len(fake.sent) == 1


def test_mail_send_not_registered_without_smtp():
    registry = Registry()
    mail_tools.register(registry, MailConfig(imap_host="i", user="u", password="p"))
    assert "mail_send" not in registry.tools and "mail_search" in registry.tools


def test_unconfigured_areas_are_not_registered(config, store):
    registry = build_registry(config, store)
    status = {s["area"]: s["configured"] for s in registry.status()}
    assert status == {"todoist": False, "mail": False, "homeassistant": False, "memory": True, "web": True}
    assert all(t.area == "memory" for t in registry.tools.values())


def test_configured_areas_register(store, tmp_path):
    cfg = Config.from_env({
        "JARVIS_DATA_DIR": str(tmp_path), "TODOIST_API_TOKEN": "t", "HA_URL": "http://ha:8123", "HA_TOKEN": "h",
        "MAIL_IMAP_HOST": "i", "MAIL_USER": "u", "MAIL_PASSWORD": "p", "JARVIS_WEB_SEARCH": "false",
    })
    registry = build_registry(cfg, store)
    status = {s["area"]: s["configured"] for s in registry.status()}
    assert status == {"todoist": True, "mail": True, "homeassistant": True, "memory": True, "web": False}
    assert cfg.ha.base_url == "http://ha:8123/api"
