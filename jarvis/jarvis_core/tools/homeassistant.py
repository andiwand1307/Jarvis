"""Home Assistant REST API: states, services, calendars."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import quote

import httpx

from ..config import HAConfig
from .base import CONFIRM, HA_ENTITY, HA_IDENT, READ, WRITE, Registry, Tool, ToolError, require

# Positive list (architecture doc, section 4): only these (domain, service) pairs can be `write`,
# and only when every target is an entity_id of that same domain. Everything else is `confirm`.
# `switch` is on the list although a switch can drive something sensitive (a socket, a door
# opener relay); such switches belong behind a lock/cover entity or should not be exposed.
SAFE_SERVICES: dict[str, frozenset[str]] = {
    "light": frozenset({"turn_on", "turn_off", "toggle"}),
    "switch": frozenset({"turn_on", "turn_off", "toggle"}),
    "fan": frozenset({"turn_on", "turn_off", "toggle", "set_percentage", "increase_speed",
                      "decrease_speed", "oscillate", "set_direction", "set_preset_mode"}),
    "media_player": frozenset({"turn_on", "turn_off", "toggle", "volume_up", "volume_down", "volume_set",
                               "volume_mute", "media_play", "media_pause", "media_play_pause", "media_stop",
                               "media_next_track", "media_previous_track", "media_seek", "select_source",
                               "select_sound_mode", "shuffle_set", "repeat_set", "play_media"}),
    "climate": frozenset({"turn_on", "turn_off", "set_temperature", "set_hvac_mode", "set_preset_mode",
                          "set_fan_mode", "set_humidity", "set_swing_mode"}),
}
# Scenes and scripts can switch anything; only entities the user listed in HA_TRUSTED_ENTITIES
# may run without approval, and only through these services.
TRUSTED_SERVICES: dict[str, frozenset[str]] = {
    "scene": frozenset({"turn_on"}),
    "script": frozenset({"turn_on"}),
}
TARGET_KEYS = ("entity_id", "area_id", "device_id", "floor_id", "label_id")

_IDENT = re.compile(HA_IDENT.strip("^$"))
_ENTITY = re.compile(HA_ENTITY.strip("^$"))
MAX_STATES = 150


def _as_list(value: Any) -> list | None:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    return None  # malformed


def collect_targets(inp: dict) -> tuple[list[str], bool]:
    """(entity_ids, only_entities). `only_entities` is False when any area/device/floor/label
    target is present or a target has an unexpected shape."""
    data = inp.get("data")
    if data is not None and not isinstance(data, dict):
        return [], False
    data = data or {}
    sources = [inp, data]
    if "target" in data:
        if not isinstance(data["target"], dict):
            return [], False
        sources.append(data["target"])
    entities: list[str] = []
    only_entities = True
    for src in sources:
        for key in TARGET_KEYS:
            if key not in src:
                continue
            values = _as_list(src[key])
            if values is None:
                return [], False
            if key == "entity_id":
                entities.extend(values)
            elif values:
                only_entities = False
    return entities, only_entities


def service_risk(inp: dict, trusted: Iterable[str] = ()) -> str:
    domain, service = inp.get("domain"), inp.get("service")
    if not isinstance(domain, str) or not isinstance(service, str):
        return CONFIRM
    if not _IDENT.fullmatch(domain) or not _IDENT.fullmatch(service):
        return CONFIRM
    entities, only_entities = collect_targets(inp)
    if not only_entities or not all(_ENTITY.fullmatch(e) for e in entities):
        return CONFIRM
    trusted = set(trusted)

    if domain == "script" and service not in TRUSTED_SERVICES["script"] and service != "reload":
        # script.<name> runs that script directly.
        return WRITE if f"script.{service}" in trusted and not entities else CONFIRM
    if not entities:
        return CONFIRM
    if service in SAFE_SERVICES.get(domain, ()):
        return WRITE if all(e.split(".", 1)[0] == domain for e in entities) else CONFIRM
    if service in TRUSTED_SERVICES.get(domain, ()):
        ok = all(e.split(".", 1)[0] == domain and e in trusted for e in entities)
        return WRITE if ok else CONFIRM
    return CONFIRM


class HAClient:
    def __init__(self, cfg: HAConfig, transport: httpx.AsyncBaseTransport | None = None):
        self._cfg = cfg
        self._transport = transport

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        async with httpx.AsyncClient(
            base_url=self._cfg.base_url,
            headers={"Authorization": f"Bearer {self._cfg.token}"},
            timeout=20.0,
            transport=self._transport,
        ) as client:
            try:
                resp = await client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                raise ToolError(f"Home Assistant nicht erreichbar: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ToolError("Home Assistant lehnt den Zugang ab.")
        if resp.status_code == 404:
            raise ToolError("In Home Assistant nicht gefunden.")
        if resp.status_code >= 400:
            raise ToolError(f"Home-Assistant-Fehler {resp.status_code}: {resp.text[:200]}")
        return resp.json() if resp.content else None


def _seg(value: str) -> str:
    return quote(value, safe="")


def _compact_state(s: Any) -> dict:
    if not isinstance(s, dict):
        return {}
    attrs = s.get("attributes") or {}
    out = {"entity_id": s.get("entity_id"), "state": s.get("state")}
    if attrs.get("friendly_name"):
        out["name"] = attrs["friendly_name"]
    for key in ("unit_of_measurement", "current_temperature", "temperature", "brightness",
                "media_title", "device_class"):
        if key in attrs:
            out[key] = attrs[key]
    return out


def _summary(inp: dict) -> tuple[str, dict]:
    service = f"{inp.get('domain')}.{inp.get('service')}"
    entities, _ = collect_targets(inp)
    data = inp.get("data") if isinstance(inp.get("data"), dict) else {}
    others = {k: data[k] for k in TARGET_KEYS[1:] if k in data}
    target = ", ".join(entities) if entities else (", ".join(f"{k}={v}" for k, v in others.items()) or "ohne Ziel")
    details = {
        "domain": inp.get("domain"),
        "service": inp.get("service"),
        "entity_id": inp.get("entity_id", []),
        "data": inp.get("data") or {},
    }
    return f"Home Assistant: {service} für {target}", details


def register(registry: Registry, cfg: HAConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
    client = HAClient(cfg, transport)
    trusted = frozenset(cfg.trusted_entities)

    async def get_states(inp: dict) -> dict:
        eid = inp.get("entity_id", "")
        if eid:
            require(HA_ENTITY, eid, "entity_id")
            return {"states": [_compact_state(await client.request("GET", f"/states/{_seg(eid)}"))]}
        states = await client.request("GET", "/states") or []
        domain = inp.get("domain", "").strip().lower()
        search = inp.get("search", "").strip().lower()
        hits = []
        for s in states if isinstance(states, list) else []:
            sid = str(s.get("entity_id", ""))
            if domain and not sid.startswith(domain + "."):
                continue
            name = str((s.get("attributes") or {}).get("friendly_name", ""))
            if search and search not in sid.lower() and search not in name.lower():
                continue
            hits.append(_compact_state(s))
        truncated = len(hits) > MAX_STATES
        return {"count": len(hits), "truncated": truncated, "states": hits[:MAX_STATES]}

    async def call_service(inp: dict) -> dict:
        domain = require(HA_IDENT, inp.get("domain"), "Domain")
        service = require(HA_IDENT, inp.get("service"), "Dienstname")
        body = dict(inp.get("data") or {})
        targets = _as_list(inp.get("entity_id"))
        if targets is None:
            raise ToolError("Ungültige entity_id.")
        if targets:
            body["entity_id"] = targets
        changed = await client.request("POST", f"/services/{_seg(domain)}/{_seg(service)}", json=body)
        changed = changed if isinstance(changed, list) else []
        return {"ok": True, "service": f"{domain}.{service}", "message": f"{domain}.{service} ausgeführt.",
                "changed": [_compact_state(s) for s in changed[:20]]}

    async def calendar_events(inp: dict) -> dict:
        start = inp.get("start") or datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            if inp.get("end"):
                end = datetime.fromisoformat(inp["end"].replace("Z", "+00:00")).isoformat(timespec="seconds")
            else:
                end = (start_dt + timedelta(days=int(inp.get("days") or 7))).isoformat(timespec="seconds")
        except ValueError as exc:
            raise ToolError("Ungültiges Datum (ISO 8601 erwartet).") from exc
        start = start_dt.isoformat(timespec="seconds")
        if inp.get("entity_id"):
            calendars = [require(r"^calendar\.[a-z0-9_]+$", inp["entity_id"], "Kalender-Entität")]
        else:
            listed = await client.request("GET", "/calendars") or []
            calendars = [c.get("entity_id") for c in listed
                         if isinstance(c, dict) and _ENTITY.fullmatch(str(c.get("entity_id", "")))]
        events = []
        for cal in calendars:
            found = await client.request("GET", f"/calendars/{_seg(cal)}", params={"start": start, "end": end})
            for ev in found if isinstance(found, list) else []:
                s, e = ev.get("start") or {}, ev.get("end") or {}
                events.append({
                    "calendar": cal,
                    "summary": ev.get("summary"),
                    "start": s.get("dateTime") or s.get("date"),
                    "end": e.get("dateTime") or e.get("date"),
                    "location": ev.get("location"),
                    "description": (ev.get("description") or "")[:500] or None,
                })
        events.sort(key=lambda x: str(x["start"]))
        return {"range": {"start": start, "end": end}, "calendars": calendars, "events": events}

    registry.register(Tool(
        name="ha_get_states",
        area="homeassistant",
        description=(
            "Liest Zustände aus Home Assistant (Lichter, Sensoren, Türen, Heizung …). Nutze es, bevor du "
            "einen Dienst aufrufst, um die richtige entity_id zu finden. Filter: entity_id (genau eine), "
            "domain (z. B. 'light') oder search (Teil von Name oder ID)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "pattern": HA_ENTITY},
                "domain": {"type": "string"},
                "search": {"type": "string"},
            },
            "additionalProperties": False,
        },
        handler=get_states,
        risk=READ,
        label="Frage Home Assistant ab …",
    ))
    registry.register(Tool(
        name="ha_call_service",
        area="homeassistant",
        description=(
            "Ruft einen Home-Assistant-Dienst auf, z. B. domain 'light', service 'turn_on', entity_id "
            "'light.wohnzimmer'. Gib Ziele immer als entity_id an. Sofort laufen nur Standarddienste von "
            "Licht, Schaltern, Ventilatoren, Medienplayern und Heizung auf Entitäten derselben Domain sowie "
            "vom Nutzer freigeschaltete Szenen und Skripte. Alles andere (Schlösser, Alarm, Rollläden, "
            "Garagentore, Bereiche/Geräte als Ziel, unbekannte Dienste) braucht eine Freigabe durch den "
            "Nutzer in der Oberfläche."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "domain": {"type": "string", "pattern": HA_IDENT},
                "service": {"type": "string", "pattern": HA_IDENT},
                "entity_id": {
                    "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                    "description": "Ziel-Entität(en).",
                },
                "data": {"type": "object", "description": "Weitere Dienstdaten, z. B. brightness_pct."},
            },
            "required": ["domain", "service"],
            "additionalProperties": False,
        },
        handler=call_service,
        risk=lambda inp: service_risk(inp, trusted),
        label="Steuere Home Assistant …",
        kind="ha_service",
        summarize=_summary,
    ))
    registry.register(Tool(
        name="ha_calendar_events",
        area="homeassistant",
        description=(
            "Liest Kalendertermine aus den Home-Assistant-Kalendern. Ohne entity_id werden alle Kalender "
            "gelesen. Zeitraum: start/end als ISO-8601 mit Zeitzone, oder start plus days (Standard 7 Tage "
            "ab jetzt)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "pattern": r"^calendar\.[a-z0-9_]+$",
                              "description": "z. B. calendar.familie"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "days": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        handler=calendar_events,
        risk=READ,
        label="Schaue in den Kalender …",
    ))
