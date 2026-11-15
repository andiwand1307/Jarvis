"""Builds the tool registry from the configuration."""

from __future__ import annotations

from ..config import Config
from ..store import Store
from . import homeassistant, mail, memory, todoist
from .base import CONFIRM, READ, WRITE, Registry, Tool, ToolError, validate_input

AREAS = ("todoist", "mail", "homeassistant", "memory", "web")

WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 5,
    "user_location": {"type": "approximate", "country": "DE", "timezone": "Europe/Berlin"},
}


def build_registry(config: Config, store: Store) -> Registry:
    registry = Registry()
    for area in AREAS:
        registry.set_area(area, False)

    if config.todoist_token:
        todoist.register(registry, config.todoist_token)
    if config.mail.configured:
        mail.register(registry, config.mail)
    if config.ha.configured:
        homeassistant.register(registry, config.ha)
    memory.register(registry, store)
    registry.set_area("web", config.web_search)
    return registry


def server_tools(config: Config) -> list[dict]:
    if not config.web_search:
        return []
    tool = dict(WEB_SEARCH_TOOL)
    tool["user_location"] = dict(tool["user_location"], timezone=config.timezone)
    return [tool]


__all__ = [
    "AREAS", "CONFIRM", "READ", "WRITE", "Registry", "Tool", "ToolError",
    "build_registry", "server_tools", "validate_input",
]
