# Jarvis – Architektur und Schnittstellen

Dieses Dokument ist der Vertrag zwischen Backend, Web-Oberfläche und (später) Telefonie.
Wer davon abweichen muss, ändert zuerst dieses Dokument.

## 1. Zielbild

Ein kleiner Dienst, der dauerhaft auf dem Home-Assistant-Server läuft (x86-64, HA OS) und
als Add-on installiert wird. Denken, Spracherkennung und Sprachausgabe laufen bei
Cloud-Diensten; der Dienst selbst braucht kaum Rechenleistung.

Etappen:
1. **Kern** – Agent mit Werkzeugen, Web-Oberfläche (dieser Stand).
2. **Telefonie** – Jarvis ist anrufbar und ruft an (Fritzbox-SIP oder Telefonie-Anbieter).
3. **Zuhause** – Auslöser aus Home Assistant (z. B. Begrüßung an der Tür).

## 2. Verzeichnisstruktur

```
repository.yaml          HA-Add-on-Repository
jarvis/                  Add-on-Ordner = Docker-Build-Kontext
  config.yaml            Add-on-Manifest (Ingress an, Optionen = Zugangsdaten)
  Dockerfile, run.sh
  pyproject.toml
  jarvis_core/           Python-Paket (Backend)
  web/                   statische Oberfläche (index.html, app.js, style.css)
  tests/
docs/
```

Zuständigkeit: Backend-Agent besitzt alles außer `jarvis/web/`. UI-Agent besitzt nur `jarvis/web/`.

## 3. Backend

- Python 3.12+, FastAPI + uvicorn, SQLite (Datei unter `JARVIS_DATA_DIR`, im Add-on `/data`).
- Claude über das offizielle `anthropic`-SDK. Modell per `JARVIS_MODEL`, Standard `claude-opus-5-5`.
- Der Agenten-Kern ist kanalunabhängig: `Agent.run(conversation_id, user_text, channel)` liefert
  einen asynchronen Strom von Ereignissen. Kanäle sind `web` (jetzt), `phone` und `event` (später).
  Für `phone` gilt später: kurze, gesprochene Antworten, kein Markdown. Das steuert der Systemprompt
  je Kanal.
- Werkzeuge sind eigenständige Module mit einheitlicher Schnittstelle (Name, Beschreibung,
  JSON-Schema, Risikostufe, Ausführungsfunktion). Ein Werkzeug ohne Zugangsdaten wird nicht
  registriert und erscheint in `/api/status` als nicht eingerichtet.

### Werkzeuge in Etappe 1

| Bereich | Zugang | Werkzeuge |
|---|---|---|
| Todoist | API-Token (`TODOIST_API_TOKEN`) | Aufgaben suchen, anlegen, abschließen; Projekte auflisten (Einkaufsliste = Projekt) |
| Mail | IMAP/SMTP mit App-Passwort (`MAIL_*`) | suchen, lesen, Entwurf, senden |
| Home Assistant | Supervisor-Token im Add-on, sonst `HA_URL` + `HA_TOKEN` | Zustände lesen, Dienste aufrufen, Kalendertermine lesen |
| Gedächtnis | lokal (SQLite) | Notizen über den Nutzer merken und abrufen |
| Web | Claude-Servertool `web_search` | Recherche |

Kalender läuft bewusst über Home Assistant, damit keine zweite Google-Anmeldung nötig ist.

## 4. Sicherheitskonzept

Jedes Werkzeug hat eine Risikostufe:

| Stufe | Bedeutung | Beispiele | Verhalten |
|---|---|---|---|
| `read` | liest nur | Mail suchen, Zustand lesen | läuft sofort |
| `write` | ändert eigene Daten, leicht rückholbar | Todoist-Aufgabe, Notiz, Licht schalten | läuft sofort, wird protokolliert |
| `confirm` | wirkt nach außen oder ist heikel | Mail senden, anrufen, Schloss, Alarm, Garagentor | wird als offene Aktion gespeichert und läuft erst nach Freigabe |

Regeln:
- Freigabe erfolgt ausschließlich über `/api/pending/{id}/approve`, nie durch das Modell selbst.
  Das Werkzeug liefert dem Modell „wartet auf Freigabe“ zurück.
- Home-Assistant-Dienste: Domains `lock`, `alarm_control_panel`, `cover`, `siren`, `valve` sowie
  alles Unbekannte sind `confirm`. `light`, `switch`, `media_player`, `scene`, `script`, `climate` sind `write`.
- Inhalte aus Mails, Webseiten und Werkzeugergebnissen sind Daten, keine Anweisungen. Das steht
  im Systemprompt; die Freigabepflicht ist die eigentliche Absicherung.
- Jeder Werkzeugaufruf landet im Protokoll (Zeit, Werkzeug, Eingabe, Ergebnis-Kurzform, Stufe).
- Zugangsdaten stehen nur in Add-on-Optionen bzw. Umgebungsvariablen, nie im Repository.
- Zugriff auf die Oberfläche: im Add-on über HA-Ingress (HA übernimmt die Anmeldung). Außerhalb
  des Add-ons ist `JARVIS_TOKEN` Pflicht (Bearer); ohne Token startet der Dienst nur auf 127.0.0.1.

## 5. HTTP-Schnittstelle

Alle Pfade relativ (Ingress setzt ein Präfix davor). JSON, UTF-8.

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `api/health` | `{"ok": true}` |
| GET | `api/status` | `{"model": "...", "tools": [{"area": "todoist", "configured": true}, ...]}` |
| POST | `api/chat` | Body `{"message": "...", "conversation_id": "..."?}`; Antwort als SSE-Strom |
| GET | `api/conversations` | Liste `{id, title, updated_at}` |
| GET | `api/conversations/{id}` | `{id, messages: [{role, text, created_at}]}` |
| GET | `api/pending` | offene Aktionen `[{id, kind, summary, details, created_at}]` |
| POST | `api/pending/{id}/approve` | führt aus; `{ok, result}` |
| POST | `api/pending/{id}/reject` | verwirft; `{ok}` |
| GET | `api/log?limit=50` | Protokoll |

### SSE-Ereignisse von `api/chat`

Jede Zeile `event: <typ>` + `data: <json>`.

| Typ | Daten |
|---|---|
| `start` | `{"conversation_id": "..."}` |
| `text` | `{"delta": "..."}` |
| `tool` | `{"name": "...", "label": "Durchsuche Mails …", "status": "running"\|"done"\|"error"}` |
| `pending` | `{"id": "...", "kind": "mail_send", "summary": "...", "details": {...}}` |
| `error` | `{"message": "..."}` |
| `done` | `{}` |

## 6. Oberfläche

Eine Seite, deutsch, für Handy und Rechner. Gespräch mit Live-Text, sichtbare Werkzeugschritte,
Freigabekarten für offene Aktionen (Details ansehen, freigeben, verwerfen), Statusanzeige der
eingerichteten Bereiche. Keine Schrift kleiner als 12 pt (16 px). Kein Build-Schritt, keine
externen Abhängigkeiten zur Laufzeit.
