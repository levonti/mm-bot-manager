# Kubernetes Deployment Design

## Цель

Добавить поддерживаемый способ развёртывания `mm-bot-manager` в Kubernetes
версии 1.21.1 и выше. Решение должно работать как в обычном окружении, так и
в закрытом контуре, где образы доступны только из локального Harbor.

Основной артефакт поставки — самостоятельный Helm chart без внешних
chart-зависимостей. Он поддерживает внешний PostgreSQL и одиночный PostgreSQL
внутри кластера. Ingress для приложения не создаётся, поскольку приложение не
принимает пользовательский HTTP-трафик и само устанавливает исходящие HTTPS и
WebSocket-соединения с Mattermost.

## Ограничения и допущения

- Минимальная версия Kubernetes: 1.21.1.
- На машине, с которой выполняется установка, доступен Helm 3.
- PostgreSQL-операторы, Argo CD, Flux и External Secrets Operator отсутствуют.
- В кластере доступны StorageClass `longhorn` и `local-path`.
- Для встроенной БД достаточно одного PostgreSQL `StatefulSet` с PVC.
- Приложение запускается в одной реплике: несколько одновременно активных
  WebSocket-процессов могут повторно обработать одно событие Mattermost.
- Секреты создаёт оператор до установки Helm release.
- Доверие к TLS-сертификату Harbor настраивается в container runtime узлов и
  находится вне зоны ответственности chart.

## Рассмотренные варианты

| Вариант | Оценка | Обоснование |
| --- | ---: | --- |
| Helm chart приложения и внешний PostgreSQL | 9.6/10 | Наименьшая область ответственности release, независимый жизненный цикл БД и лучший production-профиль |
| Самодостаточный Helm chart, PostgreSQL StatefulSet и Longhorn | 9.1/10 | Предсказуемая air-gap установка без операторов и внешних chart-зависимостей |
| Два Helm release: приложение и vendored PostgreSQL chart | 8.4/10 | Независимые обновления, но сложнее поставка, настройка и проверка совместимости |
| SQLite на PVC | 6.4/10 | Простая установка, но слабее восстановление после отказов и отсутствует путь к нескольким репликам |

Выбран один собственный Helm chart с двумя режимами:
`database.mode=external` и `database.mode=internal`. Это покрывает два
предпочтительных варианта без дублирования шаблонов и документации.

## Структура поставки

В репозитории создаётся каталог `deploy/helm/mm-bot-manager` со следующими
обязанностями:

- `Chart.yaml` объявляет chart и ограничение `kubeVersion: ">=1.21.1-0"`;
- `values.yaml` содержит безопасные значения по умолчанию без секретов;
- `values.schema.json` проверяет режим БД, обязательные имена Secret,
  параметры образов, PVC и резервного копирования;
- `templates/` содержит именованные helpers и Kubernetes-ресурсы;
- `examples/` содержит профили values и незаполненные шаблоны env-файлов.

Отдельная документация описывает подготовку образов и секретов, установку,
проверку, резервное копирование, восстановление, обновление, rollback и
удаление.

## Режим внешнего PostgreSQL

При `database.mode=external` chart создаёт:

- `ServiceAccount` без автоматического монтирования API-токена;
- `ConfigMap` с несекретной конфигурацией приложения;
- `Deployment` приложения с одной репликой и стратегией `Recreate`.

Chart не создаёт PostgreSQL `StatefulSet`, PostgreSQL `Service`, data PVC,
backup PVC или backup `CronJob`. Приложение получает полный `DB_URL` из
заранее созданного Secret.

Резервное копирование, восстановление и доступность внешней БД остаются
ответственностью её оператора.

## Режим встроенного PostgreSQL

При `database.mode=internal` chart дополнительно создаёт:

- внутренний `ClusterIP Service` для PostgreSQL;
- PostgreSQL `StatefulSet` с одной репликой;
- PVC для каталога данных PostgreSQL;
- отдельный PVC для логических резервных копий;
- `CronJob`, выполняющий и проверяющий `pg_dump`.

Приложение получает адрес внутреннего Service из Helm-шаблона, а реквизиты
БД — из существующего Secret. PostgreSQL получает те же согласованные
реквизиты через `secretKeyRef`.

Чтобы пароль не приходилось встраивать в URL, конфигурация приложения
расширяется переменными `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER` и
`DB_PASSWORD`. При наличии `DB_URL` он сохраняет приоритет для обратной
совместимости. Если `DB_URL` отсутствует, но задан `DB_HOST`, приложение
требует остальные компонентные параметры и передаёт их драйверу PostgreSQL
отдельными аргументами. Если не заданы ни `DB_URL`, ни `DB_HOST`, сохраняется
существующий SQLite fallback через `DB_PATH`.

