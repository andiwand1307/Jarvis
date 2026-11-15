# Einrichtung Jarvis

Ehrlich vorweg: Der aktuelle Stand ist nicht gegen echte Dienste getestet. Alle Tests liefen gegen Attrappen. Anthropic, Todoist, dein Postfach und Home Assistant sind bisher nicht real angesprochen worden. Rechne beim ersten Start mit Fehlern und lies bei Problemen Abschnitt 6. Dieser Stand ist Etappe 1 (Kern und Web-Oberfläche). Telefonie ist noch nicht gebaut.

## 1. Was du vorher brauchst

1. **Anthropic-API-Schlüssel.** Lege ihn in der Anthropic Console an (ungefähr hier: Bereich „API Keys“). Auf dem Konto muss API-Guthaben vorhanden sein.
2. **Todoist-API-Token.** Öffne in Todoist die Einstellungen und dort „Integrationen“. Der Token steht im Bereich „Entwickler“ (ungefähr hier). Kopiere ihn von dort.
3. **App-Passwort für dein Postfach** sowie die Servernamen für IMAP (Posteingang) und SMTP (Postausgang).
   - Gmail: IMAP `imap.gmail.com`, Port 993. SMTP `smtp.gmail.com`, Port 465. Ein App-Passwort gibt es nur, wenn die Zwei-Faktor-Anmeldung eingeschaltet ist.
   - Andere Anbieter: Servernamen und Ports stehen in deren Hilfe.
   - Das normale Kennwort funktioniert in der Regel nicht. Nimm das App-Passwort.

Zugangsdaten trägst du nur in die Add-on-Konfiguration ein. Nicht in einen Chat, nicht in eine Datei im Repository.

Nur der Claude-Schlüssel ist Pflicht. Todoist und Mail sind optional. Ohne Zugangsdaten fehlt nur der jeweilige Bereich.

## 2. Add-on bereitstellen

Offener Punkt: Das Repository ist privat. Der Code liegt derzeit auf dem Branch `ccr-5d4d7d11-giqwjb`, nicht auf `main`. Home Assistant lädt den Standardzweig und kann private Repositories nicht ohne Weiteres laden. Es gibt zwei Wege.

- **Weg (a):** Den Branch nach `main` zusammenführen und das Repository öffentlich machen. Im Repository liegen keine Geheimnisse. Die Zugangsdaten stehen nur in der Add-on-Konfiguration auf dem Server.
- **Weg (b):** Den Ordner `jarvis/` als lokales Add-on auf den Home-Assistant-Server kopieren. Für den ersten Versuch empfehle ich diesen Weg.

Für Weg (b):

1. Nutze das vorhandene Add-on „Advanced SSH & Web Terminal“ oder den Samba-Zugriff auf die Freigaben.
2. Kopiere den Inhalt des Ordners `jarvis/` nach `/addons/jarvis/`. Dort muss `config.yaml` direkt liegen, nicht in einem zusätzlichen Unterordner.
3. Lade im Add-on Store die Liste neu (ungefähr hier: Menü mit drei Punkten oben rechts). Unter „Lokale Add-ons“ erscheint Jarvis.

Bei späteren Codeänderungen kopierst du den Ordner erneut und baust das Add-on neu.

Für Weg (a), sobald er umgesetzt ist: Einstellungen → Add-ons → Add-on Store → Menü → Repositories. Dort trägst du `https://github.com/andiwand1307/jarvis` ein und bestätigst mit „Hinzufügen“.

## 3. Add-on installieren und einrichten

Zieh vor dem ersten Einbau ein vollständiges Backup in Home Assistant (ungefähr hier: Einstellungen → System → Backups).

1. Öffne Jarvis unter „Lokale Add-ons“ (Weg b) oder im Store (Weg a) und wähle „Installieren“. Die Installation baut das Image auf dem Server und dauert eine Weile.
2. Öffne den Tab „Konfiguration“ und trage die Optionen ein (Tabelle unten).
3. Speichere. Wähle dann „Starten“. Die Option „Start beim Hochfahren“ ist vorgegeben eingeschaltet.
4. Schalte „In Seitenleiste anzeigen“ ein (ungefähr hier: Schalter im Add-on-Bereich).
5. Öffne Jarvis über die Seitenleiste. Der Zugriff läuft über deine Home-Assistant-Anmeldung. Ein eigenes Passwort gibt es dafür nicht.

| Option | Wofür | Pflicht |
|---|---|---|
| `anthropic_api_key` | Claude-Schlüssel aus der Anthropic Console | ja |
| `model` | Claude-Modell. Vorgabe `claude-opus-5-5` | nein |
| `effort` | Denkaufwand je Antwort: `low`, `medium`, `high`, `xhigh`, `max`. Vorgabe `medium` | nein |
| `user_name` | Name, mit dem Jarvis dich im Systemprompt führt | nein |
| `web_search` | Websuche an oder aus. Vorgabe an | nein |
| `todoist_api_token` | Todoist-Token. Ohne Eintrag fehlen die Aufgaben-Werkzeuge | nein |
| `mail_imap_host` | IMAP-Server zum Lesen und Suchen, z. B. `imap.gmail.com` | nein |
| `mail_imap_port` | IMAP-Port. Vorgabe 993 | nein |
| `mail_smtp_host` | SMTP-Server zum Senden. Leer heißt: Mail nur lesen, nicht senden | nein |
| `mail_smtp_port` | SMTP-Port. Vorgabe 465 | nein |
| `mail_smtp_security` | `ssl` (Port 465) oder `starttls`. Vorgabe `ssl` | nein |
| `mail_user` | Anmeldename, meist die Postfach-Adresse | nein |
| `mail_password` | App-Passwort, nicht das normale Kennwort | nein |
| `mail_from` | Absenderadresse. Leer heißt: Anmeldename wird verwendet | nein |
| `mail_drafts_folder` | Name des Entwurfsordners. Leer heißt: Jarvis sucht den Ordner selbst im Postfach | nein |
| `ha_trusted_entities` | Erlaubnisliste: Szenen, Skripte und Schalter, die Jarvis ohne Freigabe schalten darf, z. B. `scene.abend`. Vorgabe leer | nein |

