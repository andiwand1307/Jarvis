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

Ergänzungen (abwärtskompatibel, vom Backend nachgetragen):
- `api/status` enthält zusätzlich `"api_key_configured": bool` (Claude-Schlüssel vorhanden).
- `api/chat` akzeptiert optional `"channel": "web"|"phone"|"event"` (Standard `web`).
- Fehler: unbekanntes Gespräch → 404, unbekannte/erledigte offene Aktion bei approve/reject → 404
  (`{"ok": false, ...}`), leere Nachricht → 400. Ist das Gespräch in `api/chat` unbekannt, kommen
  nur `error` und `done` (kein `start`).
- `api/log`-Einträge: `{id, created_at, conversation_id, tool, risk, input, result, status}`,
  neueste zuerst; `status` ∈ `ok`, `error`, `invalid`, `pending`, `approved`, `approved_failed`, `rejected`.
- Token-Modus (außerhalb des Add-ons mit `JARVIS_TOKEN`): `api/*` braucht `Authorization: Bearer
  <token>`; alternativ `?token=<token>` oder das Cookie `jarvis_token`. Wer die Oberfläche einmal mit
  `/?token=<token>` öffnet, bekommt das Cookie gesetzt, danach funktionieren die API-Aufrufe der
  Oberfläche ohne Änderung. `api/health` und die statischen Dateien sind ohne Token erreichbar.
- Ergebnisse freigegebener oder verworfener Aktionen erfährt das Modell beim nächsten Beitrag im
  selben Gespräch (Kontextblock); sie erscheinen nicht als eigene Nachricht in `messages`.

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

## 7. Konfiguration (Backend)

Umgebungsvariablen; im Add-on setzt `run.sh` sie aus `/data/options.json` (Optionsname in Klammern).

| Variable | Bedeutung | Standard |
|---|---|---|
| `ANTHROPIC_API_KEY` (`anthropic_api_key`) | Claude-Zugang | – |
| `JARVIS_MODEL` (`model`) | Modell | `claude-opus-5-5` |
| `JARVIS_EFFORT` (`effort`) | `low`…`max` | `medium` |
| `JARVIS_USER_NAME` (`user_name`) | Name des Nutzers für den Systemprompt | – |
| `JARVIS_WEB_SEARCH` (`web_search`) | Websuche an/aus | `true` |
| `JARVIS_MAX_TOKENS`, `JARVIS_MAX_STEPS` | Ausgabegrenze je Antwort, Werkzeugrunden je Anfrage | `32000`, `12` |
| `JARVIS_FALLBACKS` | Server-Fallback bei Ablehnung (`fallbacks: "default"`) | `true` |
| `JARVIS_TIMEZONE` | Zeitzone | `Europe/Berlin` |
| `JARVIS_DATA_DIR`, `JARVIS_WEB_DIR` | SQLite-Ordner, Oberfläche | `data` (Add-on `/data`), `jarvis/web` |
| `JARVIS_HOST`, `JARVIS_PORT` | Bindung (ohne Token immer 127.0.0.1) | `0.0.0.0`, `8099` |
| `JARVIS_TOKEN` | Bearer-Token außerhalb des Add-ons | – |
| `TODOIST_API_TOKEN` (`todoist_api_token`) | Todoist (API v1) | – |
| `MAIL_IMAP_HOST`/`_PORT`, `MAIL_SMTP_HOST`/`_PORT`, `MAIL_SMTP_SECURITY` (`ssl`/`starttls`), `MAIL_USER`, `MAIL_PASSWORD`, `MAIL_FROM`, `MAIL_DRAFTS_FOLDER` (`mail_*`) | Mail; Senden nur mit SMTP-Host | Ports 993/465 |
| `HA_URL`, `HA_TOKEN` | Home Assistant außerhalb des Add-ons (im Add-on `SUPERVISOR_TOKEN` automatisch) | – |

Start lokal: `cd jarvis && pip install -e .[test] && python -m jarvis_core`; Tests: `pytest`.
