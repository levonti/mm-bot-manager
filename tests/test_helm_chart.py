import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[1]
CHART = ROOT / "deploy/helm/mm-bot-manager"
HELM = shutil.which("helm")
VERIFY_KUBERNETES = ROOT / "scripts/verify-kubernetes.sh"
pytestmark = pytest.mark.skipif(HELM is None, reason="helm is required")


def schema_diagnostic(result: subprocess.CompletedProcess[str]) -> str:
    # Helm 3 reports JSON schema paths with dots; Helm 4 uses slashes.
    return result.stderr.replace(".", "/")


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


def test_configmap_changes_roll_the_application_pod_template() -> None:
    original = by_kind(render("values-external.yaml"), "Deployment")[0]
    changed = by_kind(
        render(
            "values-external.yaml",
            "--set-string",
            "mattermost.url=https://changed.internal.example",
        ),
        "Deployment",
    )[0]

    original_annotations = original["spec"]["template"]["metadata"]["annotations"]
    changed_annotations = changed["spec"]["template"]["metadata"]["annotations"]
    assert original_annotations["checksum/config"]
    assert changed_annotations["checksum/config"]
    assert original_annotations["checksum/config"] != changed_annotations["checksum/config"]


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
    assert cronjob["spec"]["jobTemplate"]["metadata"]["labels"] == {
        "app.kubernetes.io/name": "mm-bot-manager",
        "app.kubernetes.io/instance": "mmbot",
        "app.kubernetes.io/component": "backup",
    }
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

    result = subprocess.run(
        [
            str(HELM),
            "template",
            "mmbot",
            str(CHART),
            "--values",
            str(CHART / "examples" / "values-internal-longhorn.yaml"),
            "--set",
            "database.internal.restore.enabled=true",
            "--set",
            "database.internal.restore.file=../backup.dump",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "restore/file" in schema_diagnostic(result)


def workload_images(resources: list[dict[str, Any]]) -> list[str]:
    images: list[str] = []
    for resource in resources:
        if resource["kind"] in {"Deployment", "StatefulSet"}:
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
    assert all(image == f"postgres@{digest}" for image in images if image.startswith("postgres"))


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
    assert "database/mode" in schema_diagnostic(result)


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


def test_schema_rejects_unknown_toleration_and_affinity_fields() -> None:
    for extra, expected_path in (
        (("--set", "tolerations[0].unexpected=value"), "tolerations/0"),
        (("--set", "affinity.unexpected=value"), "affinity"),
    ):
        result = subprocess.run(
            [
                str(HELM),
                "template",
                "mmbot",
                str(CHART),
                "--values",
                str(CHART / "examples" / "values-external.yaml"),
                *extra,
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert expected_path in schema_diagnostic(result)


def test_schema_requires_only_the_active_database_secret() -> None:
    assert render(
        "values-external.yaml",
        "--set-string",
        "database.internal.auth.existingSecret=",
    )
    assert render(
        "values-internal-longhorn.yaml",
        "--set-string",
        "database.external.existingSecret=",
    )

    for values, setting, expected_path in (
        (
            "values-external.yaml",
            "database.external.existingSecret=",
            "external/existingSecret",
        ),
        (
            "values-internal-longhorn.yaml",
            "database.internal.auth.existingSecret=",
            "internal/auth/existingSecret",
        ),
    ):
        result = subprocess.run(
            [
                str(HELM),
                "template",
                "mmbot",
                str(CHART),
                "--values",
                str(CHART / "examples" / values),
                "--set-string",
                setting,
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert expected_path in schema_diagnostic(result)


def test_verify_script_requires_an_explicit_local_schema_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KUBECONFORM_SCHEMA_LOCATION", raising=False)

    result = subprocess.run(
        [str(VERIFY_KUBERNETES)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "KUBECONFORM_SCHEMA_LOCATION" in result.stderr
    assert "local directory" in result.stderr


def test_verify_script_passes_only_a_local_schema_location(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    kubeconform_arguments = tmp_path / "kubeconform-arguments"

    helm = tools / "helm"
    helm.write_text("#!/usr/bin/env bash\nexit 0\n")
    helm.chmod(0o755)
    kubeconform = tools / "kubeconform"
    kubeconform.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" >"$KUBECONFORM_ARGUMENT_LOG"\n'
    )
    kubeconform.chmod(0o755)

    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setenv("KUBECONFORM_SCHEMA_LOCATION", str(schema_dir))
    monkeypatch.setenv("KUBECONFORM_ARGUMENT_LOG", str(kubeconform_arguments))

    result = subprocess.run([str(VERIFY_KUBERNETES)], cwd=ROOT, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    arguments = kubeconform_arguments.read_text()
    assert "-schema-location" in arguments
    assert str(schema_dir) in arguments
    assert "raw.githubusercontent.com" not in arguments


def test_catalog_viewer_setting_is_passed_from_values_to_application() -> None:
    resources = render("values-external.yaml", "--set-string", "mattermost.catalogViewers=auditor")
    config = by_kind(resources, "ConfigMap")[0]
    assert config["data"]["MM_CATALOG_VIEWERS"] == "auditor"
    deployment = by_kind(resources, "Deployment")[0]
    env = {
        item["name"]: item
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["MM_CATALOG_VIEWERS"]["valueFrom"]["configMapKeyRef"]["key"] == "MM_CATALOG_VIEWERS"
