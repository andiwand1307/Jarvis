"""Home Assistant domain rating and HTTP calls, Todoist API v1 calls (mocked transport)."""

import json

import httpx
import pytest

from jarvis_core.agent import Agent
from jarvis_core.config import Config, HAConfig
from jarvis_core.tools import Registry
from jarvis_core.tools import homeassistant as ha
from jarvis_core.tools import todoist
from jarvis_core.tools.base import CONFIRM, WRITE, ToolError, validate_input

from .conftest import FakeLLM, text_turn, tool_turn


TRUSTED = ("scene.abend", "script.gute_nacht")


@pytest.mark.parametrize("inp,expected", [
    # positive list, entity targets of the same domain
    ({"domain": "light", "service": "turn_on", "entity_id": "light.kueche"}, WRITE),
    ({"domain": "light", "service": "turn_on", "entity_id": ["light.a", "light.b"]}, WRITE),
    ({"domain": "switch", "service": "toggle", "entity_id": "switch.kaffee"}, WRITE),
    ({"domain": "media_player", "service": "volume_set", "entity_id": "media_player.wz", "data": {"volume_level": 0.3}}, WRITE),
    ({"domain": "climate", "service": "set_temperature", "entity_id": "climate.bad", "data": {"temperature": 21}}, WRITE),
    ({"domain": "fan", "service": "turn_off", "entity_id": "fan.decke"}, WRITE),
    ({"domain": "light", "service": "turn_on", "data": {"entity_id": "light.flur", "brightness_pct": 50}}, WRITE),
    # sensitive domains
    ({"domain": "lock", "service": "unlock", "entity_id": "lock.haustuer"}, CONFIRM),
    ({"domain": "cover", "service": "open_cover", "entity_id": "cover.garage"}, CONFIRM),
    ({"domain": "alarm_control_panel", "service": "alarm_disarm", "entity_id": "alarm_control_panel.haus"}, CONFIRM),
    ({"domain": "siren", "service": "turn_on", "entity_id": "siren.a"}, CONFIRM),
    ({"domain": "valve", "service": "open_valve", "entity_id": "valve.a"}, CONFIRM),
    # services outside the positive list, also on harmless domains
    ({"domain": "light", "service": "reload"}, CONFIRM),
    ({"domain": "homeassistant", "service": "turn_on", "entity_id": "light.a"}, CONFIRM),
    ({"domain": "homeassistant", "service": "restart"}, CONFIRM),
    ({"domain": "automation", "service": "trigger", "entity_id": "automation.x"}, CONFIRM),
    ({"domain": "input_boolean", "service": "turn_on", "entity_id": "input_boolean.alarm_scharf"}, CONFIRM),
    ({"domain": "button", "service": "press", "entity_id": "button.tor"}, CONFIRM),
    ({"domain": "notify", "service": "notify", "data": {"message": "x"}}, CONFIRM),
    ({"domain": "shell_command", "service": "x"}, CONFIRM),
    ({"domain": "rest_command", "service": "x"}, CONFIRM),
    ({"domain": "python_script", "service": "x"}, CONFIRM),
    ({"domain": "group", "service": "set", "data": {"object_id": "x", "entities": ["lock.a"]}}, CONFIRM),
    # entity domains must match the service domain
    ({"domain": "switch", "service": "turn_on", "entity_id": "lock.haustuer"}, CONFIRM),
    ({"domain": "light", "service": "turn_on", "entity_id": "group.alle"}, CONFIRM),
    ({"domain": "light", "service": "turn_on", "data": {"entity_id": "alarm_control_panel.x"}}, CONFIRM),
    ({"domain": "light", "service": "turn_off", "entity_id": "all"}, CONFIRM),
    ({"domain": "light", "service": "turn_on"}, CONFIRM),
    # unresolved targets
    ({"domain": "light", "service": "turn_on", "data": {"area_id": "kueche"}}, CONFIRM),
    ({"domain": "light", "service": "turn_on", "entity_id": "light.a", "data": {"device_id": "abc"}}, CONFIRM),
    ({"domain": "light", "service": "turn_on", "data": {"target": {"label_id": "x"}}}, CONFIRM),
    ({"domain": "light", "service": "turn_on", "data": {"target": {"floor_id": "og"}}}, CONFIRM),
    ({"domain": "light", "service": "turn_on", "data": {"entity_id": {"a": "lock.x"}}}, CONFIRM),
    ({"domain": "light", "service": "turn_on", "data": "nope"}, CONFIRM),
    # traversal and malformed names
    ({"domain": "light", "service": "../lock/unlock", "data": {"area_id": "flur"}}, CONFIRM),
    ({"domain": "light", "service": "../../../../addons/core_ssh/stop"}, CONFIRM),
    ({"domain": "Light ", "service": "turn_on", "entity_id": "light.a"}, CONFIRM),
    ({"domain": "light", "service": "turn_on", "entity_id": "light.a/../../x"}, CONFIRM),
    # scenes and scripts: only trusted entities, only turn_on / script.<name>
    ({"domain": "scene", "service": "turn_on", "entity_id": "scene.abend"}, WRITE),
    ({"domain": "scene", "service": "turn_on", "entity_id": ["scene.abend", "scene.party"]}, CONFIRM),
    ({"domain": "scene", "service": "apply", "data": {"entities": {"lock.haustuer": "unlocked"}}}, CONFIRM),
    ({"domain": "scene", "service": "create", "data": {"scene_id": "x", "entities": {"lock.a": "unlocked"}}}, CONFIRM),
    ({"domain": "script", "service": "turn_on", "entity_id": "script.gute_nacht"}, WRITE),
    ({"domain": "script", "service": "gute_nacht"}, WRITE),
    ({"domain": "script", "service": "gute_nacht", "entity_id": "lock.x"}, CONFIRM),
    ({"domain": "script", "service": "turn_on", "entity_id": "script.haustuer_oeffnen"}, CONFIRM),
    ({"domain": "script", "service": "haustuer_oeffnen"}, CONFIRM),
    ({"domain": "script", "service": "reload"}, CONFIRM),
])
def test_service_risk(inp, expected):
    assert ha.service_risk(inp, TRUSTED) == expected


