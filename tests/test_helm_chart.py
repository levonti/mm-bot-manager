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


def render(
    values: str, *extra: str, show_only: str | None = None, chart: Path = CHART
) -> list[dict[str, Any]]:
    show_only_args = ["--show-only", show_only] if show_only is not None else []
    result = subprocess.run(
        [
            str(HELM),
            "template",
            "mmbot",
            str(chart),
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
    assert all(
        job["metadata"]["labels"]["app.kubernetes.io/component"] != "restore"
        for job in by_kind(render("values-internal-longhorn.yaml"), "Job")
    )

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
        if resource["kind"] in {"Deployment", "StatefulSet", "Job"}:
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


def test_default_language_setting_reaches_container_and_rejects_unknown_locale() -> None:
    resources = render("values-external.yaml", "--set-string", "mattermost.defaultLanguage=ru")
    config = by_kind(resources, "ConfigMap")[0]
    assert config["data"]["MM_DEFAULT_LANGUAGE"] == "ru"
    deployment = by_kind(resources, "Deployment")[0]
    env = {
        item["name"]: item
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert (
        env["MM_DEFAULT_LANGUAGE"]["valueFrom"]["configMapKeyRef"]["key"] == "MM_DEFAULT_LANGUAGE"
    )
    with pytest.raises(subprocess.CalledProcessError):
        render("values-external.yaml", "--set-string", "mattermost.defaultLanguage=unsupported")


def test_application_label_is_unique_without_changing_immutable_selector() -> None:
    resources = render("values-internal-longhorn.yaml")
    app = by_kind(resources, "Deployment")[0]
    assert app["metadata"]["labels"]["app.kubernetes.io/component"] == "application"
    assert (
        app["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"]
        == "application"
    )
    assert app["spec"]["selector"]["matchLabels"] == {
        "app.kubernetes.io/name": "mm-bot-manager",
        "app.kubernetes.io/instance": "mmbot",
    }
    assert (
        by_kind(resources, "StatefulSet")[0]["spec"]["template"]["metadata"]["labels"][
            "app.kubernetes.io/component"
        ]
        == "postgresql"
    )


@pytest.mark.parametrize("key", ["name", "instance", "component"])
def test_custom_labels_cannot_override_application_identity(tmp_path: Path, key: str) -> None:
    override = tmp_path / "labels.yaml"
    override.write_text(f"podLabels:\n  app.kubernetes.io/{key}: wrong\n")
    result = subprocess.run(
        [str(HELM), "template", "mmbot", str(CHART), "-f", str(override)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and f"app.kubernetes.io/{key}" in result.stderr


def test_private_mattermost_ca_is_read_only_and_shared_by_both_clients() -> None:
    app = by_kind(
        render(
            "values-internal-longhorn.yaml",
            "--set",
            "mattermost.caBundle.existingConfigMap=private-ca",
            "--set",
            "mattermost.caBundle.key=roots.pem",
        ),
        "Deployment",
    )[0]
    pod = app["spec"]["template"]["spec"]
    env = {x["name"]: x for x in pod["containers"][0]["env"]}
    assert env["SSL_CERT_FILE"]["value"] == "/etc/mm-bot-manager/ca/ca-bundle.crt"
    assert pod["volumes"][-1] == {
        "name": "mattermost-ca",
        "configMap": {
            "name": "private-ca",
            "items": [{"key": "roots.pem", "path": "ca-bundle.crt"}],
        },
    }
    assert pod["containers"][0]["volumeMounts"][-1]["readOnly"] is True


@pytest.mark.parametrize("kind", ["inspect", "verification", "restore"])
def test_maintenance_jobs_inherit_harbor_digest_secrets_and_readonly_backup(kind: str) -> None:
    template = {
        "inspect": "backup-inspector-job.yaml",
        "verification": "backup-verification-job.yaml",
        "restore": "restore-job.yaml",
    }[kind]
    flags = [
        "--set",
        f"database.internal.{kind}.enabled=true",
        "--set",
        "database.internal.image.digest=sha256:" + "a" * 64,
    ]
    if kind != "inspect":
        flags += ["--set", f"database.internal.{kind}.file=archive.dump"]
    job = by_kind(render("values-harbor.yaml", *flags, show_only=f"templates/{template}"), "Job")[0]
    assert job["spec"]["backoffLimit"] == 0
    pod = job["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["imagePullSecrets"] == [{"name": "harbor-pull"}]
    assert (
        pod["containers"][0]["image"]
        == "harbor.internal.example/library/postgres@sha256:" + "a" * 64
    )
    assert pod["volumes"][0]["persistentVolumeClaim"]["readOnly"]
    assert pod["containers"][0]["volumeMounts"][0]["readOnly"]


@pytest.mark.parametrize(
    "extra",
    [
        [
            "--set",
            "database.internal.backup.enabled=false",
            "--set",
            "database.internal.inspect.enabled=true",
        ],
        [
            "--set",
            "database.internal.inspect.enabled=true",
            "--set",
            "database.internal.verification.enabled=true",
            "--set",
            "database.internal.verification.file=x.dump",
        ],
        ["--set", "database.internal.verification.enabled=true"],
        [
            "--set",
            "database.internal.verification.enabled=true",
            "--set",
            "database.internal.verification.file=../x.dump",
        ],
        ["--set", "database.mode=external", "--set", "database.internal.inspect.enabled=true"],
    ],
)
def test_invalid_maintenance_configuration_is_rejected_before_job_creation(
    extra: list[str],
) -> None:
    result = subprocess.run(
        [
            str(HELM),
            "template",
            "mmbot",
            str(CHART),
            "-f",
            str(CHART / "examples/values-internal-longhorn.yaml"),
            *extra,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_backup_filename_is_unique_per_pod_and_no_password_is_rendered() -> None:
    job = by_kind(render("values-internal-longhorn.yaml"), "CronJob")[0]
    container = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    assert "${timestamp}-${POD_UID}.dump" in container["args"][0]
    env = {x["name"]: x for x in container["env"]}
    assert env["POD_UID"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.uid"


def test_packaged_chart_excludes_filled_environment_working_copies(tmp_path: Path) -> None:
    import tarfile

    copied = tmp_path / "source"
    shutil.copytree(CHART, copied)
    (copied / "examples/app-secret.env").write_text("DO_NOT_PACKAGE=this-is-only-a-test\n")
    (copied / "values-site.yaml").write_text("not-for-distribution: true\n")
    subprocess.run(
        [str(HELM), "package", str(copied), "--destination", str(tmp_path)],
        check=True,
        capture_output=True,
    )
    package = next(tmp_path.glob("*.tgz"))
    with tarfile.open(package) as archive:
        names = archive.getnames()
    assert not any(x.endswith(".env") or x.endswith("/values-site.yaml") for x in names)
    assert any(x.endswith("values-site.yaml.example") for x in names)


def test_display_timezone_reaches_application() -> None:
    resources = render(
        "values-external.yaml", "--set-string", "mattermost.displayTimezone=Europe/Moscow"
    )
    assert by_kind(resources, "ConfigMap")[0]["data"]["MM_DISPLAY_TIMEZONE"] == "Europe/Moscow"
    deployment = by_kind(resources, "Deployment")[0]
    env = {
        item["name"]: item
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert (
        env["MM_DISPLAY_TIMEZONE"]["valueFrom"]["configMapKeyRef"]["key"] == "MM_DISPLAY_TIMEZONE"
    )
