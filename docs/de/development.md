# Entwicklung und Übersetzungen

[English](../en/development.md) · [Русский](../ru/development.md) · [Deutsch](../de/development.md)

Verwenden Sie Python 3.14 und eine uv-Umgebung. Befehle, Parameter, IDs, JSON-Schlüssel und Benutzerdaten bleiben unverändert. Englisch ist die Ausgangssprache. Deutsch verwendet Sie, Russisch eine neutrale höfliche Ansprache.

Bei Nachrichtenänderungen alle drei JSON-Kataloge aktualisieren. Platzhalter, Formatspezifikationen, Befehlsbeispiele und Markdown erhalten. Zahlen 0, 1, 2, 5, 11, 21 und Unicode prüfen. Integrationstests für das Verhalten ergänzen.

Bei Verhaltensänderungen die entsprechenden Anleitungen in allen drei Sprachen aktualisieren. Historische Berichte und Plattformreferenzen behalten ihre Ausgangssprache; übersetzte Anleitungen erklären die wichtigsten Abläufe. Bedeutung manuell prüfen: gleiche Schlüssel beweisen keine Übersetzungsqualität.

Nach inhaltlicher Prüfung die Quellfingerabdrücke mit dem folgenden Befehl aktualisieren. Nicht bloß zur Umgehung eines Fehlers aktualisieren. Kataloge, Anleitungen und Manifest gemeinsam committen. CI erkennt veraltete Fingerabdrücke, defekte Links, fehlende Schlüssel und inkompatible Platzhalter.

Glossar: owner / владелец / Eigentümer; approver / согласующий / Genehmiger; request / заявка / Antrag; criticality / критичность / Kritikalität; feedback handler / обработчик обращений / Rückmeldungsbearbeiter.

```sh
uv venv .venv
uv sync --frozen --extra postgres
uv run --no-sync python scripts/check_translations.py
uv run --no-sync ruff check src tests scripts/check_translations.py
uv run --no-sync mypy src
uv run --no-sync pytest -q
helm lint deploy/helm/mm-bot-manager
```

```sh
uv run --no-sync python scripts/check_translations.py --refresh
```

Optionaler Live-Test: `uv run --no-sync python scripts/check_live_languages.py --output /private/tmp/mm-language-report.json`. Benötigt Docker; erstellt einen isolierten lokalen Mattermost mit drei echten Benutzern. Container, Volumes und Netzwerk werden danach entfernt. `.env` wird nie geladen. Das festgelegte amd64-Image kann Download und Emulation erfordern. Umfang und Grenzen stehen im [Prüfbericht](../trilingual-live-acceptance-2026-10-30.md).
