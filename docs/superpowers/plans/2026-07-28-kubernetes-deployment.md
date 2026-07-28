# Kubernetes Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a self-contained Helm deployment for Kubernetes 1.21.1+ with external and single-instance in-cluster PostgreSQL modes, air-gapped Harbor support, health probes, persistent backups, restore tooling, and operator documentation.

**Architecture:** Extend the Python process with component-based PostgreSQL configuration and a Pod-local health server, then package the process in one dependency-free Helm chart. The chart always deploys one application `Deployment`; internal database mode conditionally adds PostgreSQL `StatefulSet`, Service, retained data/backup PVCs, backup `CronJob`, and an explicitly rendered restore `Job`.

**Tech Stack:** Python 3.14, asyncio, Pydantic Settings, psycopg 3, Helm 3, Kubernetes `v1`/`apps/v1`/`batch/v1`, PostgreSQL 15.18, pytest, PyYAML, kubeconform.

## Global Constraints

- Minimum Kubernetes version is exactly 1.21.1.
- Deployment must work with Helm 3 and must not require PostgreSQL operators, Argo CD, Flux, or External Secrets Operator.
- The Helm chart must have no external chart dependencies and must install without internet access.
- Both `database.mode=external` and `database.mode=internal` are supported.
- Internal PostgreSQL is exactly one `StatefulSet` replica with a PVC and logical backup strategy.
- The application is exactly one replica with `Recreate` strategy.
- `longhorn` is the recommended production StorageClass; `local-path` remains supported for single-node and test installations.
- Secrets are always pre-created Kubernetes Secrets; passwords and tokens never appear in Helm values.
- Every workload image supports an arbitrary Harbor repository and either tag or digest.
- No application Service or Ingress is created; ingress-nginx is not used.
- Preserve the user's existing uncommitted changes in `.env.example`, `CLAUDE.md`, `README.md`, `src/mm_bot_manager/`, and `tests/`; stage only files belonging to the current task at each commit.
- When a task touches a file that was already dirty, use patch-mode staging and inspect
  `git diff --cached` so unrelated pre-existing hunks remain unstaged.
