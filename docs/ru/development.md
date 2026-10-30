# Разработка и переводы

[English](../en/development.md) · [Русский](../ru/development.md) · [Deutsch](../de/development.md)

Используйте Python 3.14 и виртуальное окружение uv. Команды, параметры, идентификаторы, JSON-ключи и пользовательские данные неизменны. EN — исходный язык. В DE используется Sie, в RU — нейтральное вежливое обращение.

Меняя сообщение, обновляйте все три JSON-каталога. Сохраняйте параметры шаблонов, спецификации формата, примеры команд и Markdown. Проверяйте числа 0, 1, 2, 5, 11, 21 и Unicode. Добавляйте интеграционные проверки поведения.

При изменении поведения обновляйте соответствующие руководства на трёх языках. Исторические отчёты и платформенные справочники сохраняют исходный язык; локальные руководства описывают основные операции. Смысл проверяется вручную: совпадение ключей не подтверждает точность перевода.

После смысловой проверки обновите отпечатки исходников указанной командой. Не обновляйте их только ради обхода ошибки. Каталоги, руководства и манифест коммитьте вместе. CI отклоняет устаревшие отпечатки, сломанные ссылки, пропуски ключей и несовместимые параметры.

Словарь: owner / владелец / Eigentümer; approver / согласующий / Genehmiger; request / заявка / Antrag; criticality / критичность / Kritikalität; feedback handler / обработчик обращений / Rückmeldungsbearbeiter.

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

Необязательная live-проверка: `uv run --no-sync python scripts/check_live_languages.py --output /private/tmp/mm-language-report.json`. Нужен Docker; создаётся отдельный локальный Mattermost с тремя настоящими пользователями. После проверки удаляются его контейнеры, тома и сеть. `.env` не загружается. Закреплённый amd64-образ может потребовать скачивания и эмуляции. Область проверки и ограничения описаны в [отчёте](../trilingual-live-acceptance-2026-10-30.md).
