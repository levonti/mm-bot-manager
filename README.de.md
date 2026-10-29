# mm-bot-manager

[English](README.md) · [Русский](README.ru.md) · [Deutsch](README.de.md)

Ein Mattermost-Bot zur Verwaltung eigener Bots per Nachricht: Erstellung,
Attributänderungen, Import sowie Ausstellung und Widerruf von Tokens.
SQLite oder PostgreSQL speichert Eigentümer und Metadaten. Zugriff richtet sich
nach Eigentum und zugewiesenen Rollen.

- [Benutzerhandbuch](docs/de/user-guide.md)
- [Installation und Betrieb](docs/de/operations.md)
- [Entwicklung und Übersetzungen](docs/de/development.md)

Für den ersten Start benötigen Sie Mattermost, einen eigenen Manager-Bot und ein
persönliches Zugriffstoken eines Administrators. Setzen Sie `MM_DEFAULT_LANGUAGE=de`.
Senden Sie anschließend `!language de` und `!help` an den Bot. Die Einstellung wird
pro Benutzer gespeichert. `!language auto` übernimmt die Profilsprache aus Mattermost;
bei unbekannter Sprache gilt die konfigurierte Standardsprache. Fehlende
Übersetzungen werden durch Englisch ersetzt.

Befehle und Parameter sind in allen Sprachen identisch. Benutzerdaten werden nicht
übersetzt. Die Sprache bestimmt nicht die Zeitzone: setzen Sie `MM_DISPLAY_TIMEZONE`
separat. Vollständige Konfigurationsreferenz und historische Prüfberichte finden Sie
im [englischen Referenzhandbuch](README.md). Historische Berichte beschreiben frühere
Prüfungen und bestätigen nicht den aktuellen Zustand Ihrer Installation.