Zum Mail-Lesen brauchst du `mail_imap_host`, `mail_user` und `mail_password` zusammen. Änderst du Optionen später, startest du das Add-on neu.

## 4. Erster Test

Halte diese Reihenfolge ein. Jeder Schritt baut auf dem vorigen auf.

1. **Statusanzeige prüfen.** Sie zeigt, welche Bereiche eingerichtet sind (ungefähr hier: Statusanzeige in der Oberfläche). Bereiche ohne Zugangsdaten stehen als nicht eingerichtet darin. Prüfe, ob das deinen Eingaben entspricht.
2. **Harmlose Frage stellen**, zum Beispiel: „Was kannst du, und welche Bereiche sind eingerichtet?“ Hier wird nur das Modell gefragt, kein Werkzeug läuft.
3. **Todoist-Aufgabe anlegen:** „Lege in Todoist eine Aufgabe mit dem Namen Jarvis-Test an.“ Die Aufgabe erscheint in Todoist. Die Stufe ist `write`, sie läuft also sofort. Lösche die Testaufgabe danach in Todoist.
4. **Mail suchen:** „Suche die letzten drei Mails von <Absender>.“ Du bekommst eine Liste mit Absender und Betreff. Die Stufe ist `read`.
5. **Mail-Entwurf anlegen:** „Schreibe einen Entwurf an mich selbst mit dem Betreff Jarvis-Test.“ Der Entwurf liegt danach im Entwurfsordner deines Postfachs. Die Stufe ist `write`.
6. **Zum Schluss eine Mail an dich selbst senden:** „Sende den Entwurf an meine eigene Adresse.“ Jarvis sendet nicht sofort. Es erscheint eine Freigabekarte. Öffne die Details, prüfe Empfänger und Text und wähle „Freigeben“. Erst danach geht die Mail raus. Kontrolliere den Posteingang.

## 5. Wie die Freigabe funktioniert

Jedes Werkzeug hat eine Risikostufe.

| Stufe | Bedeutung | Beispiele | Verhalten |
|---|---|---|---|
| `read` | liest nur | Mail suchen, Zustand lesen | läuft sofort |
| `write` | ändert eigene Daten, leicht rückholbar | Todoist-Aufgabe, Entwurf, Licht schalten | läuft sofort, wird protokolliert |
| `confirm` | wirkt nach außen oder ist heikel | Mail senden, Schloss, Alarm, Garagentor | wird als offene Aktion gespeichert und läuft erst nach Freigabe |

- Freigegeben oder verworfen wird nur über die Freigabekarte in der Oberfläche. Das Modell kann nicht selbst freigeben.
- Bei Home Assistant gilt eine Positivliste: Ohne Freigabe laufen nur die Standarddienste von Licht, Lüfter und Heizung sowie Wiedergabesteuerung und Lautstärke der Mediaplayer. Alles andere braucht deine Freigabe, auch Schalter, Szenen, Skripte, Rollläden, Alarmanlage und das Abspielen von Inhalten.
- Szenen, Skripte und Schalter, denen du vertraust, trägst du in `ha_trusted_entities` ein. Dann laufen sie ohne Freigabe.
- Meldet eine Freigabe „Ergebnis unbekannt“, sieh erst nach, ob die Aktion durchging, bevor du sie wiederholst.
- Inhalte aus Mails und Webseiten sind Daten, keine Anweisungen. Verlass dich trotzdem nicht darauf. Die Freigabe ist die eigentliche Absicherung.
- Jeder Werkzeugaufruf landet im Protokoll von Jarvis.

## 6. Wenn etwas nicht geht

1. Sieh dir das Add-on-Protokoll an (ungefähr hier: Einstellungen → Add-ons → Jarvis → Protokoll). Fehler stehen dort meist mit Hinweis auf die Ursache.
2. Prüfe die typischen Ursachen:
   - **Falsches Passwort fürs Postfach.** Du hast das normale Kennwort statt des App-Passworts eingetragen. Bei Gmail muss dafür die Zwei-Faktor-Anmeldung eingeschaltet sein.
   - **IMAP im Postfach nicht aktiviert.** Schalte den IMAP-Zugriff in den Konto-Einstellungen deines Anbieters ein (ungefähr hier: Weiterleitung und POP/IMAP-Bereich).
   - **Kein API-Guthaben bei Anthropic.** Anfragen schlagen dann fehl. Prüfe das Guthaben in der Anthropic Console.
   - **Falscher Todoist-Token.** Kopiere ihn neu aus den Todoist-Einstellungen.
3. Hast du Optionen geändert, starte das Add-on neu.

## 7. Kosten

Die Nutzung der Claude-API wird nach Verbrauch abgerechnet. Lange Gespräche, mehrere Werkzeugrunden und ein hoher Denkaufwand (`effort`) erhöhen den Verbrauch. Die aktuellen Preise stehen in der Anthropic Console (ungefähr hier: Bereich Abrechnung). Diese Anleitung nennt keine Preise.