def test_scenes_without_trust_list_are_confirm():
    assert ha.service_risk({"domain": "scene", "service": "turn_on", "entity_id": "scene.abend"}) == CONFIRM
    assert ha.service_risk({"domain": "script", "service": "gute_nacht"}) == CONFIRM


def test_trusted_entities_from_env():
    cfg = Config.from_env({"HA_URL": "http://ha", "HA_TOKEN": "t", "HA_TRUSTED_ENTITIES": "scene.abend, Script.Gute_Nacht"})
    assert cfg.ha.trusted_entities == ("scene.abend", "script.gute_nacht")


class Recorder:
    def __init__(self, responder):
        self.requests: list[httpx.Request] = []
        self.responder = responder

    def transport(self):
        def handle(request):
            self.requests.append(request)
            return self.responder(request)
        return httpx.MockTransport(handle)


def ha_registry(rec: Recorder, trusted=()) -> Registry:
    reg = Registry()
    ha.register(reg, HAConfig(base_url="http://supervisor/core/api", token="tok", trusted_entities=tuple(trusted)),
                transport=rec.transport())
    return reg


@pytest.mark.parametrize("bad", [
    {"domain": "light", "service": "../lock/unlock"},
    {"domain": "light", "service": "../../../../addons/core_ssh/stop"},
    {"domain": "light/../lock", "service": "unlock"},
    {"domain": "light", "service": "turn_on?x=1"},
    {"domain": "light", "service": "turn_on\n"},
    {"domain": "light", "service": "turn_on", "entity_id": {"a": 1}},
])
async def test_ha_handler_rejects_traversal_even_after_approval(bad):
    rec = Recorder(lambda r: httpx.Response(200, json=[]))
    reg = ha_registry(rec)
    tool = reg.get("ha_call_service")
    assert tool.risk_for(bad) == CONFIRM
    with pytest.raises(ToolError):
        await tool.handler(bad)
    assert rec.requests == []


def test_ha_schema_rejects_bad_names():
    tool = ha_registry(Recorder(lambda r: httpx.Response(200))).get("ha_call_service")
    assert validate_input(tool.input_schema, {"domain": "light", "service": "../lock/unlock"})
    assert validate_input(tool.input_schema, {"domain": "light", "service": "turn_on\n"})
    assert not validate_input(tool.input_schema, {"domain": "light", "service": "turn_on"})


