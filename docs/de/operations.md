# Installation und Betrieb

[English](../en/operations.md) · [Русский](../ru/operations.md) · [Deutsch](operations.md)

## Mattermost vorbereiten

Aktivieren Sie Bot Account Creation und Personal Access Tokens in System Console.
Erstellen Sie ein persönliches Token eines normalen system_admin-Benutzers: MM_TOKEN.
Ein Bot-Token kann keine anderen Bots erstellen. Erstellen Sie einen eigenen Manager-Bot
und speichern Sie dessen Token als MM_BOT_TOKEN. Fügen Sie den Manager für Erwähnungen
zu Teams und Kanälen hinzu. Direktnachrichten brauchen keine Kanalkonfiguration.

## Konfiguration und Start

Kopieren Sie `.env.example` nach `.env`, beschränken Sie die Dateirechte und setzen Sie
MM_URL, MM_TOKEN, MM_BOT_TOKEN. Keine Zugangsdaten committen. Setzen Sie
MM_DEFAULT_LANGUAGE=de (en/ru/de), MM_DISPLAY_TIMEZONE=UTC oder eine IANA-Zeitzone.
Behalten Sie MM_VERIFY_SSL=true mit vertrauenswürdiger CA bei.
MM_APPROVERS, MM_CATALOG_VIEWERS und MM_FEEDBACK_HANDLERS enthalten kommagetrennte
Benutzernamen für unabhängige Rollen. Leere Bearbeiterliste deaktiviert Rückmeldungen.
Nach Konfigurationsänderungen neu starten.

Datenbankpriorität: DB_URL, vollständige DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD,
danach DB_PATH (SQLite). Daten dauerhaft speichern. Für Compose einen festgelegten
MM_BOT_MANAGER_IMAGE konfigurieren, `docker compose up -d` ausführen und
`docker compose logs` prüfen. Der Container läuft ohne root mit schreibgeschütztem
Dateisystem; behalten Sie diese Einstellungen bei.
Lokal: Python 3.14, `uv venv .venv`, `uv sync --frozen --extra postgres`, danach
`uv run --no-sync mm-bot-manager --language de`.

Für Kubernetes mattermost.defaultLanguage=en/ru/de im Standort-Overlay setzen.
Zugangsdaten durch vorhandene Secrets oder External Secrets Operator bereitstellen.
Datenbank, Speicher und kube-context vorab auswählen. Befehle und Standortparameter:
[Kubernetes-Anleitung](../kubernetes.md), [Helm-Referenz](../../deploy/helm/mm-bot-manager/README.md).
Das Bereitstellungsskript prüft die gespeicherte Standortauswahl vor der Installation.
Diese plattformspezifischen Referenzen werden auf Englisch gepflegt.

## Aktualisierung, Sicherung und Wiederherstellung

Alten Manager vor der Aktualisierung stoppen. Nie zwei Manager mit derselben Datenbank
betreiben. PostgreSQL mit pg_dump sichern, mit pg_restore --list und Wiederherstellung
in eine isolierte Datenbank prüfen. Für SQLite den Manager stoppen und die Backup-API
oder `.backup` verwenden; keine aktive Datenbank ohne WAL kopieren. Sicherungen privat
halten. Nach Wiederherstellung Eigentümer, Anträge, Einstellungen und Tickets prüfen.
Für Kubernetes-PostgreSQL scripts/kubernetes-db.sh mit dem Prüfverfahren der Anleitung
verwenden. Wiederherstellung überschreibt neuere Änderungen; zuerst isoliert testen.

Migration 3 erweitert die Sprachauswahl und erhält Sprache sowie Katalogformat.
Sie läuft beim Start transaktional. Vor Rückkehr zu einem älteren Anwendungsimage den
Manager stoppen und in derselben Datenbank ausführen:
`UPDATE user_preference SET language = 'auto' WHERE language = 'de';`.
Erweiterte Einschränkung und Migrationseintrag behalten; Schema nicht zurückstufen.
Alte Sicherungen verlieren neuere Schreibvorgänge. Vor Änderungen sichern.

## Diagnose

Prüfen Sie `!version`, Startprotokoll und konfigurierten lokalen Gesundheitsendpunkt.
403 bei Bot-Erstellung deutet oft auf einen falschen MM_TOKEN-Kontotyp oder deaktivierte
Bot-Erstellung hin. Bei API-Fehlern Diagnose-ID behalten, keine Tokens weitergeben.
Bei ungewissem Schreibergebnis Befehle nicht blind wiederholen. Manager stoppen und zuerst
`uv run --no-sync python -m mm_bot_manager.recovery --language de --help` lesen.
Administrative Hilfe funktioniert ohne Server-Zugangsdaten. Auch --language en/ru möglich.
Sprachwechsel ändert keine Berechtigungen und übersetzt keine Benutzerdaten.