В internal-режиме chart задаёт `DB_HOST` именем внутреннего Service,
`DB_PORT=5432`, а `POSTGRES_USER`, `POSTGRES_PASSWORD` и `POSTGRES_DB`
отображает из Secret соответственно в `DB_USER`, `DB_PASSWORD` и `DB_NAME`.

`longhorn` является рекомендуемым StorageClass для production. `local-path`
поддерживается для тестовых и single-node кластеров. Документация явно
предупреждает, что потеря узла с `local-path` приводит к потере локального
тома.

## Контракт секретов

Chart никогда не принимает токены или пароли непосредственно через
`values.yaml` и не генерирует пароль PostgreSQL.

### Application Secret

Имя задаётся через `app.existingSecret`. Обязательные ключи:

- `MM_TOKEN`;
- `MM_BOT_TOKEN`.

### External database Secret

Для `database.mode=external` имя задаётся через
`database.external.existingSecret`. Обязательный ключ:

- `DB_URL`.

### Internal database Secret

Для `database.mode=internal` имя задаётся через
`database.internal.auth.existingSecret`. Обязательные ключи:

- `POSTGRES_USER`;
- `POSTGRES_PASSWORD`;
- `POSTGRES_DB`.

Chart отображает эти ключи в переменные окружения приложения и PostgreSQL
через явные `secretKeyRef`. Секретные значения не попадают в ConfigMap,
Deployment annotations, Helm notes или отрендерированные Helm values.

### Harbor pull Secret

Список имён registry Secret задаётся через `imagePullSecrets`. Документация
показывает создание Secret типа `kubernetes.io/dockerconfigjson` командой
`kubectl create secret docker-registry`.

Примеры используют локальные env-файлы с правами `0600` и
`kubectl create secret generic --from-env-file`. Заполненные env-файлы нельзя
коммитить. Документация также показывает проверку наличия ключей и ротацию
каждого Secret.

## Несекретная конфигурация

Через values настраиваются:

- URL Mattermost;
- список approver usernames;
- проверка TLS Mattermost;
- уровень логирования;
- образ приложения: registry/repository, tag или digest и pull policy;
- образ PostgreSQL: registry/repository, tag или digest и pull policy;
- `imagePullSecrets`;
- requests и limits;
- StorageClass и размер каждого PVC;
- cron-расписание, срок хранения и ресурсы backup job;
- pod и container security contexts;
- probe thresholds и termination grace period.

Digest, если указан, имеет приоритет над tag. Это позволяет воспроизводимо
устанавливать предварительно проверенные образы из Harbor.

## Сетевой поток

Приложение устанавливает:

- исходящее HTTPS-соединение с Mattermost REST API;
- исходящее WebSocket-соединение с Mattermost;
- соединение с внешним или внутренним PostgreSQL.

Входящий порт приложения используется только kubelet для health probes.
Chart не создаёт `Service` или `Ingress` для приложения. Наличие
ingress-nginx в кластере не влияет на установку.

В режиме внутренней БД PostgreSQL Service доступен только внутри кластера и
не получает Ingress, NodePort или LoadBalancer.

## Health endpoint и жизненный цикл

Приложение получает минимальный внутренний HTTP health server:

- `GET /health/live` подтверждает, что event loop и текущая DB connection
  способны обслуживать запросы;
- `GET /health/ready` подтверждает завершение инициализации и успешный
  `SELECT 1` через текущую DB connection;
- остальные пути отвечают `404`;
- ответы не содержат конфигурацию, DSN, токены или внутренние исключения.

Порт health server не публикуется за пределы Pod. Deployment использует
`startupProbe`, `readinessProbe` и `livenessProbe`. Порог и период
`livenessProbe` дают БД время на кратковременное восстановление; после
последовательных ошибок Kubernetes перезапускает приложение и тем самым
создаёт новую DB connection. Временные разрывы WebSocket не делают процесс
нездоровым: существующий WebSocket-клиент переподключается самостоятельно.

Приложение использует одну реплику и стратегию `Recreate`. При завершении Pod
получает достаточный grace period для остановки asyncio loop и закрытия
клиентов. Ошибка конфигурации или недоступная при старте БД приводит к
завершению процесса; Kubernetes повторяет запуск с обычным backoff.

PostgreSQL использует `pg_isready` в startup, readiness и liveness probes.

## Security baseline

Контейнер приложения:

- запускается от UID/GID 1000;
- требует `runAsNonRoot`;
- использует read-only root filesystem;
- монтирует временный `emptyDir` в `/tmp`;
- запрещает privilege escalation;
- удаляет все Linux capabilities;
- использует `seccompProfile: RuntimeDefault`.

ServiceAccount приложения устанавливает
`automountServiceAccountToken: false`, поскольку приложению не нужен
Kubernetes API.