- Official compatibility references: [Kubernetes CronJob API migration](https://kubernetes.io/docs/reference/using-api/deprecation-guide/), [Kubernetes probes](https://kubernetes.io/docs/concepts/workloads/pods/probes/), [Helm chart structure and kubeVersion](https://helm.sh/docs/topics/charts/), and [official PostgreSQL image variables](https://github.com/docker-library/docs/blob/master/postgres/README.md).

---

## File Structure

### Application changes

- `src/mm_bot_manager/config.py` — validates URL-based and component-based database configuration and health bind settings.
- `src/mm_bot_manager/db.py` — opens PostgreSQL from a DSN or keyword parameters and exposes a non-throwing `ping()`.
- `src/mm_bot_manager/health.py` — serves Pod-local liveness/readiness HTTP requests without a new runtime dependency.
- `src/mm_bot_manager/__main__.py` — selects the database mode, starts/stops the health server, handles SIGTERM, and closes resources.
- `tests/test_config.py` — database configuration validation and precedence.
- `tests/test_main.py` — connection selection tests.
- `tests/test_health.py` — real TCP tests for `200`, `503`, and `404` health responses.
- `Dockerfile` — declares the health port and makes runtime bytecode behavior compatible with a read-only root filesystem.

### Helm chart

- `deploy/helm/mm-bot-manager/Chart.yaml` — chart metadata and Kubernetes floor.
- `deploy/helm/mm-bot-manager/values.yaml` — complete non-secret configuration.
- `deploy/helm/mm-bot-manager/values.schema.json` — conditional JSON Schema validation.
- `deploy/helm/mm-bot-manager/templates/_helpers.tpl` — names, labels, and tag/digest image helpers.
- `deploy/helm/mm-bot-manager/templates/serviceaccount.yaml` — token-less ServiceAccount.
- `deploy/helm/mm-bot-manager/templates/configmap.yaml` — Mattermost, logging, and health settings.
- `deploy/helm/mm-bot-manager/templates/deployment.yaml` — single application Pod with probes and secret references.
- `deploy/helm/mm-bot-manager/templates/postgresql-service.yaml` — internal-only headless Service.
- `deploy/helm/mm-bot-manager/templates/postgresql-statefulset.yaml` — internal-only PostgreSQL and data claim template.
- `deploy/helm/mm-bot-manager/templates/backup-pvc.yaml` — retained logical-backup volume.
- `deploy/helm/mm-bot-manager/templates/backup-cronjob.yaml` — validated custom-format dumps and retention.
- `deploy/helm/mm-bot-manager/templates/restore-job.yaml` — opt-in, manually rendered restore job.
- `deploy/helm/mm-bot-manager/templates/NOTES.txt` — safe post-install checks without secret output.
- `deploy/helm/mm-bot-manager/examples/values-external.yaml` — external PostgreSQL profile.
- `deploy/helm/mm-bot-manager/examples/values-internal-longhorn.yaml` — production internal profile.
- `deploy/helm/mm-bot-manager/examples/values-internal-local-path.yaml` — single-node profile.
- `deploy/helm/mm-bot-manager/examples/values-harbor.yaml` — closed-contour image profile.
- `deploy/helm/mm-bot-manager/examples/app-secret.env.example` — application Secret keys.
- `deploy/helm/mm-bot-manager/examples/external-db-secret.env.example` — `DB_URL` Secret key.
- `deploy/helm/mm-bot-manager/examples/internal-db-secret.env.example` — PostgreSQL Secret keys.

### Verification and documentation

- `tests/test_helm_chart.py` — semantic assertions against rendered manifests.
- `scripts/verify-kubernetes.sh` — Helm lint/template plus Kubernetes 1.21.1 schema validation.
- `docs/kubernetes.md` — complete operator runbook.
- `deploy/helm/mm-bot-manager/README.md` — chart-local quick reference.
- `README.md` — links to the Kubernetes runbook.
- `.gitignore` — ignores copied secret env files.
- `.dockerignore` — excludes secrets, VCS data, tests, caches, and local runtime files from the image build context.
- `pyproject.toml` and `uv.lock` — add PyYAML for manifest assertions.

---

### Task 1: Component-based PostgreSQL configuration

**Files:**
- Create: `tests/test_config.py`
- Create: `tests/test_main.py`
- Modify: `src/mm_bot_manager/config.py:1-59`
- Modify: `src/mm_bot_manager/db.py:49-90`
- Modify: `src/mm_bot_manager/__main__.py:4-40`

**Interfaces:**
- Consumes: existing `Settings`, `DbConn`, `connect()`, and `connect_postgres(dsn)`.
- Produces: `Settings.db_host`, `db_port`, `db_name`, `db_user`, `db_password`; `connect_postgres(dsn=None, *, host, port, dbname, user, password)`; `_connect_database(settings) -> DbConn`; `DbConn.ping() -> bool`.

- [ ] **Step 1: Add failing Settings tests**

Create `tests/test_config.py`:

```python
import pytest
from pydantic import ValidationError

from mm_bot_manager.config import Settings


BASE = {
    "mm_url": "https://mm.example.com",
    "mm_token": "admin-token",
    "mm_bot_token": "bot-token",
}


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **BASE, **overrides)  # type: ignore[call-arg]


def test_component_postgres_settings_are_accepted() -> None:
    settings = make_settings(
        db_host="postgresql",
        db_port=5433,
        db_name="mm_bot_manager",
        db_user="mm_bot",
        db_password="secret",
    )
    assert settings.db_host == "postgresql"
    assert settings.db_port == 5433


@pytest.mark.parametrize("missing", ["db_name", "db_user", "db_password"])
def test_component_postgres_settings_must_be_complete(missing: str) -> None:
    values: dict[str, object] = {
        "db_host": "postgresql",
        "db_name": "mm_bot_manager",
        "db_user": "mm_bot",
        "db_password": "secret",
    }
    values.pop(missing)
    with pytest.raises(ValidationError, match="DB_HOST requires"):
        make_settings(**values)


def test_db_url_takes_precedence_over_partial_component_settings() -> None:
    settings = make_settings(
        db_url="postgresql://user:pass@external/database",
        db_host="ignored",
    )
    assert settings.db_url == "postgresql://user:pass@external/database"
```

- [ ] **Step 2: Add failing database-selection tests**

Create `tests/test_main.py`:

```python
from unittest.mock import Mock

from mm_bot_manager.__main__ import _connect_database
from mm_bot_manager.config import Settings
from mm_bot_manager.db import DbConn


BASE = {
    "mm_url": "https://mm.example.com",
    "mm_token": "admin-token",
    "mm_bot_token": "bot-token",
}


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **BASE, **overrides)  # type: ignore[call-arg]


def test_connect_database_prefers_db_url(monkeypatch) -> None:
    expected = Mock(spec=DbConn)
    connect_postgres = Mock(return_value=expected)
    monkeypatch.setattr("mm_bot_manager.__main__.connect_postgres", connect_postgres)

    result = _connect_database(
        make_settings(
            db_url="postgresql://user:pass@external/database",
            db_host="ignored",
        )
    )

    assert result is expected
    connect_postgres.assert_called_once_with("postgresql://user:pass@external/database")


def test_connect_database_uses_component_parameters(monkeypatch) -> None:
    expected = Mock(spec=DbConn)
    connect_postgres = Mock(return_value=expected)
    monkeypatch.setattr("mm_bot_manager.__main__.connect_postgres", connect_postgres)

    result = _connect_database(
        make_settings(
            db_host="postgresql",
            db_port=5432,
            db_name="mm_bot_manager",
            db_user="mm_bot",
            db_password="p@ss:/word",
        )
    )

    assert result is expected
    connect_postgres.assert_called_once_with(
        None,
        host="postgresql",
        port=5432,
        dbname="mm_bot_manager",
        user="mm_bot",
        password="p@ss:/word",
    )


def test_connect_database_falls_back_to_sqlite(monkeypatch) -> None:
    expected = Mock(spec=DbConn)
    connect_sqlite = Mock(return_value=expected)
    monkeypatch.setattr("mm_bot_manager.__main__.connect", connect_sqlite)

    result = _connect_database(make_settings(db_path="/tmp/mm-bot.sqlite3"))

    assert result is expected
    connect_sqlite.assert_called_once_with("/tmp/mm-bot.sqlite3")
```

- [ ] **Step 3: Run the focused tests and verify failure**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider tests/test_config.py tests/test_main.py -q
```

Expected: collection or assertion failure because the component fields and `_connect_database` do not exist.

- [ ] **Step 4: Implement validation and connection selection**

In `src/mm_bot_manager/config.py`, import `model_validator` and add:

```python
from typing import Self

from pydantic import Field, HttpUrl, model_validator

# Inside Settings:
db_host: str | None = Field(default=None)
db_port: int = Field(default=5432, ge=1, le=65535)
db_name: str | None = Field(default=None)
db_user: str | None = Field(default=None)
db_password: str | None = Field(default=None)

@model_validator(mode="after")
def validate_database_settings(self) -> Self:
    if self.db_url is not None:
        return self
    if self.db_host is None:
        return self
    missing = [
        env_name
        for env_name, value in (
            ("DB_NAME", self.db_name),
            ("DB_USER", self.db_user),
            ("DB_PASSWORD", self.db_password),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"DB_HOST requires {', '.join(missing)}")
    return self
```

Change `connect_postgres` in `src/mm_bot_manager/db.py` to:

```python
def connect_postgres(
    dsn: str | None = None,
    *,
    host: str | None = None,
    port: int = 5432,
    dbname: str | None = None,
    user: str | None = None,
    password: str | None = None,
) -> DbConn:
    """Connect to PostgreSQL 15+ by DSN or discrete parameters."""
    import psycopg  # type: ignore[import-not-found, unused-ignore]
    from psycopg.rows import dict_row  # type: ignore[import-not-found, unused-ignore]

    if dsn is not None:
        raw = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
    else:
        if not all((host, dbname, user, password)):
            raise ValueError("host, dbname, user and password are required without a DSN")
        raw = psycopg.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
            row_factory=dict_row,
            autocommit=True,
        )
    return DbConn(raw, dialect="postgres")
```

Add `_connect_database` to `src/mm_bot_manager/__main__.py` and use it in `main_async`:

```python
from .config import Settings, load_settings


def _connect_database(settings: Settings) -> DbConn:
    if settings.db_url:
        return connect_postgres(settings.db_url)
    if settings.db_host:
        return connect_postgres(
            None,
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
        )
    return connect(settings.db_path)
```

- [ ] **Step 5: Run focused and static checks**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider tests/test_config.py tests/test_main.py -q
PYTHONDONTWRITEBYTECODE=1 uv run ruff check src/mm_bot_manager/config.py src/mm_bot_manager/db.py src/mm_bot_manager/__main__.py tests/test_config.py tests/test_main.py
PYTHONDONTWRITEBYTECODE=1 uv run mypy src
```

Expected: all commands pass.

- [ ] **Step 6: Commit only Task 1 files**

```bash
git add tests/test_config.py tests/test_main.py
git add -p src/mm_bot_manager/config.py src/mm_bot_manager/db.py src/mm_bot_manager/__main__.py
git diff --cached
git commit -m "feat: support component PostgreSQL settings"
```

---

### Task 2: Pod-local health server and graceful lifecycle

**Files:**
- Create: `src/mm_bot_manager/health.py`
- Create: `tests/test_health.py`
- Modify: `src/mm_bot_manager/config.py`
- Modify: `src/mm_bot_manager/db.py`
- Modify: `src/mm_bot_manager/__main__.py`
- Modify: `Dockerfile`

**Interfaces:**
- Consumes: `DbConn.execute()`, initialized repositories, and existing Mattermost clients.
- Produces: `DbConn.ping() -> bool`; `start_health_server(db, host, port) -> asyncio.Server`; `HEALTH_HOST` and `HEALTH_PORT`; HTTP paths `/health/live` and `/health/ready`.

- [ ] **Step 1: Add failing `DbConn.ping()` tests**

Append to `tests/test_repository.py`:

```python
def test_ping_reports_healthy_connection() -> None:
    conn = connect(":memory:")
    try:
        assert conn.ping() is True
    finally:
        conn.close()


def test_ping_hides_database_errors() -> None:
    conn = connect(":memory:")
    conn.close()
    assert conn.ping() is False
```

- [ ] **Step 2: Add failing real-TCP health tests**

Create `tests/test_health.py`:

```python
import asyncio
from dataclasses import dataclass

import pytest

from mm_bot_manager.health import start_health_server


@dataclass
class FakeDb:
    healthy: bool = True

    def ping(self) -> bool:
        return self.healthy


async def request(server: asyncio.Server, path: str) -> bytes:
    socket = server.sockets[0]
    host, port = socket.getsockname()[:2]
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: health\r\n\r\n".encode())
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    return response


@pytest.mark.parametrize("path", ["/health/live", "/health/ready"])
async def test_healthy_endpoints_return_200(path: str) -> None:
    server = await start_health_server(FakeDb(), "127.0.0.1", 0)
    async with server:
        response = await request(server, path)
    assert response.startswith(b"HTTP/1.1 200 OK\r\n")
    assert response.endswith(b"ok\n")


async def test_database_failure_returns_503_without_error_details() -> None:
    server = await start_health_server(FakeDb(healthy=False), "127.0.0.1", 0)
    async with server:
        response = await request(server, "/health/ready")
    assert response.startswith(b"HTTP/1.1 503 Service Unavailable\r\n")
    assert response.endswith(b"unavailable\n")


async def test_unknown_path_returns_404() -> None:
    server = await start_health_server(FakeDb(), "127.0.0.1", 0)
    async with server:
        response = await request(server, "/metrics")
    assert response.startswith(b"HTTP/1.1 404 Not Found\r\n")
```

- [ ] **Step 3: Run the focused tests and verify failure**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider tests/test_repository.py tests/test_health.py -q
```

Expected: collection or attribute failure because `DbConn.ping` and `mm_bot_manager.health` do not exist.

- [ ] **Step 4: Implement `ping()` and the bounded HTTP server**

Add to `DbConn`:

```python
def ping(self) -> bool:
    try:
        row = self.execute("SELECT 1").fetchone()
    except Exception:
        return False
    return row is not None
```

Create `src/mm_bot_manager/health.py` with a `Protocol` so tests need no real database:

```python
import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Protocol


class HealthCheck(Protocol):
    def ping(self) -> bool:
        raise NotImplementedError


_RESPONSES = {
    200: ("OK", b"ok\n"),
    404: ("Not Found", b"not found\n"),
    503: ("Service Unavailable", b"unavailable\n"),
}


async def _handle_request(
    db: HealthCheck,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    status = 404
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=2)
        parts = line.decode("ascii", errors="replace").split()
        if len(parts) == 3 and parts[0] == "GET":
            if parts[1] in {"/health/live", "/health/ready"}:
                status = 200 if db.ping() else 503
    except (TimeoutError, ValueError):
        status = 404

    reason, body = _RESPONSES[status]
    writer.write(
        (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        + body
    )
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def start_health_server(
    db: HealthCheck,
    host: str,
    port: int,
) -> asyncio.Server:
    handler: Callable[
        [asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]
    ] = partial(_handle_request, db)
    return await asyncio.start_server(handler, host, port, limit=8192)
```

- [ ] **Step 5: Wire health settings and lifecycle into the process**

Add to `Settings`:

```python
health_host: str = Field(default="127.0.0.1")
health_port: int = Field(default=8080, ge=1, le=65535)
```

In `main_async`, wrap the database in `try/finally`, start the health server only after
Mattermost identity and approvers have been resolved, and cancel outstanding command tasks
before closing the DB:

```python
async def main_async() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    log = get_logger("mm_bot_manager")

    conn = _connect_database(settings)
    background: set[asyncio.Task[None]] = set()
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    sigterm_handler_installed = False
    if main_task is not None:
        try:
            loop.add_signal_handler(signal.SIGTERM, main_task.cancel)
            sigterm_handler_installed = True
        except NotImplementedError:
            pass

    try:
        init_schema(conn)
        bot_repo = BotRepo(conn)
        token_repo = TokenRepo(conn)
        pending_repo = PendingRequestRepo(conn)

        kw = {"verify_ssl": settings.mm_verify_ssl}
        async with (
            MattermostClient(settings.mm_rest_base, settings.mm_bot_token, **kw) as bot_mm,
            MattermostClient(settings.mm_rest_base, settings.mm_token, **kw) as admin_mm,
        ):
            me = await bot_mm.get_me()
            manager_user_id = me["id"]
            manager_username = me["username"]
            log.info("logged_in", user_id=manager_user_id, username=manager_username)

            approver_ids = await _resolve_approvers(
                admin_mm, settings.approver_usernames, log
            )
            if approver_ids:
                log.info("approval_required", approver_count=len(approver_ids))
            else:
                log.info("approval_disabled")

            router = MessageRouter(manager_user_id, manager_username)
            factory = CommandContextFactory(
                bot_repo=bot_repo,
                token_repo=token_repo,
                pending_repo=pending_repo,
                admin_mm=admin_mm,
                bot_mm=bot_mm,
                manager_user_id=manager_user_id,
                approver_ids=approver_ids,
            )

            health_server = await start_health_server(
                conn, settings.health_host, settings.health_port
            )
            log.info(
                "health_server_started",
                host=settings.health_host,
                port=settings.health_port,
            )
            async with health_server:
                async for event in stream_events(
                    settings.mm_ws_url,
                    settings.mm_bot_token,
                    verify_ssl=settings.mm_verify_ssl,
                ):
                    task = asyncio.create_task(handle_event(event, router, factory))
                    background.add(task)
                    task.add_done_callback(background.discard)
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        conn.close()
        if sigterm_handler_installed:
            loop.remove_signal_handler(signal.SIGTERM)
```

Install a SIGTERM handler that cancels the running `main_async` task, remove it during
cleanup, and suppress the resulting `asyncio.CancelledError` in `run()`. Keep the handler
guarded by `except NotImplementedError` so non-Unix development remains valid.
Import `signal`, `Settings`, and `start_health_server`; change `run()` to:

```python
def run() -> None:
    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        asyncio.run(main_async())
```

Update the runtime stage in `Dockerfile`:

```dockerfile
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8080
```

- [ ] **Step 6: Run focused and full non-Docker verification**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider tests --ignore=tests/test_db_postgres.py -q
PYTHONDONTWRITEBYTECODE=1 uv run ruff check src tests
PYTHONDONTWRITEBYTECODE=1 uv run ruff format --check src tests
PYTHONDONTWRITEBYTECODE=1 uv run mypy src
```

Expected: all commands pass.

- [ ] **Step 7: Commit only Task 2 files**

```bash
git add Dockerfile src/mm_bot_manager/health.py tests/test_health.py tests/test_repository.py
git add -p src/mm_bot_manager/config.py src/mm_bot_manager/db.py src/mm_bot_manager/__main__.py
git diff --cached
git commit -m "feat: expose Kubernetes health probes"
```

---

### Task 3: External-PostgreSQL Helm chart

**Files:**
- Create: `deploy/helm/mm-bot-manager/Chart.yaml`
- Create: `deploy/helm/mm-bot-manager/values.yaml`
- Create: `deploy/helm/mm-bot-manager/templates/_helpers.tpl`
- Create: `deploy/helm/mm-bot-manager/templates/serviceaccount.yaml`
- Create: `deploy/helm/mm-bot-manager/templates/configmap.yaml`
- Create: `deploy/helm/mm-bot-manager/templates/deployment.yaml`
- Create: `deploy/helm/mm-bot-manager/templates/NOTES.txt`
- Create: `deploy/helm/mm-bot-manager/examples/values-external.yaml`
- Create: `tests/test_helm_chart.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: application env vars, `app.existingSecret` keys `MM_TOKEN`/`MM_BOT_TOKEN`, and external DB Secret key `DB_URL`.
- Produces: `mm-bot-manager.fullname`, `selectorLabels`, `appImage`; one ServiceAccount, ConfigMap, and single-replica Deployment.

- [ ] **Step 1: Add PyYAML and lock it**

Add `"pyyaml>=6.0.2"` to `[dependency-groups].dev` in `pyproject.toml`, then run:

```bash
uv lock
uv sync
```

Expected: `uv.lock` records PyYAML and the development environment is synchronized.

- [ ] **Step 2: Write failing external-profile render tests**

Create `tests/test_helm_chart.py`:

```python
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).parents[1]
CHART = ROOT / "deploy/helm/mm-bot-manager"
HELM = shutil.which("helm")
pytestmark = pytest.mark.skipif(HELM is None, reason="helm is required")


def render(values: str, *extra: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            str(HELM),
            "template",
            "mmbot",
            str(CHART),
            "--namespace",
            "mmbot",
            "--kube-version",
            "1.21.1",
            "--values",
            str(CHART / "examples" / values),
            *extra,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def by_kind(resources: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [resource for resource in resources if resource["kind"] == kind]


def test_external_profile_renders_only_application_resources() -> None:
    resources = render("values-external.yaml")
    assert {resource["kind"] for resource in resources} == {
        "ConfigMap",
        "Deployment",
        "ServiceAccount",
    }
    deployment = by_kind(resources, "Deployment")[0]
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert deployment["spec"]["template"]["spec"]["automountServiceAccountToken"] is False


def test_external_database_and_tokens_use_secret_key_refs() -> None:
    deployment = by_kind(render("values-external.yaml"), "Deployment")[0]
    env = {
        item["name"]: item
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["MM_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "mm-bot-manager-app",
        "key": "MM_TOKEN",
    }
    assert env["MM_BOT_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "mm-bot-manager-app",
        "key": "MM_BOT_TOKEN",
    }
    assert env["DB_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "mm-bot-manager-external-db",
        "key": "DB_URL",
    }


def test_application_has_security_context_tmp_and_probes() -> None:
    deployment = by_kind(render("values-external.yaml"), "Deployment")[0]
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert {probe["httpGet"]["path"] for probe in (
        container["startupProbe"],
        container["readinessProbe"],
        container["livenessProbe"],
    )} == {"/health/live", "/health/ready"}
    assert pod["volumes"] == [{"name": "tmp", "emptyDir": {}}]
    assert container["volumeMounts"] == [{"name": "tmp", "mountPath": "/tmp"}]
```

- [ ] **Step 3: Run the render tests and verify failure**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider tests/test_helm_chart.py -q
```

Expected: failure because the chart does not exist.

- [ ] **Step 4: Create chart metadata and final values structure**

Create `Chart.yaml`:

```yaml
apiVersion: v2
name: mm-bot-manager
description: Mattermost bot manager
type: application
version: 0.1.0
appVersion: "0.1.0"
kubeVersion: ">=1.21.1-0"
```

Create `values.yaml` with these final top-level contracts:

```yaml
image:
  repository: mm-bot-manager
  tag: ""
  digest: ""
  pullPolicy: IfNotPresent
imagePullSecrets: []
nameOverride: ""
fullnameOverride: ""

app:
  existingSecret: mm-bot-manager-app

mattermost:
  url: https://mm.example.com
  approvers: ""
  verifySSL: true
logLevel: INFO

health:
  host: 0.0.0.0
  port: 8080
probes:
  startup:
    periodSeconds: 5
    timeoutSeconds: 2
    failureThreshold: 30
  readiness:
    periodSeconds: 10
    timeoutSeconds: 2
    failureThreshold: 3
  liveness:
    periodSeconds: 10
    timeoutSeconds: 2
    failureThreshold: 6

serviceAccount:
  create: true
  name: ""
podAnnotations: {}
podLabels: {}
podSecurityContext:
  runAsNonRoot: true
  runAsUser: 1000
  runAsGroup: 1000
  seccompProfile:
    type: RuntimeDefault
containerSecurityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: [ALL]
resources:
  requests:
    cpu: 50m
    memory: 64Mi
  limits:
    cpu: 500m
    memory: 256Mi
nodeSelector: {}
tolerations: []
affinity: {}
terminationGracePeriodSeconds: 30

database:
  mode: external
  external:
    existingSecret: mm-bot-manager-external-db
  internal:
    image:
      repository: postgres
      tag: 15.18-alpine3.24
      digest: ""
      pullPolicy: IfNotPresent
    auth:
      existingSecret: mm-bot-manager-postgresql
    service:
      port: 5432
    storage:
      storageClass: longhorn
      accessModes: [ReadWriteOnce]
      size: 8Gi
    podSecurityContext:
      seccompProfile:
        type: RuntimeDefault
    containerSecurityContext: {}
    resources:
      requests:
        cpu: 100m
        memory: 256Mi
      limits:
        cpu: "1"
        memory: 1Gi
    probes:
      startup:
        periodSeconds: 5
        timeoutSeconds: 3
        failureThreshold: 30
      readiness:
        periodSeconds: 10
        timeoutSeconds: 3
        failureThreshold: 3
      liveness:
        periodSeconds: 10
        timeoutSeconds: 3
        failureThreshold: 6
    backup:
      enabled: true
      schedule: "0 2 * * *"
      retentionDays: 14
      successfulJobsHistoryLimit: 3
      failedJobsHistoryLimit: 3
      storageClass: longhorn
      accessModes: [ReadWriteOnce]
      size: 5Gi
      resources:
        requests:
          cpu: 50m
          memory: 64Mi
        limits:
          cpu: 500m
          memory: 256Mi
    restore:
      enabled: false
      file: ""
```

- [ ] **Step 5: Implement helpers and external-mode resources**

In `_helpers.tpl`, implement:

- DNS-safe `name` and `fullname`;
- standard `helm.sh/chart`, `app.kubernetes.io/*` labels;
- immutable selector labels;
- ServiceAccount name;
- `appImage` and `postgresImage` helpers where non-empty digest produces
  `repository@digest`, otherwise `repository:tag`, with empty app tag falling back to
  `.Chart.AppVersion`.

In `serviceaccount.yaml`, render only when `serviceAccount.create` is true and set:

```yaml
automountServiceAccountToken: false
```

In `configmap.yaml`, render only non-secret values:

```yaml
MM_URL: <mattermost.url>
MM_APPROVERS: <mattermost.approvers>
MM_VERIFY_SSL: <mattermost.verifySSL as quoted string>
LOG_LEVEL: <logLevel>
HEALTH_HOST: <health.host>
HEALTH_PORT: <health.port as quoted string>
```

In `deployment.yaml`:

- hard-code `replicas: 1` and `strategy.type: Recreate`;
- reference ConfigMap keys explicitly;
- reference `MM_TOKEN` and `MM_BOT_TOKEN` from `app.existingSecret`;
- for external mode reference `DB_URL` from `database.external.existingSecret`;
- for internal mode emit `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`;
- expose a named `health` container port;
- configure all three HTTP probes;
- set Pod and container security contexts;
- mount an `emptyDir: {}` at `/tmp`;
- apply requests/limits, scheduling knobs, pull secrets, and termination grace.

Create `values-external.yaml`:

```yaml
app:
  existingSecret: mm-bot-manager-app
mattermost:
  url: https://mm.internal.example
database:
  mode: external
  external:
    existingSecret: mm-bot-manager-external-db
```

Make `NOTES.txt` print only namespace-safe `kubectl get pods` and `kubectl logs` commands.
It must never render Secret values.

- [ ] **Step 6: Run chart and application verification**

Run:

```bash
helm lint deploy/helm/mm-bot-manager
helm template mmbot deploy/helm/mm-bot-manager --namespace mmbot --kube-version 1.21.1 --values deploy/helm/mm-bot-manager/examples/values-external.yaml
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider tests/test_helm_chart.py -q
PYTHONDONTWRITEBYTECODE=1 uv run ruff check tests/test_helm_chart.py
```

Expected: lint, render, and tests pass; rendered output has no Secret, Service, Ingress,
StatefulSet, PVC, Job, or CronJob.

- [ ] **Step 7: Commit only Task 3 files**

```bash
git add pyproject.toml uv.lock deploy/helm/mm-bot-manager/Chart.yaml deploy/helm/mm-bot-manager/values.yaml deploy/helm/mm-bot-manager/templates/_helpers.tpl deploy/helm/mm-bot-manager/templates/serviceaccount.yaml deploy/helm/mm-bot-manager/templates/configmap.yaml deploy/helm/mm-bot-manager/templates/deployment.yaml deploy/helm/mm-bot-manager/templates/NOTES.txt deploy/helm/mm-bot-manager/examples/values-external.yaml tests/test_helm_chart.py
git commit -m "feat: add external database Helm deployment"
```

---

### Task 4: Internal PostgreSQL, backup, and manual restore

**Files:**
- Create: `deploy/helm/mm-bot-manager/templates/postgresql-service.yaml`
- Create: `deploy/helm/mm-bot-manager/templates/postgresql-statefulset.yaml`
- Create: `deploy/helm/mm-bot-manager/templates/backup-pvc.yaml`
- Create: `deploy/helm/mm-bot-manager/templates/backup-cronjob.yaml`
- Create: `deploy/helm/mm-bot-manager/templates/restore-job.yaml`
- Create: `deploy/helm/mm-bot-manager/examples/values-internal-longhorn.yaml`
- Create: `deploy/helm/mm-bot-manager/examples/values-internal-local-path.yaml`
- Modify: `tests/test_helm_chart.py`

**Interfaces:**
- Consumes: internal Secret keys `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`; `postgresImage`; `database.internal.*` values.
- Produces: internal PostgreSQL DNS name, retained data/backup volumes, verified dump files named `mm-bot-manager-<UTC timestamp>.dump`, and opt-in restore Job.

- [ ] **Step 1: Add failing internal-mode tests**

Append tests that assert:

```python
def test_internal_longhorn_profile_renders_database_and_backup() -> None:
    resources = render("values-internal-longhorn.yaml")
    kinds = {resource["kind"] for resource in resources}
    assert {"StatefulSet", "Service", "PersistentVolumeClaim", "CronJob"} <= kinds
    assert "Ingress" not in kinds

    statefulset = by_kind(resources, "StatefulSet")[0]
    claim = statefulset["spec"]["volumeClaimTemplates"][0]
    assert claim["spec"]["storageClassName"] == "longhorn"
    assert claim["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"

    cronjob = by_kind(resources, "CronJob")[0]
    assert cronjob["apiVersion"] == "batch/v1"
    assert cronjob["spec"]["concurrencyPolicy"] == "Forbid"
    script = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0][
        "args"
    ][0]
    assert "pg_dump --format=custom" in script
    assert "pg_restore --list" in script


def test_local_path_profile_changes_both_claims() -> None:
    resources = render("values-internal-local-path.yaml")
    statefulset = by_kind(resources, "StatefulSet")[0]
    backup = by_kind(resources, "PersistentVolumeClaim")[0]
    assert (
        statefulset["spec"]["volumeClaimTemplates"][0]["spec"]["storageClassName"]
        == "local-path"
    )
    assert backup["spec"]["storageClassName"] == "local-path"


def test_internal_application_uses_component_database_settings() -> None:
    deployment = by_kind(render("values-internal-longhorn.yaml"), "Deployment")[0]
    env = {
        item["name"]: item
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert "DB_URL" not in env
    assert env["DB_HOST"]["value"] == "mmbot-mm-bot-manager-postgresql"
    assert env["DB_PASSWORD"]["valueFrom"]["secretKeyRef"]["key"] == "POSTGRES_PASSWORD"
```

Add a render helper argument for `--show-only`, then assert that restore is absent by default
and renders only when both `database.internal.restore.enabled=true` and a safe filename are
provided.

- [ ] **Step 2: Run internal render tests and verify failure**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider tests/test_helm_chart.py -q
```

Expected: failures because internal templates and profiles do not exist.

- [ ] **Step 3: Implement the PostgreSQL Service and StatefulSet**

Render both resources only when `database.mode` is `internal`.

The Service is headless and internal:

```yaml
spec:
  clusterIP: None
  ports:
    - name: postgresql
      port: <database.internal.service.port>
      targetPort: postgresql
  selector:
    app.kubernetes.io/name: <chart name>
    app.kubernetes.io/instance: <release>
    app.kubernetes.io/component: postgresql
```

The StatefulSet has one replica, the PostgreSQL image helper, no API token, and:

```yaml
env:
  - name: POSTGRES_USER
    valueFrom:
      secretKeyRef:
        name: <database.internal.auth.existingSecret>
        key: POSTGRES_USER
  - name: POSTGRES_PASSWORD
    valueFrom:
      secretKeyRef:
        name: <database.internal.auth.existingSecret>
        key: POSTGRES_PASSWORD
  - name: POSTGRES_DB
    valueFrom:
      secretKeyRef:
        name: <database.internal.auth.existingSecret>
        key: POSTGRES_DB
  - name: PGDATA
    value: /var/lib/postgresql/data/pgdata
```

Mount the claim at `/var/lib/postgresql/data`. Use `pg_isready` shell exec probes with
`POSTGRES_USER` and `POSTGRES_DB`. Put `helm.sh/resource-policy: keep` on the claim template,
set `ReadWriteOnce` by default, and do not use
`persistentVolumeClaimRetentionPolicy`, which is outside the Kubernetes 1.21.1 contract.

- [ ] **Step 4: Implement retained backup storage and validated dumps**

Render `backup-pvc.yaml` and `backup-cronjob.yaml` only for internal mode with backups enabled.
Name them `<fullname>-backup`; the default release name `mm-bot-manager` therefore produces
PVC and CronJob names `mm-bot-manager-backup`.
Annotate the backup PVC:

```yaml
metadata:
  annotations:
    helm.sh/resource-policy: keep
```

Label the PVC and CronJob with `app.kubernetes.io/component: backup` so the runbook can
resolve the retained claim without guessing a generated name.

Set the CronJob to `apiVersion: batch/v1`, `concurrencyPolicy: Forbid`, and use the PostgreSQL
image with `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, and `PGPASSWORD`. `PGDATABASE`,
`PGUSER`, and `PGPASSWORD` come from the internal auth Secret; `PGHOST` and `PGPORT` come
from chart-generated service configuration.

Render `successfulJobsHistoryLimit` and `failedJobsHistoryLimit` from the corresponding
backup values. Apply chart-level `imagePullSecrets` to the StatefulSet, backup CronJob, and
restore Job as well as the application Deployment.

Use this exact shell logic:

```sh
set -eu
umask 077
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
temporary="/backups/.mm-bot-manager-${timestamp}.dump.tmp"
final="/backups/mm-bot-manager-${timestamp}.dump"
trap 'rm -f "$temporary"' EXIT
pg_dump --format=custom --file="$temporary"
pg_restore --list "$temporary" >/dev/null
mv "$temporary" "$final"
trap - EXIT
printf 'backup created: %s\n' "$final"
find /backups -type f -name 'mm-bot-manager-*.dump' \
  -mtime "+${RETENTION_DAYS}" -delete
```

Mount the backup PVC at `/backups`; use `restartPolicy: OnFailure`, disable API-token
automounting, and apply backup resources.

- [ ] **Step 5: Implement the explicitly rendered restore Job**

Render `restore-job.yaml` only when all of these are true:

- `database.mode=internal`;
- `database.internal.restore.enabled=true`;
- `database.internal.restore.file` is non-empty.

The Job is not a Helm hook. It mounts the retained backup PVC read-only, uses the same
`PG*` variables as backup, validates the archive, replaces the target database, and restores:
name the resource `<fullname>-restore`, which is `mm-bot-manager-restore` for the documented
default release.

```sh
set -eu
archive="/backups/${RESTORE_FILE}"
test -f "$archive"
pg_restore --list "$archive" >/dev/null
target_database="$PGDATABASE"
export PGDATABASE=postgres
dropdb --if-exists --force "$target_database"
createdb "$target_database"
pg_restore --exit-on-error --no-owner --dbname="$target_database" "$archive"
```

The operator will render only this file with `helm template --show-only
templates/restore-job.yaml` after scaling the application to zero.

- [ ] **Step 6: Add Longhorn and local-path profiles**

Create `values-internal-longhorn.yaml`:

```yaml
app:
  existingSecret: mm-bot-manager-app
mattermost:
  url: https://mm.internal.example
database:
  mode: internal
  internal:
    auth:
      existingSecret: mm-bot-manager-postgresql
    storage:
      storageClass: longhorn
      size: 8Gi
    backup:
      storageClass: longhorn
      size: 5Gi
      schedule: "0 2 * * *"
      retentionDays: 14
```

Create `values-internal-local-path.yaml` with the same structure, but set both StorageClasses
to `local-path`, data size to `4Gi`, backup size to `2Gi`, and retention to 7 days.

- [ ] **Step 7: Run internal chart verification**

Run:

```bash
helm lint deploy/helm/mm-bot-manager --values deploy/helm/mm-bot-manager/examples/values-internal-longhorn.yaml
helm template mmbot deploy/helm/mm-bot-manager --namespace mmbot --kube-version 1.21.1 --values deploy/helm/mm-bot-manager/examples/values-internal-longhorn.yaml
helm template mmbot deploy/helm/mm-bot-manager --namespace mmbot --kube-version 1.21.1 --values deploy/helm/mm-bot-manager/examples/values-internal-local-path.yaml
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider tests/test_helm_chart.py -q
```

Expected: all commands pass. No `Ingress`, application `Service`, external chart dependency,
or plaintext Secret is rendered.

- [ ] **Step 8: Commit only Task 4 files**

```bash
git add deploy/helm/mm-bot-manager/templates/postgresql-service.yaml deploy/helm/mm-bot-manager/templates/postgresql-statefulset.yaml deploy/helm/mm-bot-manager/templates/backup-pvc.yaml deploy/helm/mm-bot-manager/templates/backup-cronjob.yaml deploy/helm/mm-bot-manager/templates/restore-job.yaml deploy/helm/mm-bot-manager/examples/values-internal-longhorn.yaml deploy/helm/mm-bot-manager/examples/values-internal-local-path.yaml tests/test_helm_chart.py
git commit -m "feat: deploy internal PostgreSQL with backups"
```

---

### Task 5: Air-gap profiles, schema, and Kubernetes 1.21.1 validation

**Files:**
- Create: `deploy/helm/mm-bot-manager/values.schema.json`
- Create: `deploy/helm/mm-bot-manager/examples/values-harbor.yaml`
- Create: `scripts/verify-kubernetes.sh`
- Modify: `tests/test_helm_chart.py`

**Interfaces:**
- Consumes: final `values.yaml`, all four example profiles, Helm-generated YAML.
- Produces: conditional values validation, Harbor-only image rendering, digest precedence tests, and strict 1.21.1 schema verification.

- [ ] **Step 1: Add failing Harbor and digest assertions**

Append:

```python
def workload_images(resources: list[dict[str, Any]]) -> list[str]:
    images: list[str] = []
    for resource in resources:
        if resource["kind"] == "Deployment":
            pod = resource["spec"]["template"]["spec"]
        elif resource["kind"] == "StatefulSet":
            pod = resource["spec"]["template"]["spec"]
        elif resource["kind"] == "CronJob":
            pod = resource["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        else:
            continue
        images.extend(container["image"] for container in pod.get("containers", []))
    return images


def test_harbor_profile_uses_only_local_registry() -> None:
    images = workload_images(render("values-harbor.yaml"))
    assert images
    assert all(image.startswith("harbor.internal.example/") for image in images)


def test_digest_takes_precedence_over_tag() -> None:
    digest = "sha256:" + "a" * 64
    deployment = by_kind(
        render("values-external.yaml", "--set", f"image.digest={digest}"),
        "Deployment",
    )[0]
    image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image == f"mm-bot-manager@{digest}"


def test_postgresql_digest_takes_precedence_for_database_and_backup() -> None:
    digest = "sha256:" + "b" * 64
    resources = render(
        "values-internal-longhorn.yaml",
        "--set",
        f"database.internal.image.digest={digest}",
    )
    images = workload_images(resources)
    assert f"postgres@{digest}" in images
    assert all(
        image == f"postgres@{digest}"
        for image in images
        if image.startswith("postgres")
    )


def test_schema_rejects_an_unknown_database_mode() -> None:
    result = subprocess.run(
        [
            str(HELM),
            "template",
            "mmbot",
            str(CHART),
            "--set",
            "database.mode=sqlite",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "database.mode" in result.stderr


def test_chart_never_renders_secret_or_ingress() -> None:
    for profile in (
        "values-external.yaml",
        "values-internal-longhorn.yaml",
        "values-internal-local-path.yaml",
        "values-harbor.yaml",
    ):
        kinds = {resource["kind"] for resource in render(profile)}
        assert "Secret" not in kinds
        assert "Ingress" not in kinds
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider tests/test_helm_chart.py -q
```

Expected: Harbor profile is missing and invalid values are not yet rejected.

- [ ] **Step 3: Add strict conditional JSON Schema**

Create draft-07 `values.schema.json` with:

- `additionalProperties: false` at every fixed-shape object level, while annotation,
  label, node selector, and resource maps allow their documented arbitrary keys;
- `database.mode` enum `external|internal`;
- non-empty Secret names;
- URL format for `mattermost.url`;
- `logLevel` enum `DEBUG|INFO|WARNING|ERROR`;
- port ranges `1..65535`;
- positive CPU/memory quantity strings;
- digest pattern `^$|^sha256:[0-9a-f]{64}$`;
- backup cron schedule as a non-empty string;
- positive retention and storage sizes;
- restore filename pattern `^$|^[A-Za-z0-9][A-Za-z0-9._-]*$`;
- `if/then` rules requiring external Secret configuration in external mode and internal auth,
  image, storage, and backup configuration in internal mode.

Validate the schema itself:

```bash
python -m json.tool deploy/helm/mm-bot-manager/values.schema.json
```

- [ ] **Step 4: Add the closed-contour profile**

Create `values-harbor.yaml`:

```yaml
image:
  repository: harbor.internal.example/platform/mm-bot-manager
  tag: "0.1.0"
imagePullSecrets:
  - name: harbor-pull
app:
  existingSecret: mm-bot-manager-app
mattermost:
  url: https://mm.internal.example
database:
  mode: internal
  internal:
    image:
      repository: harbor.internal.example/library/postgres
      tag: 15.18-alpine3.24
    auth:
      existingSecret: mm-bot-manager-postgresql
    storage:
      storageClass: longhorn
    backup:
      storageClass: longhorn
```

- [ ] **Step 5: Add a repeatable compatibility gate**

Create executable `scripts/verify-kubernetes.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

chart="deploy/helm/mm-bot-manager"
profiles=(
  "values-external.yaml"
  "values-internal-longhorn.yaml"
  "values-internal-local-path.yaml"
  "values-harbor.yaml"
)
temporary_dir="$(mktemp -d)"
trap 'rm -rf "$temporary_dir"' EXIT

helm lint "$chart"

for profile in "${profiles[@]}"; do
  output="$temporary_dir/${profile%.yaml}.yaml"
  helm template mmbot "$chart" \
    --namespace mmbot \
    --kube-version 1.21.1 \
    --values "$chart/examples/$profile" >"$output"
  kubeconform \
    -strict \
    -summary \
    -kubernetes-version 1.21.1 \
    "$output"
done
```

Make it executable. Do not add `timeZone` to the CronJob: that field is not part of the
Kubernetes 1.21.1 target.

- [ ] **Step 6: Run schema, render, and semantic verification**

Run:

```bash
python -m json.tool deploy/helm/mm-bot-manager/values.schema.json
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider tests/test_helm_chart.py -q
scripts/verify-kubernetes.sh
```

Expected: all four profiles lint, render with `--kube-version 1.21.1`, pass strict
kubeconform 1.21.1 schemas, contain no Secret/Ingress, and use only Harbor repositories in
the Harbor profile.

- [ ] **Step 7: Commit only Task 5 files**

```bash
git add deploy/helm/mm-bot-manager/values.schema.json deploy/helm/mm-bot-manager/examples/values-harbor.yaml scripts/verify-kubernetes.sh tests/test_helm_chart.py
git commit -m "test: validate Kubernetes 1.21 air-gap manifests"
```

---

### Task 6: Secret examples and operator runbook

**Files:**
- Create: `deploy/helm/mm-bot-manager/examples/app-secret.env.example`
- Create: `deploy/helm/mm-bot-manager/examples/external-db-secret.env.example`
- Create: `deploy/helm/mm-bot-manager/examples/internal-db-secret.env.example`
- Create: `deploy/helm/mm-bot-manager/README.md`
- Create: `docs/kubernetes.md`
- Create: `.dockerignore`
- Modify: `.gitignore`
- Modify: `README.md`
- Modify: `.env.example`

**Interfaces:**
- Consumes: all chart values, Secret key contracts, backup/restore resource names, and Harbor repositories.
- Produces: copy-safe secret creation commands, air-gap image workflow, operations runbook, and build-context exclusions.

- [ ] **Step 1: Add secret-key example files**

Create:

```dotenv
# app-secret.env.example
MM_TOKEN=CHANGE_THIS_ADMIN_PAT
MM_BOT_TOKEN=CHANGE_THIS_MANAGER_BOT_TOKEN
```

```dotenv
# external-db-secret.env.example
DB_URL=postgresql://mm_bot:CHANGE_THIS_PASSWORD@postgres.example.internal:5432/mm_bot_manager
```

```dotenv
# internal-db-secret.env.example
POSTGRES_USER=mm_bot
POSTGRES_PASSWORD=CHANGE_THIS_RANDOM_PASSWORD
POSTGRES_DB=mm_bot_manager
```

Update `.gitignore`:

```gitignore
# Kubernetes Secret working copies
/app-secret.env
/external-db-secret.env
/internal-db-secret.env
deploy/helm/**/examples/*.env
```

The committed `.env.example` files remain visible because their suffix is `.env.example`.

- [ ] **Step 2: Exclude credentials and development files from Docker build context**

Create `.dockerignore`:

```dockerignore
.git
.github
.claude
.env
*.env
data
docs
tests
.venv
__pycache__
*.py[cod]
.pytest_cache
.mypy_cache
.ruff_cache
htmlcov
deploy/helm/**/examples/*.env
```

- [ ] **Step 3: Write the chart-local README**

Document:

- Kubernetes `>=1.21.1` and Helm 3 requirements;
- the exact external/internal resource matrix;
- every values section and its default;
- fixed Secret key contracts;
- Longhorn production recommendation and local-path limitation;
- absence of application Service/Ingress;
- commands for `helm lint`, each `helm template` profile, and `helm package`;
- two required images for internal mode and one for external mode.

Keep this README usable after `helm package` without relying on repository-external links.

- [ ] **Step 4: Write secret creation and rotation procedures**

In `docs/kubernetes.md`, include exact copy-safe commands:

```bash
kubectl create namespace mm-bot-manager

cp deploy/helm/mm-bot-manager/examples/app-secret.env.example app-secret.env
chmod 0600 app-secret.env
kubectl -n mm-bot-manager create secret generic mm-bot-manager-app \
  --from-env-file=app-secret.env \
  --dry-run=client -o yaml | kubectl apply -f -
```

Include the exact external database commands:

```bash
cp deploy/helm/mm-bot-manager/examples/external-db-secret.env.example \
  external-db-secret.env
chmod 0600 external-db-secret.env
kubectl -n mm-bot-manager create secret generic mm-bot-manager-external-db \
  --from-env-file=external-db-secret.env \
  --dry-run=client -o yaml | kubectl apply -f -
```

Include the exact internal database commands:

```bash
cp deploy/helm/mm-bot-manager/examples/internal-db-secret.env.example \
  internal-db-secret.env
chmod 0600 internal-db-secret.env
kubectl -n mm-bot-manager create secret generic mm-bot-manager-postgresql \
  --from-env-file=internal-db-secret.env \
  --dry-run=client -o yaml | kubectl apply -f -
```

Show key-only validation that never decodes values:

```bash
kubectl -n mm-bot-manager get secret mm-bot-manager-app \
  -o go-template='{{range $key, $value := .data}}{{printf "%s\n" $key}}{{end}}'
```

For rotation, repeat the dry-run/apply command and then run:

```bash
kubectl -n mm-bot-manager rollout restart deployment/mm-bot-manager
kubectl -n mm-bot-manager rollout status deployment/mm-bot-manager
```

Explain that filled `*.env` files are ignored, must remain mode `0600`, and should be
deleted or moved to the organization's secret vault after use.

- [ ] **Step 5: Document Harbor and closed-contour preparation**

Include:

```bash
docker build -t harbor.internal.example/platform/mm-bot-manager:0.1.0 .
docker push harbor.internal.example/platform/mm-bot-manager:0.1.0
docker pull postgres:15.18-alpine3.24
docker tag postgres:15.18-alpine3.24 \
  harbor.internal.example/library/postgres:15.18-alpine3.24
docker push harbor.internal.example/library/postgres:15.18-alpine3.24
```

Document physically separated contours and digest inspection with:

```bash
docker save \
  --output mm-bot-manager-images.tar \
  harbor.internal.example/platform/mm-bot-manager:0.1.0 \
  harbor.internal.example/library/postgres:15.18-alpine3.24
sha256sum mm-bot-manager-images.tar >mm-bot-manager-images.tar.sha256
sha256sum --check mm-bot-manager-images.tar.sha256
docker load --input mm-bot-manager-images.tar
docker image inspect harbor.internal.example/platform/mm-bot-manager:0.1.0 \
  --format '{{json .RepoDigests}}'
```

Create the pull Secret without writing the real password into shell history:

```bash
read -r HARBOR_USERNAME
read -rs HARBOR_PASSWORD
kubectl -n mm-bot-manager create secret docker-registry harbor-pull \
  --docker-server=harbor.internal.example \
  --docker-username="$HARBOR_USERNAME" \
  --docker-password="$HARBOR_PASSWORD"
unset HARBOR_PASSWORD
```

State explicitly that Harbor CA trust belongs in containerd/CRI-O/Docker configuration on
every node and is not fixed by `imagePullSecrets`.

- [ ] **Step 6: Document install, backup, restore, upgrade, rollback, and deletion**

Cover both installs using:

```bash
helm upgrade --install mm-bot-manager deploy/helm/mm-bot-manager \
  --namespace mm-bot-manager \
  --values deploy/helm/mm-bot-manager/examples/values-external.yaml \
  --atomic --wait
```

and the equivalent internal/Harbor profile.

Include the production internal command:

```bash
helm upgrade --install mm-bot-manager deploy/helm/mm-bot-manager \
  --namespace mm-bot-manager \
  --values deploy/helm/mm-bot-manager/examples/values-internal-longhorn.yaml \
  --atomic --wait
```

For the closed contour, replace the last values argument with
`deploy/helm/mm-bot-manager/examples/values-harbor.yaml`.

Document health verification with `kubectl get pods`, `kubectl describe pod`, and Pod-local
port-forwarding to port 8080. Explain why no Ingress exists.

Document a manual backup:

```bash
kubectl -n mm-bot-manager create job \
  --from=cronjob/mm-bot-manager-backup \
  "mm-bot-manager-backup-manual-$(date -u +%Y%m%d%H%M%S)"
```

Document archive inspection without hard-coding the public PostgreSQL image:

```bash
backup_image="$(kubectl -n mm-bot-manager get cronjob mm-bot-manager-backup \
  -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].image}')"
backup_claim="$(kubectl -n mm-bot-manager get pvc \
  -l app.kubernetes.io/instance=mm-bot-manager,app.kubernetes.io/component=backup \
  -o jsonpath='{.items[0].metadata.name}')"
kubectl -n mm-bot-manager run backup-inspector \
  --restart=Never \
  --image="$backup_image" \
  --overrides="$(
    printf '{"spec":{"automountServiceAccountToken":false,"containers":[{"name":"backup-inspector","image":"%s","command":["sh","-ec","ls -lh /backups"],"volumeMounts":[{"name":"backups","mountPath":"/backups","readOnly":true}]}],"volumes":[{"name":"backups","persistentVolumeClaim":{"claimName":"%s","readOnly":true}}]}}' \
      "$backup_image" "$backup_claim"
  )"
kubectl -n mm-bot-manager logs backup-inspector
kubectl -n mm-bot-manager delete pod backup-inspector
```

Document restore:

1. list archives from a temporary Pod mounting the retained backup PVC;
2. scale `deployment/mm-bot-manager` to zero;
3. render only `templates/restore-job.yaml` with the same values file,
   `database.internal.restore.enabled=true`, and the selected safe filename;
4. create the rendered Job, wait for completion, and inspect logs;
5. delete the restore Job;
6. scale the Deployment to one and wait for readiness.

Include the exact default-release flow:

```bash
kubectl -n mm-bot-manager scale deployment/mm-bot-manager --replicas=0
kubectl -n mm-bot-manager rollout status deployment/mm-bot-manager

helm template mm-bot-manager deploy/helm/mm-bot-manager \
  --namespace mm-bot-manager \
  --kube-version 1.21.1 \
  --values deploy/helm/mm-bot-manager/examples/values-internal-longhorn.yaml \
  --show-only templates/restore-job.yaml \
  --set database.internal.restore.enabled=true \
  --set-string database.internal.restore.file=mm-bot-manager-20260728T020000Z.dump \
  >restore-job.yaml
kubectl -n mm-bot-manager create -f restore-job.yaml
kubectl -n mm-bot-manager wait \
  --for=condition=complete job/mm-bot-manager-restore \
  --timeout=30m
kubectl -n mm-bot-manager logs job/mm-bot-manager-restore
kubectl -n mm-bot-manager delete job/mm-bot-manager-restore

kubectl -n mm-bot-manager scale deployment/mm-bot-manager --replicas=1
kubectl -n mm-bot-manager rollout status deployment/mm-bot-manager
```

State that the filename in this example must be replaced with one returned by the archive
inspection command, while retaining the schema-safe basename format.

Document that uninstall retains data and backup PVCs, then provide separate name-resolved
commands to inspect and delete only those PVCs after explicit operator confirmation.

Explain that two PVCs in the same Longhorn cluster are not complete disaster recovery:
configure Longhorn export to a separate S3/NFS target. For `local-path`, require copying
validated dump files off the node because both local data and local backups disappear with
that node.

- [ ] **Step 7: Link the runbook and update environment documentation**

Add a short Kubernetes section to `README.md` linking to `docs/kubernetes.md`.

Extend `.env.example` and the README configuration table with:

- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`;
- `HEALTH_HOST`, `HEALTH_PORT`;
- precedence: `DB_URL`, then component PostgreSQL settings, then `DB_PATH`.

Do not disturb the user's existing Mattermost setup and approval-flow edits.

- [ ] **Step 8: Verify documentation paths and secret hygiene**

Run:

```bash
! rg -n 'MM_TOKEN=replace|MM_BOT_TOKEN=replace|POSTGRES_PASSWORD=secret' deploy docs
rg -n 'Ingress|ingress-nginx|local-path|longhorn|Harbor|imagePullSecrets' docs/kubernetes.md deploy/helm/mm-bot-manager/README.md
git check-ignore app-secret.env
git diff --check
```

Expected:

- no real or compose-default credentials appear in deploy/runbook files;
- all required operational topics are present;
- `app-secret.env` is ignored;
- no whitespace errors are reported.

- [ ] **Step 9: Commit only Task 6 files**

```bash
git add .dockerignore .gitignore docs/kubernetes.md deploy/helm/mm-bot-manager/README.md deploy/helm/mm-bot-manager/examples/app-secret.env.example deploy/helm/mm-bot-manager/examples/external-db-secret.env.example deploy/helm/mm-bot-manager/examples/internal-db-secret.env.example
git add -p .env.example README.md
git diff --cached
git commit -m "docs: add Kubernetes deployment runbook"
```

---

### Task 7: Full regression and release artifact verification

**Files:**
- Modify only files requiring corrections discovered by the commands below.
- Do not alter unrelated working-tree changes.

**Interfaces:**
- Consumes: completed Python changes, Helm chart, all examples, and the operator runbook.
- Produces: verified source tree and a locally packaged dependency-free chart.

- [ ] **Step 1: Run all non-container Python checks**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider tests --ignore=tests/test_db_postgres.py -q
PYTHONDONTWRITEBYTECODE=1 uv run ruff check src tests
PYTHONDONTWRITEBYTECODE=1 uv run ruff format --check src tests
PYTHONDONTWRITEBYTECODE=1 uv run mypy src
```

Expected: all commands pass.

- [ ] **Step 2: Run the PostgreSQL integration suite**

```bash
uv sync --extra postgres
PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider tests/test_db_postgres.py -q
```

Expected: PostgreSQL 15 integration tests pass. If Docker is unavailable, report that exact
environmental blocker instead of treating the suite as passed.

- [ ] **Step 3: Run the Kubernetes compatibility gate**

```bash
scripts/verify-kubernetes.sh
```

Expected: external, internal-longhorn, internal-local-path, and Harbor profiles pass
`helm lint`, `helm template --kube-version 1.21.1`, and strict kubeconform 1.21.1 validation.

- [ ] **Step 4: Verify Helm 3 rendering explicitly**

Use an available Helm 3 binary and run:

```bash
helm version --short
helm lint deploy/helm/mm-bot-manager
helm template mmbot deploy/helm/mm-bot-manager \
  --namespace mmbot \
  --kube-version 1.21.1 \
  --values deploy/helm/mm-bot-manager/examples/values-harbor.yaml
```

Expected: the version starts with `v3.` and both commands pass. Helm 4 results may be
additional evidence but do not replace this check.

- [ ] **Step 5: Package and inspect the offline artifact**

Use a temporary output directory:

```bash
package_dir="$(mktemp -d)"
helm package deploy/helm/mm-bot-manager --destination "$package_dir"
tar -tzf "$package_dir/mm-bot-manager-0.1.0.tgz"
```

Expected: the archive contains templates, schema, README, and examples; it has no
`charts/` dependencies, filled env files, images, tokens, or passwords.

- [ ] **Step 6: Review final diff and status**

```bash
git diff --check
git status --short
git log --oneline -8
```

Expected: only intended Kubernetes implementation changes and the user's preserved
pre-existing edits are present; every implementation task has its focused commit.

If a verification command exposes a defect, return to the task that owns the affected file,
add a regression assertion there, make the focused fix, rerun that task's checks, and commit
the exact corrected files before repeating this full verification task. Do not create an
empty verification commit.

---

## Final acceptance evidence

Before declaring completion, capture:

- the exact pytest pass count for non-Docker and PostgreSQL suites;
- Ruff, formatting, and mypy success;
- Helm 3 version used;
- successful lint/render results for all profiles;
- kubeconform summary against Kubernetes 1.21.1;
- packaged chart filename and contents check;
- confirmation that no Secret or Ingress is rendered;
- confirmation that Harbor mode contains no public image repository;
- current `git status --short`, distinguishing the user's pre-existing edits from this work.
