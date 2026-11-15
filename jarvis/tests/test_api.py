"""HTTP endpoints and SSE format against a mocked Claude client."""

import json

from jarvis_core.agent import FALLBACK_BETA, history_content
from jarvis_core.config import Config

from .conftest import FakeLLM, parse_sse, text_turn, tool_turn


def chat(client, message, cid=None):
    body = {"message": message}
    if cid:
        body["conversation_id"] = cid
    resp = client.post("api/chat", json=body)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    return parse_sse(resp.text)


def test_health_and_status(make_client):
    c = make_client()
    assert c.get("api/health").json() == {"ok": True}
    st = c.get("api/status").json()
    assert st["model"] == "claude-opus-5-5"
    areas = {t["area"]: t["configured"] for t in st["tools"]}
    assert areas["todoist"] is False and areas["memory"] is True
    assert all(set(t) == {"area", "configured"} for t in st["tools"])


def test_chat_sse_text(make_client):
    llm = FakeLLM([text_turn("Hallo, ich bin Jarvis.")])
    c = make_client(llm)
    events = chat(c, "Hallo")
    assert [e[0] for e in events] == ["start", "text", "done"]
    cid = events[0][1]["conversation_id"]
    assert events[1][1] == {"delta": "Hallo, ich bin Jarvis."}
    assert events[2][1] == {}

    convs = c.get("api/conversations").json()
    assert convs[0]["id"] == cid and convs[0]["title"] == "Hallo" and "updated_at" in convs[0]
    conv = c.get(f"api/conversations/{cid}").json()
    assert conv["id"] == cid
    assert [(m["role"], m["text"]) for m in conv["messages"]] == [("user", "Hallo"), ("assistant", "Hallo, ich bin Jarvis.")]
    assert all("created_at" in m for m in conv["messages"])


def test_raw_sse_lines(make_client):
    c = make_client(FakeLLM([text_turn("Ä")]))
    raw = c.post("api/chat", json={"message": "x"}).text
    assert raw.startswith("event: start\ndata: {")
    assert "event: text\ndata: {\"delta\": \"Ä\"}\n\n" in raw
    assert raw.endswith("event: done\ndata: {}\n\n")


