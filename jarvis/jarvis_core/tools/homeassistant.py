"""Home Assistant REST API: states, services, calendars."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import quote

import httpx

from ..config import HAConfig
from .base import (CONFIRM, HA_ENTITY, HA_IDENT, READ, WRITE, OutcomeUnknown, Registry, Tool, ToolError,
                   bounded_int, require)

# Positive list (architecture doc, section 4): only these (domain, service) pairs can be `write`,
# and only when every target is an entity_id of that same domain. Everything else is `confirm`.
SAFE_SERVICES: dict[str, frozenset[str]] = {
    "light": frozenset({"turn_on", "turn_off", "toggle"}),
    "fan": frozenset({"turn_on", "turn_off", "toggle", "set_percentage", "increase_speed",
                      "decrease_speed", "oscillate", "set_direction", "set_preset_mode"}),
    # Playback control and volume only; nothing that starts content, URLs, sources or apps.
    "media_player": frozenset({"turn_on", "turn_off", "toggle", "volume_up", "volume_down", "volume_set",
                               "volume_mute", "media_play", "media_pause", "media_play_pause", "media_stop",
                               "media_next_track", "media_previous_track", "media_seek", "shuffle_set",
                               "repeat_set"}),
    "climate": frozenset({"turn_on", "turn_off", "set_temperature", "set_hvac_mode", "set_preset_mode",
                          "set_fan_mode", "set_humidity", "set_swing_mode"}),
}
# Scenes, scripts and switches can drive anything (a switch may be a door relay). Only entities
# the user listed in HA_TRUSTED_ENTITIES run without approval, only via these services and only
# without extra service data (no script variables, no scene entities).
TRUSTED_SERVICES: dict[str, frozenset[str]] = {
    "scene": frozenset({"turn_on"}),
    "script": frozenset({"turn_on"}),
    "switch": frozenset({"turn_on", "turn_off", "toggle"}),
}
TARGET_KEYS = ("entity_id", "area_id", "device_id", "floor_id", "label_id")
# Any data key that looks like a target selector (also misspelled/plural) counts as unresolved.
_TARGET_LIKE = re.compile(r"^(target|(area|device|floor|label)s?(_?ids?)?)$", re.I)

_IDENT = re.compile(HA_IDENT.strip("^$"))
_ENTITY = re.compile(HA_ENTITY.strip("^$"))
MAX_STATES = 150


def _as_list(value: Any) -> list | None:
    if value is None:
        return []
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        return None  # malformed
    if any(re.search(r"[\x00-\x1f\x7f]", v) for v in values):
        return None
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [v.strip() for v in values]


def build_request(inp: dict) -> tuple[str, str, dict, dict]:
    """(domain, service, target, data) exactly as sent: body = {**data, **target}.

    Raises ToolError for malformed input: bad names, bad entity ids, data that is not an
    object, or a target given in more than one place."""
    domain = require(HA_IDENT, inp.get("domain"), "Domain")
    service = require(HA_IDENT, inp.get("service"), "Dienstname")
    data = inp.get("data")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ToolError("data muss ein Objekt sein.")
    data = dict(data)
    target: dict[str, Any] = {}
    places = 0
    if "entity_id" in inp:
        places += 1
        target["entity_id"] = inp["entity_id"]
    if "target" in data:
        places += 1
        nested = data.pop("target")
        if not isinstance(nested, dict):
            raise ToolError("data.target muss ein Objekt sein.")
        target.update(nested)
    selectors = {k: data.pop(k) for k in list(data) if k == "entity_id" or _TARGET_LIKE.match(k)}
    if selectors:
        places += 1
        target.update(selectors)
    if places > 1:
        raise ToolError("Ziel bitte nur an einer Stelle angeben (entity_id oben ODER in data).")
    if "entity_id" in target:
        ids = _as_list(target["entity_id"])
        if ids is None or not ids:
            raise ToolError("Ungültige entity_id.")
        for eid in ids:
            if eid != "all":
                require(HA_ENTITY, eid, "entity_id")
        target["entity_id"] = ids
    return domain, service, target, data


def service_risk(inp: dict, trusted: Iterable[str] = ()) -> str:
    try:
        domain, service, target, data = build_request(inp)
    except ToolError:
        return CONFIRM
    if set(target) - {"entity_id"}:
        return CONFIRM  # area/device/floor/label: unresolved
    entities = target.get("entity_id", [])
    if "all" in entities:
        return CONFIRM
    trusted = set(trusted)

    if domain == "script" and service not in TRUSTED_SERVICES["script"]:
        # script.<name> runs that script directly; never with data (variables).
        if service in ("reload", "turn_off", "toggle") or entities or data:
            return CONFIRM
        return WRITE if f"script.{service}" in trusted else CONFIRM
    if not entities:
        return CONFIRM
    if any(e.split(".", 1)[0] != domain for e in entities):
        return CONFIRM
    if service in SAFE_SERVICES.get(domain, ()):
        return WRITE
    if service in TRUSTED_SERVICES.get(domain, ()):
        return WRITE if not data and all(e in trusted for e in entities) else CONFIRM
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
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol) as exc:
                raise ToolError(f"Home Assistant nicht erreichbar: {exc}") from exc
            except httpx.HTTPError as exc:
                # Sent (at least partly) but no answer: it may have taken effect.
                if method == "GET":
                    raise ToolError(f"Home Assistant antwortet nicht: {exc}") from exc
                raise OutcomeUnknown(
                    "Ergebnis unbekannt: Die Anfrage wurde gesendet, aber Home Assistant hat nicht "
                    "geantwortet. Nicht erneut senden, erst den Zustand nachsehen.") from exc
        if resp.status_code in (401, 403):
            raise ToolError("Home Assistant lehnt den Zugang ab.")
        if resp.status_code == 404:
            raise ToolError("In Home Assistant nicht gefunden.")
        if resp.status_code >= 500 and method != "GET":
            raise OutcomeUnknown(
                f"Ergebnis unbekannt: Home Assistant meldet Fehler {resp.status_code}. Der Dienst kann "
                "teilweise gelaufen sein. Nicht erneut senden, erst den Zustand nachsehen.")
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
    """Card text and details built from exactly the request that will be sent."""
    try:
        domain, service, target, data = build_request(inp)
    except ToolError as exc:
        return f"Home Assistant: ungültiger Aufruf ({exc})", {"input": inp}
    parts = []
    for key, value in target.items():
        if key == "entity_id":
            parts.append(", ".join(value))
        else:
            parts.append(f"{key} {value if isinstance(value, str) and value.strip() else json.dumps(value)}")
    details = {"domain": domain, "service": service, "target": target, "data": data,
               "body": {**data, **target}}
    if not parts:
        return f"Home Assistant: {domain}.{service} (ohne Ziel)", details
    return f"Home Assistant: {domain}.{service} für {'; '.join(parts)}", details


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
        domain, service, target, data = build_request(inp)
        body = {**data, **target}
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
                days = bounded_int(inp.get("days"), 1, 366, "days", default=7)
                end = (start_dt + timedelta(days=days)).isoformat(timespec="seconds")
        except (ValueError, OverflowError) as exc:
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
            "'light.wohnzimmer'. Gib das Ziel genau einmal an, als entity_id oben. Sofort laufen nur "
            "Licht, Ventilatoren, Heizung, Wiedergabe-Steuerung und Lautstärke von Medienplayern auf "
            "Entitäten derselben Domain sowie vom Nutzer freigeschaltete Schalter, Szenen und Skripte ohne "
            "Zusatzdaten. Alles andere (Schlösser, Alarm, Rollläden, Garagentore, andere Schalter, Medien "
            "abspielen, Bereiche/Geräte als Ziel, unbekannte Dienste) braucht eine Freigabe durch den "
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
                "data": {"type": ["object", "null"], "description": "Weitere Dienstdaten, z. B. brightness_pct."},
            },
            "required": ["domain", "service"],
            "additionalProperties": False,
        },
        handler=call_service,
        risk=lambda inp: service_risk(inp, trusted),
        label="Steuere Home Assistant …",
        kind="ha_service",
        summarize=_summary,
        precheck=lambda inp: build_request(inp) and None,
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
                "days": {"type": "integer", "minimum": 1, "maximum": 366},
            },
            "additionalProperties": False,
        },
        handler=calendar_events,
        risk=READ,
        label="Schaue in den Kalender …",
    ))
