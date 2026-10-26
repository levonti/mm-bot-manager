"""Helm contracts for optional ESO delivery and existing-Secret compatibility."""

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.test_helm_chart import CHART, HELM, ROOT, by_kind, render

pytestmark = pytest.mark.skipif(HELM is None, reason="helm is required")


def eso_flags(mode: str = "external") -> list[str]:
    return ["--values", str(CHART / "examples" / f"values-eso-{mode}.yaml")]


@pytest.mark.parametrize(
    "profile", ["external", "internal-longhorn", "internal-local-path", "harbor"]
)
def test_existing_secret_profiles_do_not_require_eso(profile: str) -> None:
    resources = render(f"values-{profile}.yaml")
    assert not by_kind(resources, "ExternalSecret")
    assert not by_kind(resources, "Secret")
    assert not any(r["apiVersion"].startswith("external-secrets.io/") for r in resources)


@pytest.mark.parametrize(
    ("profile", "mode", "database_name", "keys"),
    [
        ("external", "external", "mm-bot-manager-external-db", {"DB_URL"}),
        (
            "internal-longhorn",
            "internal",
            "mm-bot-manager-postgresql",
            {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"},
        ),
        (
            "internal-local-path",
            "internal",
            "mm-bot-manager-postgresql",
            {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"},
        ),
        (
            "harbor",
            "internal",
            "mm-bot-manager-postgresql",
            {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"},
        ),
    ],
)
def test_eso_targets_existing_names_and_fixed_key_contracts(
    profile: str, mode: str, database_name: str, keys: set[str]
) -> None:
    resources = render(f"values-{profile}.yaml", *eso_flags(mode))
    secrets = by_kind(resources, "ExternalSecret")
    assert {s["spec"]["target"]["name"] for s in secrets} == {
        "mm-bot-manager-app",
        database_name,
    }
    assert not by_kind(resources, "Secret")
    assert not by_kind(resources, "SecretStore")
    assert not by_kind(resources, "ClusterSecretStore")
    assert not by_kind(resources, "RoleBinding")
    for secret in secrets:
        assert secret["apiVersion"] == "external-secrets.io/v1"
        assert secret["metadata"]["namespace"] == "mmbot"
        assert secret["metadata"]["labels"]["app.kubernetes.io/instance"] == "mmbot"
        assert "helm.sh/hook" not in secret["metadata"].get("annotations", {})
        spec = secret["spec"]
        assert spec["secretStoreRef"] == {"name": "vault-mm-bot-manager", "kind": "SecretStore"}
        assert spec["refreshInterval"] == "1h"
        assert spec["target"]["creationPolicy"] == "Orphan"
        assert spec["target"]["deletionPolicy"] == "Retain"
        assert spec["target"]["template"] == {"type": "Opaque"}
        expected_keys = (
            {"MM_TOKEN", "MM_BOT_TOKEN"} if spec["target"]["name"] == "mm-bot-manager-app" else keys
        )
        assert {item["secretKey"] for item in spec["data"]} == expected_keys
        for item in spec["data"]:
            assert item["remoteRef"]["property"] == item["secretKey"]
            assert item["remoteRef"]["key"].startswith("apps/mm-bot-manager/")

    # Enabling ESO must not give the workload Vault credentials or change its env.
    original = render(f"values-{profile}.yaml")
    for kind in ("Deployment", "StatefulSet", "CronJob", "Job", "ServiceAccount"):
        assert by_kind(resources, kind) == by_kind(original, kind)


@pytest.mark.parametrize("component", ["app", "database"])
def test_each_secret_can_independently_stay_operator_managed(component: str) -> None:
    resources = render(
        "values-external.yaml", *eso_flags(), "--set", f"externalSecrets.{component}.enabled=false"
    )
    secrets = by_kind(resources, "ExternalSecret")
    expected = "mm-bot-manager-external-db" if component == "app" else "mm-bot-manager-app"
    assert [s["spec"]["target"]["name"] for s in secrets] == [expected]
    assert (
        by_kind(resources, "Deployment")[0]
        == by_kind(render("values-external.yaml"), "Deployment")[0]
    )


@pytest.mark.parametrize("api_version", ["v1", "v1beta1"])
def test_store_api_interval_custom_names_and_versions_are_configurable(api_version: str) -> None:
    resources = render(
        "values-external.yaml",
        *eso_flags(),
        "--set-string",
        f"externalSecrets.apiVersion=external-secrets.io/{api_version}",
        "--set",
        "externalSecrets.secretStoreRef.kind=ClusterSecretStore",
        "--set",
        "externalSecrets.secretStoreRef.name=shared-vault",
        "--set",
        "externalSecrets.refreshInterval=30m",
        "--set",
        "app.existingSecret=custom-app",
        "--set",
        "database.external.existingSecret=custom-db",
        "--set-string",
        "externalSecrets.app.data.MM_TOKEN.version=2",
        "--set",
        "externalSecrets.app.data.MM_TOKEN.property=admin_pat",
    )
    secrets = by_kind(resources, "ExternalSecret")
    assert {s["spec"]["target"]["name"] for s in secrets} == {"custom-app", "custom-db"}
    for secret in secrets:
        assert secret["apiVersion"] == f"external-secrets.io/{api_version}"
        assert secret["spec"]["secretStoreRef"] == {
            "kind": "ClusterSecretStore",
            "name": "shared-vault",
        }
        assert secret["spec"]["refreshInterval"] == "30m"
    app = next(s for s in secrets if s["spec"]["target"]["name"] == "custom-app")
    admin = next(item for item in app["spec"]["data"] if item["secretKey"] == "MM_TOKEN")
    assert admin["remoteRef"] == {
        "key": "apps/mm-bot-manager/application",
        "property": "admin_pat",
        "version": "2",
    }
    env = by_kind(resources, "Deployment")[0]["spec"]["template"]["spec"]["containers"][0]["env"]
    refs = {
        item["name"]: item["valueFrom"]["secretKeyRef"]["name"]
        for item in env
        if "secretKeyRef" in item.get("valueFrom", {})
    }
    assert refs == {"MM_TOKEN": "custom-app", "MM_BOT_TOKEN": "custom-app", "DB_URL": "custom-db"}


@pytest.mark.parametrize("mode", ["external", "internal"])
@pytest.mark.parametrize("component", ["app", "database"])
def test_only_enabled_targets_require_remote_mappings(
    tmp_path: Path, mode: str, component: str
) -> None:
    data = yaml.safe_load((CHART / "examples" / f"values-eso-{mode}.yaml").read_text())
    data["externalSecrets"][component] = {"enabled": False, "data": {}}
    override = tmp_path / "mixed.yaml"
    override.write_text(yaml.safe_dump(data))
    profile = "values-external.yaml" if mode == "external" else "values-internal-longhorn.yaml"
    assert len(by_kind(render(profile, "-f", str(override)), "ExternalSecret")) == 1


@pytest.mark.parametrize(
    "setting",
    [
        "externalSecrets.secretStoreRef.name=",
        "externalSecrets.secretStoreRef.kind=ConfigMap",
        "externalSecrets.apiVersion=external-secrets.io/v2",
        "externalSecrets.refreshInterval=tomorrow",
        "externalSecrets.refreshInterval=0s",
        "externalSecrets.refreshInterval=0h0m",
        "externalSecrets.app.data.MM_TOKEN.key=",
        "externalSecrets.app.data.MM_TOKEN.value=plaintext",
        "externalSecrets.database.data.DB_URL.property=",
        "externalSecrets.unknown=true",
        "database.external.existingSecret=mm-bot-manager-app",
    ],
)
def test_invalid_eso_configuration_fails_before_installation(setting: str) -> None:
    result = subprocess.run(
        [str(HELM), "template", "mmbot", str(CHART), *eso_flags(), "--set-string", setting],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, result.stdout
    assert "externalSecrets" in result.stderr or "distinct" in result.stderr


@pytest.mark.parametrize("mode", ["external", "internal"])
def test_incomplete_or_wrong_database_keys_are_rejected(tmp_path: Path, mode: str) -> None:
    profile = "values-external.yaml" if mode == "external" else "values-internal-longhorn.yaml"
    valid = yaml.safe_load((CHART / "examples" / f"values-eso-{mode}.yaml").read_text())
    for component in ("app", "database"):
        data = valid["externalSecrets"][component]["data"]
        for key in list(data):
            broken = yaml.safe_load(yaml.safe_dump(valid))
            del broken["externalSecrets"][component]["data"][key]
            override = tmp_path / "missing.yaml"
            override.write_text(yaml.safe_dump(broken))
            with pytest.raises(subprocess.CalledProcessError):
                render(profile, "-f", str(override))
    other_mode = "internal" if mode == "external" else "external"
    with pytest.raises(subprocess.CalledProcessError):
        render(profile, *eso_flags(other_mode))
    with pytest.raises(subprocess.CalledProcessError):
        render(
            profile,
            *eso_flags(mode),
            "--set",
            "externalSecrets.app.enabled=false,externalSecrets.database.enabled=false",
        )


@pytest.mark.parametrize("operation", ["restore", "verification", "inspect"])
def test_maintenance_jobs_keep_eso_database_references(operation: str) -> None:
    template = {
        "restore": "restore-job",
        "verification": "backup-verification-job",
        "inspect": "backup-inspector-job",
    }[operation]
    flags = ["--set", f"database.internal.{operation}.enabled=true"]
    if operation != "inspect":
        flags += ["--set", f"database.internal.{operation}.file=archive.dump"]
    job = by_kind(
        render(
            "values-internal-longhorn.yaml",
            *eso_flags("internal"),
            "--set",
            "database.internal.auth.existingSecret=eso-postgres",
            *flags,
            show_only=f"templates/{template}.yaml",
        ),
        "Job",
    )[0]
    env = job["spec"]["template"]["spec"]["containers"][0].get("env", [])
    refs = [
        item["valueFrom"]["secretKeyRef"]
        for item in env
        if "secretKeyRef" in item.get("valueFrom", {})
    ]
    if operation == "inspect":
        # Archive inspection deliberately requires no database connection/credentials.
        assert not refs
    else:
        assert {ref["key"] for ref in refs} == {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}
        assert all(ref["name"] == "eso-postgres" for ref in refs)


def test_long_release_names_do_not_collide() -> None:
    result = subprocess.run(
        [str(HELM), "template", "a" * 53, str(CHART), *eso_flags()],
        capture_output=True,
        text=True,
        check=True,
    )
    names = [
        doc["metadata"]["name"]
        for doc in yaml.safe_load_all(result.stdout)
        if doc and doc["kind"] == "ExternalSecret"
    ]
    assert len(names) == len(set(names)) == 2
    assert all(len(name) <= 63 for name in names)


def test_eso_site_overlay_uses_the_existing_installation_validator(tmp_path: Path) -> None:
    values: dict[str, Any] = yaml.safe_load(
        (CHART / "examples/values-eso-external.yaml").read_text()
    )
    values.update(
        {
            "image": {"repository": "registry.example.org/team/mm-bot-manager", "tag": "test"},
            "mattermost": {"url": "https://chat.example.org"},
        }
    )
    site = tmp_path / "site.yaml"
    site.write_text(yaml.safe_dump(values))
    result = subprocess.run(
        [
            str(ROOT / "scripts/verify-kubernetes-site.sh"),
            str(CHART / "examples/values-external.yaml"),
            str(site),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("eso_schemas", [False, True])
def test_offline_verifier_covers_eso_only_with_explicit_local_schemas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, eso_schemas: bool
) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    core = tmp_path / "core-schemas"
    core.mkdir()
    eso = tmp_path / "eso-schemas"
    eso.mkdir()
    log = tmp_path / "arguments"
    helm = tools / "helm"
    helm.write_text("#!/usr/bin/env bash\nexit 0\n")
    helm.chmod(0o755)
    validator = tools / "kubeconform"
    validator.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"$SCHEMA_ARGUMENT_LOG"\n')
    validator.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setenv("KUBECONFORM_SCHEMA_LOCATION", str(core))
    monkeypatch.setenv("SCHEMA_ARGUMENT_LOG", str(log))
    if eso_schemas:
        monkeypatch.setenv("EXTERNAL_SECRETS_SCHEMA_LOCATION", str(eso))
    else:
        monkeypatch.delenv("EXTERNAL_SECRETS_SCHEMA_LOCATION", raising=False)
    result = subprocess.run(
        [str(ROOT / "scripts/verify-kubernetes.sh")], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert len(calls) == (12 if eso_schemas else 8)
    assert all(str(core) in call and "-strict" in call for call in calls)
    assert all((str(eso) in call) == eso_schemas for call in calls)
    assert not any("https://" in call or "-ignore-missing-schemas" in call for call in calls)
    assert ("ESO CRD schema validation skipped" in result.stdout) != eso_schemas