@pytest.mark.parametrize("eid", ["../services", "../../../addons", "../config", "light.a?x=1", "light.a/b", "light"])
async def test_ha_get_states_rejects_bad_ids(eid):
    rec = Recorder(lambda r: httpx.Response(200, json={}))
    tool = ha_registry(rec).get("ha_get_states")
    assert validate_input(tool.input_schema, {"entity_id": eid})
    with pytest.raises(ToolError):
        await tool.handler({"entity_id": eid})
    assert rec.requests == []


async def test_ha_calendar_rejects_bad_ids():
    rec = Recorder(lambda r: httpx.Response(200, json=[]))
    tool = ha_registry(rec).get("ha_calendar_events")
    with pytest.raises(ToolError):
        await tool.handler({"entity_id": "calendar.x/../../states"})
    with pytest.raises(ToolError):
        await tool.handler({"entity_id": "light.x"})
    assert rec.requests == []


async def test_ha_trusted_script_runs_via_agent_without_approval(config, store):
    rec = Recorder(lambda r: httpx.Response(200, json=[]))
    llm = FakeLLM([tool_turn("ha_call_service", {"domain": "script", "service": "gute_nacht"}), text_turn("ok")])
    agent = Agent(config, store, ha_registry(rec, trusted=["script.gute_nacht"]), llm)
    events = [e async for e in agent.run(None, "gute Nacht", "web")]
    assert not [e for e in events if e["type"] == "pending"]
    assert rec.requests[0].url.path == "/core/api/services/script/gute_nacht"


async def test_ha_call_service_write_runs_with_auth():
    rec = Recorder(lambda r: httpx.Response(200, json=[{"entity_id": "light.kueche", "state": "on",
                                                         "attributes": {"friendly_name": "Küche"}}]))
    tool = ha_registry(rec).get("ha_call_service")
    out = await tool.handler({"domain": "light", "service": "turn_on", "entity_id": "light.kueche",
                              "data": {"brightness_pct": 40}})
    req = rec.requests[0]
    assert req.method == "POST" and str(req.url) == "http://supervisor/core/api/services/light/turn_on"
    assert req.headers["authorization"] == "Bearer tok"
    assert json.loads(req.content) == {"brightness_pct": 40, "entity_id": ["light.kueche"]}
    assert out["changed"][0]["name"] == "Küche"


async def test_ha_lock_via_agent_creates_pending_without_http(config, store):
    rec = Recorder(lambda r: httpx.Response(200, json=[]))
    llm = FakeLLM([tool_turn("ha_call_service", {"domain": "lock", "service": "unlock",
                                                 "entity_id": "lock.haustuer"}), text_turn("Bitte freigeben.")])
    agent = Agent(config, store, ha_registry(rec), llm)
    events = [e async for e in agent.run(None, "Tür auf", "web")]
    pending = [e["data"] for e in events if e["type"] == "pending"][0]
    assert pending["kind"] == "ha_service"
    assert pending["summary"] == "Home Assistant: lock.unlock für lock.haustuer"
    assert rec.requests == []
    ok, _ = await agent.approve(pending["id"])
    assert ok and len(rec.requests) == 1
    assert rec.requests[0].url.path == "/core/api/services/lock/unlock"


async def test_ha_states_filter_and_calendar():
    states = [
        {"entity_id": "light.kueche", "state": "on", "attributes": {"friendly_name": "Küche"}},
        {"entity_id": "sensor.temp", "state": "21", "attributes": {"unit_of_measurement": "°C"}},
    ]

    def respond(r: httpx.Request):
        if r.url.path.endswith("/states"):
            return httpx.Response(200, json=states)
        if r.url.path.endswith("/calendars"):
            return httpx.Response(200, json=[{"entity_id": "calendar.familie", "name": "Familie"}])
        if "/calendars/calendar.familie" in r.url.path:
            assert "start" in r.url.params and "end" in r.url.params
            return httpx.Response(200, json=[{"summary": "Zahnarzt", "start": {"dateTime": "2026-11-16T09:00:00+01:00"},
                                              "end": {"dateTime": "2026-11-16T10:00:00+01:00"}}])
        return httpx.Response(404)

    rec = Recorder(respond)
    reg = ha_registry(rec)
    out = await reg.get("ha_get_states").handler({"domain": "light"})
    assert [s["entity_id"] for s in out["states"]] == ["light.kueche"]
    out = await reg.get("ha_get_states").handler({"search": "küche"})
    assert out["count"] == 1
    cal = await reg.get("ha_calendar_events").handler({"start": "2026-11-16T00:00:00+01:00", "days": 1})
    assert cal["events"][0]["summary"] == "Zahnarzt"
    assert cal["range"]["end"].startswith("2026-11-17T00:00:00")


