"""Access control: ingress gateway only in the add-on, bearer token or loopback outside."""

import pytest

from jarvis_core.addon_env import export_lines
from jarvis_core.config import Config
from jarvis_core.security import is_allowed

ADDON = Config.from_env({"SUPERVISOR_TOKEN": "x"})
TOKEN = Config.from_env({"JARVIS_TOKEN": "geheim", "JARVIS_HOST": "0.0.0.0"})
LOCAL = Config.from_env({"JARVIS_HOST": "0.0.0.0"})


@pytest.mark.parametrize("cfg,host,path,headers,query,allowed", [
    (ADDON, "172.30.32.2", "/api/status", {}, {}, True),
    (ADDON, "172.30.32.2", "/", {}, {}, True),
    (ADDON, "172.30.32.1", "/api/status", {}, {}, False),
    (ADDON, "192.168.1.20", "/", {}, {}, False),
    (ADDON, "127.0.0.1", "/api/status", {}, {}, False),
    (ADDON, "127.0.0.1", "/api/health", {}, {}, True),
    (ADDON, "192.168.1.20", "/api/status", {"authorization": "Bearer x"}, {}, False),
    (TOKEN, "192.168.1.20", "/api/status", {"authorization": "Bearer geheim"}, {}, True),
    (TOKEN, "192.168.1.20", "/api/status", {"authorization": "bearer geheim"}, {}, True),
    (TOKEN, "192.168.1.20", "/api/status", {"authorization": "Bearer falsch"}, {}, False),
    (TOKEN, "192.168.1.20", "/api/status", {}, {}, False),
    (TOKEN, "127.0.0.1", "/api/chat", {}, {}, False),
    (TOKEN, "192.168.1.20", "/api/pending", {}, {"token": "geheim"}, True),
    (TOKEN, "192.168.1.20", "/api/health", {}, {}, True),
    (TOKEN, "192.168.1.20", "/index.html", {}, {}, True),
    (LOCAL, "127.0.0.1", "/api/status", {}, {}, True),
    (LOCAL, "::1", "/api/status", {}, {}, True),
    (LOCAL, "192.168.1.20", "/api/status", {}, {}, False),
    (LOCAL, "192.168.1.20", "/", {}, {}, False),
    (LOCAL, None, "/api/status", {}, {}, False),
])
def test_is_allowed(cfg, host, path, headers, query, allowed):
    assert is_allowed(cfg, host, path, headers, query)[0] is allowed


def test_bind_host():
    assert ADDON.access_mode == "ingress" and ADDON.bind_host == "0.0.0.0"
    assert TOKEN.access_mode == "token" and TOKEN.bind_host == "0.0.0.0"
    assert LOCAL.access_mode == "local" and LOCAL.bind_host == "127.0.0.1"


def test_middleware_ingress(make_client, tmp_path):
    cfg = Config.from_env({"JARVIS_ADDON": "1", "JARVIS_DATA_DIR": str(tmp_path)})
    ok = make_client(cfg=cfg, client_host="172.30.32.2")
    assert ok.get("api/status").status_code == 200
    bad = make_client(cfg=cfg, client_host="10.0.0.5")
    r = bad.get("api/status")
    assert r.status_code == 403
    assert bad.post("api/pending/x/approve").status_code == 403
    assert bad.post("api/chat", json={"message": "hi"}).status_code == 403


def test_middleware_token(make_client, tmp_path):
    cfg = Config.from_env({"JARVIS_TOKEN": "geheim", "JARVIS_DATA_DIR": str(tmp_path)})
    c = make_client(cfg=cfg, client_host="192.168.1.9")
    r = c.get("api/conversations")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert c.get("api/conversations", headers={"Authorization": "Bearer geheim"}).status_code == 200
    assert c.get("api/health").status_code == 200
    assert c.get("/?token=falsch").headers.get("set-cookie") is None
    r = c.get("/?token=geheim")
    assert "jarvis_token=geheim" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
    assert c.get("api/conversations").status_code == 200  # cookie now sent by the client
    c.cookies.clear()
    assert c.get("api/conversations", headers={"Cookie": "jarvis_token=falsch"}).status_code == 401


def test_middleware_local(make_client):
    assert make_client(client_host="127.0.0.1").get("api/status").status_code == 200
    assert make_client(client_host="192.168.1.9").get("api/status").status_code == 403


def test_addon_env_quoting():
    lines = export_lines({"anthropic_api_key": "sk-'x\"$(rm)", "mail_imap_port": 993, "web_search": False,
                          "mail_user": "", "unknown": "y"})
    assert "export ANTHROPIC_API_KEY='sk-'\"'\"'x\"$(rm)'" in lines
    assert "export MAIL_IMAP_PORT=993" in lines
    assert "export JARVIS_WEB_SEARCH=false" in lines
    assert not any("MAIL_USER" in line or "unknown" in line for line in lines)
