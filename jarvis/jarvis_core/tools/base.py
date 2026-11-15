"""Uniform tool interface, risk levels and the registry."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

READ = "read"
WRITE = "write"
CONFIRM = "confirm"
RISK_LEVELS = (READ, WRITE, CONFIRM)
_RISK_ORDER = {READ: 0, WRITE: 1, CONFIRM: 2}

Handler = Callable[[dict], Awaitable[Any]]
RiskFn = Callable[[dict], str]
SummaryFn = Callable[[dict], tuple[str, dict]]
PrecheckFn = Callable[[dict], None]

# Identifiers that end up in URLs or protocol commands.
HA_IDENT = r"^[a-z0-9_]+$"
HA_ENTITY = r"^[a-z0-9_]+\.[a-z0-9_]+$"
TODOIST_ID = r"^[A-Za-z0-9_-]+$"
IMAP_UID = r"^[0-9]+$"


class ToolError(Exception):
    """Expected failure that is reported back to the model as a tool error."""


def max_risk(*levels: str) -> str:
    return max(levels, key=lambda r: _RISK_ORDER.get(r, 2))


@dataclass
class Tool:
    name: str
    area: str
    description: str
    input_schema: dict
    handler: Handler
    risk: str | RiskFn = READ
    label: str = ""
    kind: str = ""
    summarize: SummaryFn | None = None
    precheck: PrecheckFn | None = None  # raises ToolError for input that could never run

    def risk_for(self, tool_input: dict) -> str:
        try:
            level = self.risk(tool_input) if callable(self.risk) else self.risk
        except Exception:  # noqa: BLE001 - malformed input is rated, not crashed on
            return CONFIRM
        # Anything unexpected is treated as the most cautious level.
        return level if level in RISK_LEVELS else CONFIRM

    def pending_info(self, tool_input: dict) -> tuple[str, str, dict]:
        """(kind, summary, details) for an action awaiting approval."""
        if self.summarize:
            summary, details = self.summarize(tool_input)
        else:
            summary, details = f"{self.label or self.name}", dict(tool_input)
        return self.kind or self.name, summary, details

    def api_definition(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "eager_input_streaming": True,
        }


@dataclass
class Registry:
    tools: dict[str, Tool] = field(default_factory=dict)
    areas: dict[str, bool] = field(default_factory=dict)

    def set_area(self, area: str, configured: bool) -> None:
        self.areas[area] = configured

    def register(self, tool: Tool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"duplicate tool {tool.name}")
        if not callable(tool.risk) and tool.risk not in RISK_LEVELS:
            raise ValueError(f"invalid risk {tool.risk!r} for {tool.name}")
        self.tools[tool.name] = tool
        self.areas[tool.area] = True

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def api_definitions(self) -> list[dict]:
        # Deterministic order keeps the prompt-cache prefix stable.
        return [self.tools[name].api_definition() for name in sorted(self.tools)]

    def status(self) -> list[dict]:
        return [{"area": a, "configured": c} for a, c in self.areas.items()]


def validate_input(schema: dict, value: Any, path: str = "input") -> list[str]:
    """Small JSON-Schema subset check (type, required, properties, enum, items).

    Tool inputs stream eagerly and are not validated by the API, so they are
    checked here before any tool runs.
    """
    errors: list[str] = []
    types = schema.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        if not any(_type_ok(t, value) for t in allowed):
            return [f"{path}: erwartet {'/'.join(allowed)}"]
    if isinstance(value, str):
        if "pattern" in schema and not re.fullmatch(schema["pattern"].strip("^$"), value):
            errors.append(f"{path}: ungültiges Format")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: zu lang (max. {schema['maxLength']} Zeichen)")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: muss einer von {schema['enum']} sein")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: fehlt")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value:
                errors.extend(validate_input(sub, value[key], f"{path}.{key}"))
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append(f"{path}.{key}: unbekanntes Feld")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            errors.extend(validate_input(schema["items"], item, f"{path}[{i}]"))
    if "anyOf" in schema:
        if all(validate_input(s, value, path) for s in schema["anyOf"]):
            errors.append(f"{path}: passt zu keiner erlaubten Form")
    return errors


def _type_ok(t: str, value: Any) -> bool:
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "null":
        return value is None
    return True


def require(pattern: str, value: Any, what: str) -> str:
    """Handler-side check of an identifier that goes into a URL or command."""
    if not isinstance(value, str) or not re.fullmatch(pattern.strip("^$"), value):
        raise ToolError(f"Ungültige {what}: {value!r}")
    return value


def readable(result: Any) -> str:
    """Short human-readable text for a tool result (shown after an approval)."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict) and isinstance(result.get("message"), str):
        return result["message"]
    import json
    return "Ausgeführt: " + json.dumps(result, ensure_ascii=False, default=str)[:500]
