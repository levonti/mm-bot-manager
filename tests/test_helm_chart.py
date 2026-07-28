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
