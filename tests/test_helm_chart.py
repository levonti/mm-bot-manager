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


def render(values: str, *extra: str, show_only: str | None = None) -> list[dict[str, Any]]:
    show_only_args = ["--show-only", show_only] if show_only is not None else []
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
            *show_only_args,
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
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item for item in container["env"]}
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
    assert {
        probe["httpGet"]["path"]
        for probe in (
            container["startupProbe"],
            container["readinessProbe"],
            container["livenessProbe"],
        )
    } == {"/health/live", "/health/ready"}
    assert pod["volumes"] == [{"name": "tmp", "emptyDir": {}}]
    assert container["volumeMounts"] == [{"name": "tmp", "mountPath": "/tmp"}]


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
    script = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"][0]
    assert "pg_dump --format=custom" in script
    assert "pg_restore --list" in script
    retention_line = next(line for line in script.splitlines() if line.startswith("find "))
    assert retention_line.endswith("\\")
    assert not retention_line.endswith("\\\\")


def test_local_path_profile_changes_both_claims() -> None:
    resources = render("values-internal-local-path.yaml")
    statefulset = by_kind(resources, "StatefulSet")[0]
    backup = by_kind(resources, "PersistentVolumeClaim")[0]
    assert (
        statefulset["spec"]["volumeClaimTemplates"][0]["spec"]["storageClassName"] == "local-path"
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


def test_restore_job_requires_an_explicit_safe_filename() -> None:
    assert by_kind(render("values-internal-longhorn.yaml"), "Job") == []

    resources = render(
        "values-internal-longhorn.yaml",
        "--set",
        "database.internal.restore.enabled=true",
        "--set",
        "database.internal.restore.file=mm-bot-manager-20260728T000000Z.dump",
        show_only="templates/restore-job.yaml",
    )
    assert [resource["metadata"]["name"] for resource in by_kind(resources, "Job")] == [
        "mmbot-mm-bot-manager-restore"
    ]

    unsafe = render(
        "values-internal-longhorn.yaml",
        "--set",
        "database.internal.restore.enabled=true",
        "--set",
        "database.internal.restore.file=../backup.dump",
    )
    assert by_kind(unsafe, "Job") == []
