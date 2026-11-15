"""Home Assistant domain rating and HTTP calls, Todoist API v1 calls (mocked transport)."""

import json

import httpx
import pytest

from jarvis_core.agent import Agent
from jarvis_core.config import Config, HAConfig
from jarvis_core.tools import Registry
from jarvis_core.tools import homeassistant as ha
from jarvis_core.tools import todoist
from jarvis_core.tools.base import CONFIRM, WRITE

from .conftest import FakeLLM, text_turn, tool_turn


@pytest.mark.parametrize("domain", ["light", "switch", "media_player", "scene", "script", "climate", "Light"])
def test_write_domains(domain):
    assert ha.domain_risk(domain) == WRITE


@pytest.mark.parametrize("domain", ["lock", "alarm_control_panel", "cover", "siren", "valve",
                                    "homeassistant", "automation", "notify", "fan", "", "unknown_xyz"])
def test_confirm_domains(domain):
    assert ha.domain_risk(domain) == CONFIRM


@pytest.mark.parametrize("inp,expected", [
    ({"domain": "light", "service": "turn_on", "entity_id": "light.kueche"}, WRITE),
    ({"domain": "light", "service": "turn_on", "entity_id": ["light.a", "light.b"]}, WRITE),
    ({"domain": "lock", "service": "unlock", "entity_id": "lock.haustuer"}, CONFIRM),
    ({"domain": "cover", "service": "open_cover", "entity_id": "cover.garage"}, CONFIRM),
    ({"domain": "switch", "service": "turn_on", "entity_id": "lock.haustuer"}, CONFIRM),
    ({"domain": "light", "service": "turn_on", "data": {"entity_id": "alarm_control_panel.x"}}, CONFIRM),
    ({"domain": "light", "service": "turn_off", "entity_id": "all"}, CONFIRM),
    ({"domain": "homeassistant", "service": "turn_on", "entity_id": "light.a"}, CONFIRM),
    ({"domain": "light", "service": "turn_on", "data": {"area_id": "kueche"}}, WRITE),
])
def test_service_risk(inp, expected):
    assert ha.service_risk(inp) == expected


class Recorder:
    def __init__(self, responder):
        self.requests: list[httpx.Request] = []
        self.responder = responder

    def transport(self):
        def handle(request):
            self.requests.append(request)
            return self.responder(request)
        return httpx.MockTransport(handle)


def ha_registry(rec: Recorder) -> Registry:
    reg = Registry()
    ha.register(reg, HAConfig(base_url="http://supervisor/core/api", token="tok"), transport=rec.transport())
    return reg


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
    assert await reg.get("todoist_complete_task").handler({"task_id": "2"}) == {"completed": "2"}
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
