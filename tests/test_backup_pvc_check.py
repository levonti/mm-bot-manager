"""Render and execute the first consumer of the backup PVC without a cluster."""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_helm_chart import CHART, HELM, by_kind, render

pytestmark = pytest.mark.skipif(HELM is None, reason="helm is required")
TEMPLATE = "templates/backup-pvc-check-job.yaml"


def check_job(profile: str = "values-internal-local-path.yaml", *extra: str) -> dict[str, Any]:
    return by_kind(render(profile, *extra, show_only=TEMPLATE), "Job")[0]


@pytest.mark.parametrize(
    "profile", ["values-internal-local-path.yaml", "values-internal-longhorn.yaml"]
)
def test_backup_pvc_has_an_immediate_non_hook_consumer(profile: str) -> None:
    resources = render(profile)
    job = by_kind(resources, "Job")[0]
    pvc = by_kind(resources, "PersistentVolumeClaim")[0]
    cronjob = by_kind(resources, "CronJob")[0]
    pod = job["spec"]["template"]["spec"]
    backup_pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert job["metadata"]["labels"]["app.kubernetes.io/component"] == "backup-pvc-check"
    assert "helm.sh/hook" not in job["metadata"].get("annotations", {})
    assert "helm.sh/resource-policy" not in job["metadata"].get("annotations", {})
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] == 600
    assert pod["automountServiceAccountToken"] is False
    assert pod["restartPolicy"] == "Never"
    assert "env" not in container and "envFrom" not in container
    assert (
        pod["volumes"]
        == backup_pod["volumes"]
        == [{"name": "backups", "persistentVolumeClaim": {"claimName": pvc["metadata"]["name"]}}]
    )
    assert container["volumeMounts"] == backup_pod["containers"][0]["volumeMounts"]
    assert container["image"] == backup_pod["containers"][0]["image"]
    assert container["resources"] == backup_pod["containers"][0]["resources"]


@pytest.mark.parametrize("profile", ["values-external.yaml", "values-internal-local-path.yaml"])
def test_no_check_or_backup_resources_when_backups_are_unused(profile: str) -> None:
    resources = render(profile, "--set", "database.internal.backup.enabled=false")
    assert not by_kind(resources, "Job")
    assert not by_kind(resources, "CronJob")
    assert not by_kind(resources, "PersistentVolumeClaim")


def test_check_inherits_harbor_digest_pull_policy_secrets_and_resources() -> None:
    job = check_job(
        "values-harbor.yaml",
        "--set",
        "database.internal.image.digest=sha256:" + "b" * 64,
        "--set",
        "database.internal.image.pullPolicy=Never",
        "--set",
        "database.internal.backup.resources.requests.memory=96Mi",
    )
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert pod["imagePullSecrets"] == [{"name": "harbor-pull"}]
    assert container["image"] == "harbor.internal.example/library/postgres@sha256:" + "b" * 64
    assert container["imagePullPolicy"] == "Never"
    assert container["resources"]["requests"]["memory"] == "96Mi"


def test_upgrade_uses_a_new_job_name_even_with_a_long_fullname(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    shutil.copytree(CHART, chart)
    template = chart / TEMPLATE
    source = template.read_text()
    names = []
    for revision in (1, 2, 123456789):
        # helm template has no revision flag; supply the real Helm template context.
        template.write_text(f'{{{{- $_ := set .Release "Revision" {revision} -}}}}\n' + source)
        job = by_kind(
            render(
                "values-internal-local-path.yaml",
                "--is-upgrade",
                "--set-string",
                "fullnameOverride=" + "a" * 63,
                chart=chart,
                show_only=TEMPLATE,
            ),
            "Job",
        )[0]
        name = job["metadata"]["name"]
        assert len(name) <= 63
        assert name.endswith(f"-backup-pvc-check-{revision}")
        names.append(name)
    assert len(set(names)) == 3


def run_check(
    backups: Path, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    container = check_job()["spec"]["template"]["spec"]["containers"][0]
    script = container["args"][0].replace("/backups", str(backups))
    return subprocess.run(
        [*container["command"], script], capture_output=True, text=True, env=env, timeout=10
    )


def test_rendered_check_reads_writes_deletes_and_preserves_existing_archives(
    tmp_path: Path,
) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    archive = backups / "existing.dump"
    archive.write_bytes(b"existing archive must survive")
    result = run_check(backups)
    assert result.returncode == 0, result.stderr
    assert "backup PVC write/read/delete check passed" in result.stdout
    assert list(backups.iterdir()) == [archive]
    assert archive.read_bytes() == b"existing archive must survive"


def test_check_fails_when_the_mount_cannot_hold_a_file(tmp_path: Path) -> None:
    backups = tmp_path / "not-a-directory"
    backups.write_text("must survive")
    result = run_check(backups)
    assert result.returncode != 0
    assert "check passed" not in result.stdout
    assert backups.read_text() == "must survive"


@pytest.mark.parametrize("failure", ["create", "write", "read", "corrupt-read", "delete"])
def test_storage_command_failure_is_fatal(tmp_path: Path, failure: str) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    tools = tmp_path / "bin"
    tools.mkdir()
    write_directory = backups / ".pvc-check-write-failure"
    if failure == "write":
        write_directory.mkdir()
    command, source = {
        "create": ("mktemp", "exit 1"),
        "write": ("mktemp", 'printf "%s\\n" "$PVC_TEST_WRITE_DIRECTORY"'),
        # A failing read must not pass even if it produces the expected bytes.
        "read": ("cat", "printf 'mm-bot-manager backup PVC check\\n'; exit 1"),
        "corrupt-read": ("cat", "printf 'wrong data'"),
        "delete": ("rm", "exit 1"),
    }[failure]
    stub = tools / command
    stub.write_text(f"#!/bin/sh\n{source}\n")
    stub.chmod(0o755)
    result = run_check(
        backups,
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "PVC_TEST_WRITE_DIRECTORY": str(write_directory),
        },
    )
    assert result.returncode != 0
    assert "check passed" not in result.stdout
    if failure not in {"write", "delete"}:
        assert not list(backups.iterdir())