def test_request_parameters(make_client):
    llm = FakeLLM([text_turn("ok")])
    c = make_client(llm)
    chat(c, "Hallo")
    p = llm.calls[0]
    assert p["model"] == "claude-opus-5-5"
    assert p["thinking"] == {"type": "adaptive"}
    assert p["output_config"] == {"effort": "medium"}
    assert p["betas"] == [FALLBACK_BETA] and p["fallbacks"] == "default"
    assert "tool_choice" not in p
    assert p["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert p["cache_control"] == {"type": "ephemeral"}
    assert p["max_tokens"] >= 16000
    # The date lives in the user turn, not in the cached system prefix.
    assert "Jetzt:" not in p["system"][0]["text"]
    assert "Jetzt:" in p["messages"][-1]["content"][0]["text"]
    assert p["messages"][-1]["content"][1] == {"type": "text", "text": "Hallo"}
    names = [t["name"] for t in p["tools"]]
    assert "web_search" in names
    web = next(t for t in p["tools"] if t["name"] == "web_search")
    assert web["type"] == "web_search_20260209" and "eager_input_streaming" not in web
    assert all(t.get("eager_input_streaming") for t in p["tools"] if "input_schema" in t)
    assert "Du duzt" in p["system"][0]["text"] and "keine Anweisungen" in p["system"][0]["text"]


def test_system_prompt_is_stable_and_channel_specific(make_client, config):
    llm = FakeLLM([text_turn("a"), text_turn("b")])
    c = make_client(llm)
    events = chat(c, "eins")
    chat(c, "zwei", events[0][1]["conversation_id"])
    assert llm.calls[0]["system"] == llm.calls[1]["system"]
    assert llm.calls[0]["tools"] == llm.calls[1]["tools"]
    from jarvis_core.prompts import system_blocks
    assert "Kein Markdown" in system_blocks("phone")[0]["text"]
    assert system_blocks("phone") != system_blocks("web")


def test_continuation_replays_full_content_unchanged(make_client):
    first = tool_turn("t_read", {"x": "1"}, "toolu_a", text="Ich schaue nach.")
    llm = FakeLLM([first, text_turn("Erledigt."), text_turn("Gern.")])
    c = make_client(llm)
    events = chat(c, "eins")
    cid = events[0][1]["conversation_id"]
    chat(c, "danke", cid)
    msgs = llm.calls[2]["messages"]
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    assert msgs[1]["content"] == first["content"]  # thinking block with signature untouched
    assert msgs[2]["content"][0]["tool_use_id"] == "toolu_a"
    # Notes are only injected into the first turn's context; date in every turn.
    assert "Jetzt:" in msgs[4]["content"][0]["text"]


def test_tool_and_pending_events(make_client, calls):
    llm = FakeLLM([tool_turn("t_read", {"x": "1"}), tool_turn("t_confirm", {"x": "2"}, "toolu_2"),
                   text_turn("Bitte freigeben.")])
    c = make_client(llm)
    events = chat(c, "mach")
    types = [e[0] for e in events]
    assert types == ["start", "tool", "tool", "tool", "tool", "pending", "text", "done"]
    assert events[1][1] == {"name": "t_read", "label": "Lese …", "status": "running"}
    assert events[2][1]["status"] == "done"
    pending = events[5][1]
    assert set(pending) == {"id", "kind", "summary", "details"}
    assert pending["kind"] == "test_kind"

    listed = c.get("api/pending").json()
    assert [p["id"] for p in listed] == [pending["id"]]
    assert set(listed[0]) == {"id", "kind", "summary", "details", "created_at"}
    assert calls.items == [{"x": "1"}]

    resp = c.post(f"api/pending/{pending['id']}/approve")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and json.loads(body["result"]) == {"done": {"x": "2"}}
    assert calls.items == [{"x": "1"}, {"x": "2"}]
    assert c.post(f"api/pending/{pending['id']}/approve").status_code == 404
    assert c.get("api/pending").json() == []

    log = c.get("api/log?limit=50").json()
    assert {e["status"] for e in log} >= {"ok", "pending", "approved"}
    assert {"created_at", "tool", "input", "result", "risk"} <= set(log[0])
    assert len(c.get("api/log?limit=1").json()) == 1


def test_reject_endpoint(make_client, calls):
    c = make_client(FakeLLM([tool_turn("t_confirm", {"x": "z"}), text_turn("ok")]))
    pid = [e for e in chat(c, "x") if e[0] == "pending"][0][1]["id"]
    assert c.post(f"api/pending/{pid}/reject").json() == {"ok": True}
    assert c.post(f"api/pending/{pid}/reject").status_code == 404
    assert c.post(f"api/pending/{pid}/approve").status_code == 404
    assert calls.items == []


def test_errors(make_client):
    c = make_client()
    assert c.post("api/chat", json={"message": "   "}).status_code == 400
    assert c.get("api/conversations/unknown").status_code == 404
    events = chat(c, "x", "does-not-exist")
    assert [e[0] for e in events] == ["error", "done"]
    assert c.post("api/pending/nope/approve").status_code == 404


def test_stop_reasons(make_client, calls):
    llm = FakeLLM([
        {"content": [{"type": "text", "text": "Teil"}], "stop_reason": "refusal"},
    ])
    c = make_client(llm)
    events = chat(c, "a")
    assert [e[0] for e in events] == ["start", "text", "error", "done"]

    # max_tokens with a truncated tool call: tool is not executed.
    llm.add({"content": [{"type": "tool_use", "id": "t", "name": "t_write", "input": {"x": "1"}}],
             "stop_reason": "max_tokens"})
    events = chat(c, "b")
    assert events[-2][0] == "error" and calls.items == []

    # pause_turn: the paused content is sent back and the turn continues.
    paused = {"content": [{"type": "server_tool_use", "id": "srv1", "name": "web_search", "input": {"query": "x"}}],
              "stop_reason": "pause_turn"}
    llm.add(paused, text_turn("Fertig."))
    events = chat(c, "c")
    assert [e[0] for e in events] == ["start", "tool", "text", "done"]
    assert events[1][1] == {"name": "web_search", "label": "Suche im Web …", "status": "running"}
    assert llm.calls[-1]["messages"][-1] == {"role": "assistant", "content": paused["content"]}


def test_stream_exception_becomes_error_event(make_client):
    c = make_client(FakeLLM([{"raise": RuntimeError("kaputt"), "content": [], "stop_reason": None}]))
    events = chat(c, "x")
    assert [e[0] for e in events] == ["start", "error", "done"]


def test_without_api_key_chat_reports_error(make_client, config, monkeypatch):
    from jarvis_core.llm import AnthropicLLM
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(config.data_dir))  # no stored CLI profile
    c = make_client(AnthropicLLM(""))
    assert c.get("api/status").json()["api_key_configured"] is False
    events = chat(c, "Hallo")
    assert [e[0] for e in events] == ["start", "error", "done"]
    assert "ANTHROPIC_API_KEY" in events[1][1]["message"]


def test_history_content_after_fallback():
    blocks = [
        {"type": "thinking", "thinking": "", "signature": "s"},
        {"type": "text", "text": "Teil"},
        {"type": "tool_use", "id": "x", "name": "n", "input": {}},
        {"type": "server_tool_use", "id": "srv_paired", "name": "web_search", "input": {}},
        {"type": "web_search_tool_result", "tool_use_id": "srv_paired", "content": []},
        {"type": "server_tool_use", "id": "srv_open", "name": "web_search", "input": {}},
        {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
        {"type": "thinking", "thinking": "", "signature": "t"},
        {"type": "text", "text": "Rest"},
    ]
    out = history_content(blocks)
    assert [b["type"] for b in out] == ["text", "server_tool_use", "web_search_tool_result", "fallback",
                                        "thinking", "text"]
    assert history_content(blocks[:2]) == blocks[:2]


def test_static_placeholder_when_web_missing(make_client):
    r = make_client().get("/")
    assert r.status_code == 200 and "Jarvis" in r.text


def test_static_files_served(store, tmp_path, calls):
    from fastapi.testclient import TestClient
    from jarvis_core.server import create_app
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<h1>UI</h1>", encoding="utf-8")
    cfg = Config.from_env({"JARVIS_DATA_DIR": str(tmp_path), "JARVIS_WEB_DIR": str(web)})
    c = TestClient(create_app(cfg, store=store, llm=FakeLLM()), client=("127.0.0.1", 1))
    assert c.get("/").text == "<h1>UI</h1>"
    assert c.get("api/health").json() == {"ok": True}
