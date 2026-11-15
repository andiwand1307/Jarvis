"""Todoist via the unified Todoist API v1 (https://api.todoist.com/api/v1)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from .base import READ, TODOIST_ID, WRITE, OutcomeUnknown, Registry, Tool, ToolError, require

API_URL = "https://api.todoist.com/api/v1"
MAX_ITEMS = 200


class TodoistClient:
    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self._token = token
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=API_URL,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=20.0,
            transport=self._transport,
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        async with self._client() as client:
            try:
                resp = await client.request(method, path, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                raise ToolError(f"Todoist nicht erreichbar: {exc}") from exc
            except httpx.HTTPError as exc:
                if method == "GET":
                    raise ToolError(f"Todoist antwortet nicht: {exc}") from exc
                raise OutcomeUnknown("Unklar, ob ausgeführt: Die Anfrage an Todoist wurde gesendet, aber "
                                     "es kam keine Antwort. Erst mit todoist_find_tasks prüfen, nicht einfach "
                                     "wiederholen.") from exc
        if resp.status_code >= 500 and method != "GET":
            raise OutcomeUnknown(f"Unklar, ob ausgeführt: Todoist meldet Fehler {resp.status_code}. Erst prüfen, "
                                 "nicht einfach wiederholen.")
        if resp.status_code in (401, 403):
            raise ToolError("Todoist lehnt den Zugang ab (Token prüfen).")
        if resp.status_code == 404:
            raise ToolError("Bei Todoist nicht gefunden.")
        if resp.status_code >= 400:
            raise ToolError(f"Todoist-Fehler {resp.status_code}: {resp.text[:200]}")
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def _paginate(self, path: str, params: dict) -> list[dict]:
        items: list[dict] = []
        cursor = ""
        while len(items) < MAX_ITEMS:
            query = dict(params, limit=min(200, MAX_ITEMS - len(items)))
            if cursor:
                query["cursor"] = cursor
            data = await self._request("GET", path, params=query)
            items.extend(data.get("results", []))
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return items

    async def find_tasks(self, query: str = "", project_id: str = "") -> list[dict]:
        if query:
            return await self._paginate("/tasks/filter", {"query": query, "lang": "de"})
        params = {"project_id": require(TODOIST_ID, project_id, "Projekt-ID")} if project_id else {}
        return await self._paginate("/tasks", params)

    async def add_task(self, body: dict) -> dict:
        return await self._request("POST", "/tasks", json=body)

    async def close_task(self, task_id: str) -> None:
        require(TODOIST_ID, task_id, "Aufgaben-ID")
        await self._request("POST", f"/tasks/{quote(task_id, safe='')}/close")

    async def projects(self) -> list[dict]:
        return await self._paginate("/projects", {})


def _compact_task(t: dict) -> dict:
    due = t.get("due") or {}
    out = {
        "id": t.get("id"),
        "content": t.get("content"),
        "project_id": t.get("project_id"),
        "priority": t.get("priority"),
    }
    if t.get("description"):
        out["description"] = t["description"]
    if t.get("labels"):
        out["labels"] = t["labels"]
    if due:
        out["due"] = due.get("string") or due.get("date")
        out["due_date"] = due.get("date")
    return out


def register(registry: Registry, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
    client = TodoistClient(token, transport)

    async def find_tasks(inp: dict) -> dict:
        tasks = await client.find_tasks(inp.get("query", ""), inp.get("project_id", ""))
        return {"count": len(tasks), "tasks": [_compact_task(t) for t in tasks]}

    async def add_task(inp: dict) -> dict:
        body: dict[str, Any] = {"content": inp["content"]}
        for key in ("description", "project_id", "due_string", "priority", "labels"):
            if inp.get(key) not in (None, "", []):
                body[key] = inp[key]
        if "project_id" in body:
            require(TODOIST_ID, body["project_id"], "Projekt-ID")
        if "due_string" in body:
            body["due_lang"] = "de"
        task = await client.add_task(body)
        return {"created": _compact_task(task or {})}

    async def complete_task(inp: dict) -> dict:
        await client.close_task(str(inp["task_id"]))
        return {"completed": inp["task_id"], "message": "Aufgabe abgehakt."}

    async def list_projects(inp: dict) -> dict:
        projects = await client.projects()
        return {"projects": [{"id": p.get("id"), "name": p.get("name")} for p in projects]}

    registry.register(Tool(
        name="todoist_find_tasks",
        area="todoist",
        description=(
            "Sucht offene Todoist-Aufgaben. Nutze es für Fragen nach Aufgaben, Erledigungen oder der "
            "Einkaufsliste. `query` ist ein Todoist-Filter (z. B. 'heute', 'überfällig', '#Einkaufsliste', "
            "'suche: Zahnarzt'); alternativ `project_id` für alle Aufgaben eines Projekts."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Todoist-Filterausdruck."},
                "project_id": {"type": "string", "pattern": TODOIST_ID,
                               "description": "Nur Aufgaben dieses Projekts."},
            },
            "additionalProperties": False,
        },
        handler=find_tasks,
        risk=READ,
        label="Schaue in Todoist nach …",
    ))
    registry.register(Tool(
        name="todoist_add_task",
        area="todoist",
        description=(
            "Legt eine Todoist-Aufgabe an. Für Einkäufe: erst mit todoist_list_projects die ID des "
            "Einkaufslisten-Projekts holen und als project_id angeben. Fälligkeit in natürlicher Sprache "
            "(deutsch) in due_string, z. B. 'morgen 9 Uhr'. Priorität 4 = dringend, 1 = normal."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Titel der Aufgabe."},
                "description": {"type": "string"},
                "project_id": {"type": "string", "pattern": TODOIST_ID},
                "due_string": {"type": "string"},
                "priority": {"type": "integer", "enum": [1, 2, 3, 4]},
                "labels": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["content"],
            "additionalProperties": False,
        },
        handler=add_task,
        risk=WRITE,
        label="Lege eine Aufgabe an …",
    ))
    registry.register(Tool(
        name="todoist_complete_task",
        area="todoist",
        description="Hakt eine Todoist-Aufgabe als erledigt ab. Die ID kommt aus todoist_find_tasks.",
        input_schema={
            "type": "object",
            "properties": {"task_id": {"type": "string", "pattern": TODOIST_ID}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
        handler=complete_task,
        risk=WRITE,
        label="Hake eine Aufgabe ab …",
    ))
    registry.register(Tool(
        name="todoist_list_projects",
        area="todoist",
        description="Listet alle Todoist-Projekte mit ID und Namen (die Einkaufsliste ist ein Projekt).",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=list_projects,
        risk=READ,
        label="Hole die Todoist-Projekte …",
    ))
