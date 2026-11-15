"""Home Assistant REST API: states, services, calendars."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..config import HAConfig
from .base import CONFIRM, READ, WRITE, Registry, Tool, ToolError, max_risk

# Contract (architecture doc, section 4): these domains are `write`, everything else `confirm`.
WRITE_DOMAINS = frozenset({"light", "switch", "media_player", "scene", "script", "climate"})
CONFIRM_DOMAINS = frozenset({"lock", "alarm_control_panel", "cover", "siren", "valve"})

MAX_STATES = 150


def domain_risk(domain: str) -> str:
    domain = (domain or "").strip().lower()
    if domain in WRITE_DOMAINS:
        return WRITE
    return CONFIRM


def _entity_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def service_risk(inp: dict) -> str:
    """Risk of a service call: the service domain and every targeted entity's domain count."""
    levels = [domain_risk(inp.get("domain", ""))]
    data = inp.get("data") if isinstance(inp.get("data"), dict) else {}
    targets = _entity_ids(inp.get("entity_id")) + _entity_ids(data.get("entity_id"))
    for eid in targets:
        if eid == "all":
            levels.append(CONFIRM)
        else:
            levels.append(domain_risk(eid.split(".", 1)[0]))
    return max_risk(*levels)


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


def _compact_state(s: dict) -> dict:
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
    targets = _entity_ids(inp.get("entity_id")) or _entity_ids((inp.get("data") or {}).get("entity_id"))
    target = ", ".join(targets) if targets else "ohne Ziel-Entität"
    details = {"service": service, "entity_id": targets, "data": inp.get("data") or {}}
    return f"Home Assistant: {service} für {target}", details


def register(registry: Registry, cfg: HAConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
    client = HAClient(cfg, transport)

    async def get_states(inp: dict) -> dict:
        eid = inp.get("entity_id", "").strip()
        if eid:
            return {"states": [_compact_state(await client.request("GET", f"/states/{eid}"))]}
        states = await client.request("GET", "/states") or []
        domain = inp.get("domain", "").strip().lower()
        search = inp.get("search", "").strip().lower()
        hits = []
        for s in states:
            sid = s.get("entity_id", "")
            if domain and not sid.startswith(domain + "."):
                continue
            name = str((s.get("attributes") or {}).get("friendly_name", ""))
            if search and search not in sid.lower() and search not in name.lower():
                continue
            hits.append(_compact_state(s))
        truncated = len(hits) > MAX_STATES
        return {"count": len(hits), "truncated": truncated, "states": hits[:MAX_STATES]}

    async def call_service(inp: dict) -> dict:
        domain, service = inp["domain"].strip(), inp["service"].strip()
        body = dict(inp.get("data") or {})
        targets = _entity_ids(inp.get("entity_id"))
        if targets:
            body["entity_id"] = targets
        changed = await client.request("POST", f"/services/{domain}/{service}", json=body)
        changed = changed if isinstance(changed, list) else []
        return {"ok": True, "service": f"{domain}.{service}",
                "changed": [_compact_state(s) for s in changed[:20]]}

    async def calendar_events(inp: dict) -> dict:
        start = inp.get("start") or datetime.now(timezone.utc).isoformat(timespec="seconds")
        if inp.get("end"):
            end = inp["end"]
        else:
            days = int(inp.get("days") or 7)
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end = (start_dt + timedelta(days=days)).isoformat(timespec="seconds")
        calendars = [inp["entity_id"]] if inp.get("entity_id") else [
            c.get("entity_id") for c in (await client.request("GET", "/calendars") or [])
        ]
        events = []
        for cal in calendars:
            for ev in await client.request("GET", f"/calendars/{cal}", params={"start": start, "end": end}) or []:
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
                "entity_id": {"type": "string"},
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
            "'light.wohnzimmer'. Licht, Schalter, Medien, Szenen, Skripte und Heizung laufen sofort. "
            "Schlösser, Alarmanlage, Rollläden/Garagentore, Sirenen, Ventile und alle anderen Domains "
            "brauchen eine Freigabe durch den Nutzer in der Oberfläche."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "service": {"type": "string"},
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
        risk=service_risk,
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
                "entity_id": {"type": "string", "description": "z. B. calendar.familie"},
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
