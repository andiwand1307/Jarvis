from jarvis_core.store import Store


def test_conversation_and_messages(store: Store):
    cid = store.create_conversation("Wie wird das Wetter morgen in Hamburg und was ziehe ich am besten an?")
    assert len(store.get_conversation(cid)["title"]) <= 60
    store.add_message(cid, "user", "Hallo")
    store.add_message(cid, "assistant", "Hi.")
    msgs = store.get_messages(cid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["text"] == "Hallo" and msgs[0]["created_at"]
    listed = store.list_conversations()
    assert listed[0]["id"] == cid and set(listed[0]) == {"id", "title", "updated_at"}


def test_api_messages_roundtrip_unchanged(store: Store):
    cid = store.create_conversation("x")
    thinking = {"type": "thinking", "thinking": "", "signature": "abc=="}
    m1 = {"role": "user", "content": [{"type": "text", "text": "Ümläut ✓"}]}
    m2 = {"role": "assistant", "content": [thinking, {"type": "tool_use", "id": "t1", "name": "n", "input": {"a": 1}}]}
    store.append_api_messages(cid, [m1])
    store.append_api_messages(cid, [m2, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}])
    got = store.get_api_messages(cid)
    assert got[0] == m1 and got[1] == m2 and len(got) == 3


def test_pending_claim_only_once(store: Store):
    p = store.create_pending(conversation_id=None, tool="t", kind="k", summary="s", details={"a": 1},
                             tool_input={"x": "1"})
    assert store.list_pending()[0]["id"] == p["id"]
    assert set(store.list_pending()[0]) == {"id", "kind", "summary", "details", "created_at"}
    assert store.claim_pending(p["id"], "executing") is True
    assert store.claim_pending(p["id"], "executing") is False
    assert store.claim_pending(p["id"], "rejected") is False
    assert store.list_pending() == []


def test_unreported_resolutions(store: Store):
    cid = store.create_conversation("x")
    p = store.create_pending(conversation_id=cid, tool="t", kind="k", summary="s", details={}, tool_input={})
    assert store.take_unreported(cid) == []
    store.claim_pending(p["id"], "executing")
    assert store.take_unreported(cid) == []  # still running
    store.finish_pending(p["id"], "done", "ok")
    got = store.take_unreported(cid)
    assert len(got) == 1 and got[0]["status"] == "done"
    assert store.take_unreported(cid) == []


def test_log_and_notes(store: Store):
    store.log(tool="t", risk="read", tool_input={"q": "x"}, result="a" * 1000, status="ok")
    entry = store.get_log(10)[0]
    assert entry["input"] == {"q": "x"} and len(entry["result"]) <= 300
    nid = store.add_note("Lieblingsfarbe ist Blau")
    store.add_note("Arbeitet in Köln")
    assert [n["id"] for n in store.search_notes("blau")] == [nid]
    assert len(store.search_notes()) == 2
    assert store.delete_note(nid) and not store.delete_note(nid)


def test_persistence_across_instances(tmp_path):
    path = tmp_path / "db.sqlite3"
    s1 = Store(path)
    cid = s1.create_conversation("x")
    s1.append_api_messages(cid, [{"role": "user", "content": "hi"}])
    s1.close()
    s2 = Store(path)
    assert s2.get_api_messages(cid) == [{"role": "user", "content": "hi"}]
    s2.close()