def test_ha_supervisor_config_preferred():
    cfg = Config.from_env({"SUPERVISOR_TOKEN": "s", "HA_URL": "http://other", "HA_TOKEN": "x"})
    assert cfg.ha.base_url == "http://supervisor/core/api" and cfg.ha.token == "s" and cfg.addon


async def test_todoist_v1_endpoints():
    def respond(r: httpx.Request):
        if r.url.path == "/api/v1/tasks/filter":
            assert r.url.params["query"] == "heute"
            return httpx.Response(200, json={"results": [{"id": "1", "content": "Milch", "project_id": "p",
                                                          "priority": 1, "due": {"date": "2026-11-15",
                                                                                 "string": "heute"}}],
                                             "next_cursor": None})
        if r.url.path == "/api/v1/projects":
            if "cursor" not in r.url.params:
                return httpx.Response(200, json={"results": [{"id": "p", "name": "Einkaufsliste"}], "next_cursor": "c2"})
            return httpx.Response(200, json={"results": [{"id": "q", "name": "Arbeit"}], "next_cursor": None})
        if r.url.path == "/api/v1/tasks" and r.method == "POST":
            body = json.loads(r.content)
            assert body == {"content": "Brot", "project_id": "p", "due_string": "morgen", "due_lang": "de"}
            return httpx.Response(200, json={"id": "2", "content": "Brot", "project_id": "p", "priority": 1})
        if r.url.path == "/api/v1/tasks/2/close":
            return httpx.Response(204)
        return httpx.Response(404)

    rec = Recorder(respond)
    reg = Registry()
    todoist.register(reg, "tok", transport=rec.transport())
    assert (await reg.get("todoist_find_tasks").handler({"query": "heute"}))["tasks"][0]["due"] == "heute"
    projects = (await reg.get("todoist_list_projects").handler({}))["projects"]
    assert [p["name"] for p in projects] == ["Einkaufsliste", "Arbeit"]
    created = await reg.get("todoist_add_task").handler({"content": "Brot", "project_id": "p", "due_string": "morgen"})
    assert created["created"]["id"] == "2"
    assert (await reg.get("todoist_complete_task").handler({"task_id": "2"}))["completed"] == "2"
    assert all(r.headers["authorization"] == "Bearer tok" for r in rec.requests)
    assert reg.get("todoist_add_task").risk == WRITE


async def test_todoist_auth_error_is_tool_error(config, store):
    rec = Recorder(lambda r: httpx.Response(401))
    reg = Registry()
    todoist.register(reg, "bad", transport=rec.transport())
    llm = FakeLLM([tool_turn("todoist_list_projects", {}), text_turn("Zugang fehlt.")])
    agent = Agent(config, store, reg, llm)
    events = [e async for e in agent.run(None, "Projekte?", "web")]
    assert {"name": "todoist_list_projects", "label": "Hole die Todoist-Projekte …", "status": "error"} in [
        e["data"] for e in events if e["type"] == "tool"]
    result = llm.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "Token" in result["content"]


@pytest.mark.parametrize("bad", ["1/../../projects/99/archive#", "1?x=1", "a b", "../x", ""])
async def test_todoist_ids_validated(bad):
    rec = Recorder(lambda r: httpx.Response(200, json={"results": [], "next_cursor": None}))
    reg = Registry()
    todoist.register(reg, "tok", transport=rec.transport())
    assert validate_input(reg.get("todoist_complete_task").input_schema, {"task_id": bad})
    with pytest.raises(ToolError):
        await reg.get("todoist_complete_task").handler({"task_id": bad})
    if bad:
        with pytest.raises(ToolError):
            await reg.get("todoist_find_tasks").handler({"project_id": bad})
        with pytest.raises(ToolError):
            await reg.get("todoist_add_task").handler({"content": "x", "project_id": bad})
    assert rec.requests == []
