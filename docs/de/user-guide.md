# Benutzerhandbuch

[English](../en/user-guide.md) · [Русский](../ru/user-guide.md) · [Deutsch](user-guide.md)

Senden Sie dem Manager eine Direktnachricht: `!language de`, danach `!help`.
Verfügbar sind `en`, `ru`, `de` und `auto`. Manuelle Auswahl hat Vorrang vor dem
Mattermost-Profil. Auto nutzt das Profil, danach MM_DEFAULT_LANGUAGE. Profiländerungen
können bis zu fünf Minuten verzögert erscheinen; erneutes auto lädt sofort neu.
Die Zeitzone wird unabhängig von der Sprache eingestellt.

## Bots und Tokens

Erstellung: `!create example-bot --repo https://example.com/team/project --criticality 2`.
Der Antragsteller wird Eigentümer. Bei konfigurierten Genehmigern ist deren Prüfung nötig.
Benutzernamen enthalten kleine ASCII-Buchstaben, Ziffern, Punkte, Unterstriche und
Bindestriche; neue Namen müssen mit `-bot` enden. `!list` zeigt eigene Bots,
`!info example-bot` deren Details. Beschreibung ändern:
`!update example-bot --description "Dienstbenachrichtigungen"`.
Benutzername, Repository und Kritikalität erfordern eine Genehmigung per Direktnachricht.
Gemischte Änderungen warten vollständig. `!delete example-bot` deaktiviert den Bot.
Kritikalität: 1 kritisch, 2 wichtig, 3 ergänzend. Anzeigename: maximal 64 Unicode-Zeichen;
Beschreibung: maximal 1024.

`!import example-bot --repo https://example.com/team/project --criticality 2` erfordert
immer eine Genehmigung per Direktnachricht. Bestehende Server-Tokens bleiben unverändert.
`!token create example-bot` stellt ein Token per Direktnachricht aus; es bleibt im Verlauf.
Halten Sie es geheim. `!token list example-bot` zeigt erfasste Token-IDs;
`!token revoke <token_id>` widerruft ein Token. Weitere Server-Tokens können existieren.

Hängen Sie ein PNG/JPEG an `!icon set example-bot` an, um das Bild per Direktnachricht
zu ändern. Maximum: 5 MiB und 4096×4096, keine Animation.
`!icon reset example-bot` stellt das Standardbild wieder her; `!avatar` ist gleichwertig.
Nur der Eigentümer darf Bilder ändern.

## Rollen und Rückmeldungen

Genehmiger verwenden `!approve <request_id>` oder `!deny <request_id> [reason]` per
Direktnachricht; Selbstgenehmigung ist verboten. Katalogleser verwenden
`!catalog --format cards` oder `!catalog --format table` mit --owner, --criticality,
--search und --page. Privilegierte Hilfe ist nur für die jeweilige Rolle per DM sichtbar.

Wenn eingerichtet: `!feedback new <text>`, Liste `!feedback list`, Status
`!feedback status FB-1`, Verlauf `!feedback show FB-1`, Antwort
`!feedback reply FB-1 <text>`. Schließen: `!feedback close FB-1 <reason>`;
erneut öffnen: `!feedback reopen FB-1 <reason>`.
Text: 1–3000 Zeichen, keine Anhänge. Senden Sie keine Geheimnisse.
Bearbeiter verwenden `!feedback queue`, `!feedback take FB-1`, danach
`!feedback set-status FB-1 waiting <question>` oder `resolved <solution>`.
Alle Antworten sind für den Autor sichtbar; interne Notizen gibt es nicht.
