"""Memory notes about the user (local SQLite)."""

from __future__ import annotations

from ..store import Store
from .base import READ, WRITE, Registry, Tool, ToolError

MAX_NOTE_CHARS = 500
MAX_NOTES = 200


def register(registry: Registry, store: Store) -> None:
    async def save(inp: dict) -> dict:
        text = inp["text"].strip()
        if not text:
            raise ToolError("Leere Notiz.")
        if len(text) > MAX_NOTE_CHARS:
            raise ToolError(f"Notiz zu lang (max. {MAX_NOTE_CHARS} Zeichen). Bitte kürzer fassen.")
        if store.count_notes() >= MAX_NOTES:
            raise ToolError(f"Es sind schon {MAX_NOTES} Notizen gespeichert. Erst veraltete löschen.")
        return {"id": store.add_note(text), "saved": text, "message": "Notiz gespeichert."}

    async def search(inp: dict) -> dict:
        notes = store.search_notes(inp.get("query", ""), limit=int(inp.get("limit", 30)))
        return {"notes": notes}

    async def delete(inp: dict) -> dict:
        if not store.delete_note(int(inp["id"])):
            raise ToolError("Notiz nicht gefunden.")
        return {"deleted": inp["id"]}

    registry.register(Tool(
        name="memory_save",
        area="memory",
        description=(
            "Merkt sich eine dauerhafte Notiz über den Nutzer (Vorlieben, Personen, Gewohnheiten, "
            "wiederkehrende Fakten). Nutze es, wenn der Nutzer sagt, dass du dir etwas merken sollst, "
            "oder wenn er dir einen dauerhaft nützlichen Fakt nennt. Keine Passwörter oder Zugangsdaten."
        ),
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string", "maxLength": MAX_NOTE_CHARS,
                                    "description": "Die Notiz als ganzer Satz."}},
            "required": ["text"],
            "additionalProperties": False,
        },
        handler=save,
        risk=WRITE,
        label="Merke mir das …",
    ))
    registry.register(Tool(
        name="memory_search",
        area="memory",
        description=(
            "Durchsucht die gespeicherten Notizen über den Nutzer. Nutze es, wenn persönlicher Kontext "
            "hilft (z. B. Namen, Vorlieben, Adressen). Ohne Suchbegriff kommen die neuesten Notizen."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Suchbegriffe, leer für alle."},
                "limit": {"type": "integer", "description": "Höchstzahl, Standard 30."},
            },
            "additionalProperties": False,
        },
        handler=search,
        risk=READ,
        label="Schaue in meinen Notizen nach …",
    ))
    registry.register(Tool(
        name="memory_delete",
        area="memory",
        description="Löscht eine gespeicherte Notiz anhand ihrer ID (wenn sie falsch oder veraltet ist).",
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
            "additionalProperties": False,
        },
        handler=delete,
        risk=WRITE,
        label="Lösche eine Notiz …",
    ))