Security context PostgreSQL остаётся настраиваемым, чтобы chart работал с
разными одобренными зеркальными образами. Безопасные параметры, не
конфликтующие с инициализацией volume, включаются по умолчанию. Сетевая
публикация PostgreSQL за пределы кластера отсутствует.

## Резервное копирование

В режиме встроенной БД резервное копирование включено по умолчанию.
`CronJob` использует тот же образ PostgreSQL, что и `StatefulSet`, поэтому
закрытому контуру нужны только образы приложения и PostgreSQL.

Каждый запуск:

1. создаёт временный архив `pg_dump --format=custom` на backup PVC;
2. проверяет архив командой `pg_restore --list`;
3. атомарно переименовывает успешно проверенный архив;
4. удаляет архивы старше настроенного срока хранения.

`concurrencyPolicy: Forbid` исключает пересечение запусков. Настраиваются
schedule, retention days, requests/limits и история успешных и неуспешных
Job.

Backup PVC сохраняется после удаления release. PVC с данными StatefulSet
также не удаляется автоматически. Для окончательной очистки оператор
выполняет отдельную явно документированную команду.

Второй PVC не является полной disaster-recovery стратегией. Для Longhorn
рекомендуется отдельно настроить экспорт снапшотов в S3/NFS-хранилище. При
использовании `local-path` копирование backup за пределы узла обязательно для
защиты от его потери.

## Восстановление

Документация задаёт последовательность:

1. остановить Deployment приложения;
2. выбрать и проверить архив через `pg_restore --list`;
3. восстановить архив в очищенную целевую БД;
4. выполнить `SELECT 1` и проверить наличие таблиц приложения;
5. запустить Deployment;
6. проверить readiness и логи.

Процедура не выполняется автоматически Helm hook, чтобы обновление release
никогда не изменяло данные БД без явного действия оператора.

## Закрытый контур и Harbor

Chart не содержит внешних Helm dependencies. Установка из `.tgz` не требует
доступа к Helm-репозиториям.

Все workload-образы имеют полностью переопределяемые repository, tag и
digest. Профиль `values-harbor.yaml` показывает ссылки только на локальный
Harbor и подключение `imagePullSecrets`.

Документация содержит:

- сборку образа приложения;
- присвоение тега и публикацию в Harbor;
- зеркалирование PostgreSQL-образа;
- перечень образов для каждого режима БД;
- перенос образов архивом при отсутствии сетевого пути между контурами;
- проверку pull из namespace установки;
- настройку доверия к CA Harbor на узлах как внешний prerequisite.

## Совместимость Kubernetes

Chart использует только API, доступные в Kubernetes 1.21.1:

- `apps/v1`;
- `batch/v1`;
- `v1`.

Шаблоны не используют поля, появившиеся после 1.21.1. В `Chart.yaml`
указывается минимальная версия Kubernetes.

## Проверка и критерии приёмки

### Приложение

- unit-тесты подтверждают ответы health endpoint `200`, `503` и `404`;
- health endpoint не раскрывает текст исключений и секретные параметры;
- существующий набор Python-тестов проходит;
- Ruff formatting/lint и строгий mypy проходят.

### Helm chart

- `helm lint` проходит;
- chart рендерится через `helm template --kube-version 1.21.1` для external,
  internal-longhorn, internal-local-path и Harbor профилей;
- результат проверяется строгими схемами Kubernetes 1.21.1;
- external-профиль не содержит ресурсов встроенного PostgreSQL;
- internal-профиль содержит StatefulSet, внутренний Service, data PVC,
  backup PVC и CronJob;
- переключение StorageClass не требует изменения шаблонов;
- Harbor-профиль не содержит ссылок на публичные registry;
- ни один отрендерированный ресурс не содержит значений токенов или паролей;
- для образов можно использовать digest вместо tag;
- повторный `helm upgrade` не меняет Secret и не удаляет PVC.

### Операторская документация

Документация содержит проверяемые команды для:

1. сборки и публикации образа;
2. создания namespace и всех трёх типов Secret;
3. установки с `helm upgrade --install --atomic --wait`;
4. проверки Pod, probes и логов;
5. ручного запуска и проверки backup;
6. восстановления;
7. обновления и rollback;
8. безопасного удаления release и отдельного удаления сохранённых PVC.

## Не входит в объём

- PostgreSQL HA или автоматический failover;
- установка PostgreSQL-оператора;
- установка ingress-nginx;
- публикация health endpoint через Ingress;
- установка и конфигурация Harbor;
- настройка доверенного CA Harbor на узлах;
- установка Longhorn или local-path provisioner;
- автоматическая настройка Longhorn backup target;
- GitOps-контроллеры;
- автоматическое создание или ротация секретов;
- горизонтальное масштабирование приложения.
