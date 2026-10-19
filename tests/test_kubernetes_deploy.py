"""Execute the saved deployment workflow with real Helm rendering and a fake cluster."""

import json
import os
import shutil
import stat
import subprocess
import sys

import pytest

from tests.test_helm_chart import CHART, HELM, ROOT


@pytest.fixture()
def deployment(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    scripts = workspace / "scripts"
    scripts.mkdir(parents=True)
    for name in ("kubernetes-deploy.sh", "verify-kubernetes-site.sh"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    chart = workspace / "deploy/helm/mm-bot-manager"
    shutil.copytree(CHART, chart)
    tools = tmp_path / "bin"
    tools.mkdir()
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("DEPLOY_TEST_AUDIT", str(audit))
    monkeypatch.setenv("REAL_HELM", HELM or "")
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    fake = r"""
import json, os, subprocess, sys
from pathlib import Path
name = Path(sys.argv[0]).name
a = sys.argv[1:]
with Path(os.environ['DEPLOY_TEST_AUDIT']).open('a') as f:
    f.write(json.dumps([name, *a]) + '\n')
failure = os.environ.get('DEPLOY_TEST_FAIL', '')
if name == 'kubectl':
    if a == ['config', 'current-context']:
        print(os.environ.get('DEPLOY_TEST_CONTEXT', 'test-cluster'))
    else:
        assert '--dry-run=server' in a, 'Unexpected cluster mutation: ' + repr(a)
        assert a[a.index('--context') + 1] == 'test-cluster'
        assert a[a.index('-n') + 1] == 'mm-bot-manager'
        sys.stdin.read()
        if failure == 'dry-run': raise SystemExit(1)
elif a[0] == 'version':
    if failure == 'version':
        print('v3.17.4')  # Usable stdout must not mask the failed command.
        raise SystemExit(1)
    print(os.environ.get('DEPLOY_TEST_VERSION', 'v3.17.4'))
elif a[:2] == ['upgrade', '--install']:
    assert a[a.index('--kube-context') + 1] == 'test-cluster'
else:
    if failure == 'lint' and a[0] == 'lint': raise SystemExit(1)
    if a[0] == 'template':
        summary = 'deployment-summary' in (Path(a[2]) / 'Chart.yaml').read_text()
        if failure == 'summary' and summary: raise SystemExit(1)
        # Fail the installation render, after successful validator + summary.
        if failure == 'render' and not summary and '--kube-version' not in a:
            raise SystemExit(1)
    raise SystemExit(subprocess.run([os.environ['REAL_HELM'], *a], check=False).returncode)
"""
    for name in ("helm", "kubectl"):
        executable = tools / name
        executable.write_text(f"#!{sys.executable}\n" + fake)
        executable.chmod(0o755)

    def run(*args, block=None):
        return subprocess.run(
            ["bash", "-c", block] if block else [str(scripts / "kubernetes-deploy.sh"), *args],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def events():
        return (
            [json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []
        )

    def configure(profile="values-internal-local-path.yaml", *, overlay=""):
        if HELM is None:
            pytest.skip("helm is required")
        result = run("init")
        assert result.returncode == 0, result.stderr
        config = workspace / "deployment.env"
        config.write_text(
            config.read_text()
            .replace("CHANGE_ME_PROFILE", profile.removesuffix(".yaml"))
            .replace("CHANGE_ME_KUBE_CONTEXT", "test-cluster")
        )
        site = workspace / "values-site.yaml"
        site.write_text(
            site.read_text()
            .replace("CHANGE_ME_REGISTRY", "registry.test")
            .replace("CHANGE_ME_IMMUTABLE_TAG", "build-0123456789ab")
            .replace("CHANGE_ME_MATTERMOST", "mattermost.test")
            .replace("CHANGE_ME_APPROVER_USERNAME", "reviewer")
            .replace("CHANGE_ME_CATALOG_VIEWER_USERNAME", "auditor")
            + overlay
        )

    return workspace, run, events, configure


def test_repeated_init_preserves_selection_overlay_and_custom_site_path(deployment):
    workspace, run, events, _ = deployment
    assert run("init").returncode == 0
    config = workspace / "deployment.env"
    config.write_text(
        config.read_text()
        .replace("CHANGE_ME_PROFILE", "values-internal-local-path")
        .replace('site="values-site.yaml"', 'site="custom-site.yaml"')
    )
    site = workspace / "custom-site.yaml"
    site.write_text("# existing operator configuration\n")
    site.chmod(0o640)
    before = config.read_bytes(), site.read_bytes(), site.stat().st_mode
    assert run("init").returncode == 0
    assert (config.read_bytes(), site.read_bytes(), site.stat().st_mode) == before
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert not events()  # Preparation never needs Helm or Kubernetes.


@pytest.mark.parametrize("name", ["app", "internal-db", "external-db"])
def test_secret_preparation_creates_private_files_and_never_overwrites_them(deployment, name):
    workspace, run, events, _ = deployment
    assert run("prepare-secret", name).returncode == 0
    secret = workspace / f"{name}-secret.env"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    secret.write_text("# operator-owned contents\n")
    secret.chmod(0o640)
    before = secret.read_bytes(), secret.stat().st_mode
    assert run("prepare-secret", name).returncode == 0
    assert (secret.read_bytes(), secret.stat().st_mode) == before
    assert not events()


@pytest.mark.parametrize("operation", ["init", "prepare-secret"])
def test_preparation_refuses_symlinks_without_touching_their_targets(deployment, operation):
    workspace, run, _, _ = deployment
    target = workspace / "operator-file"
    target.write_text("preserve me")
    destination = "deployment.env" if operation == "init" else "app-secret.env"
    (workspace / destination).symlink_to(target)
    args = (operation,) if operation == "init" else (operation, "app")
    result = run(*args)
    assert result.returncode != 0
    assert target.read_text() == "preserve me"


@pytest.mark.parametrize("operation", ["check", "apply"])
def test_missing_or_unedited_config_cannot_use_an_inherited_profile(
    deployment, monkeypatch, operation
):
    _, run, events, _ = deployment
    monkeypatch.setenv("profile", str(CHART / "examples/values-internal-longhorn.yaml"))
    assert run(operation).returncode != 0
    assert not events()
    assert run("init").returncode == 0
    result = run(operation)
    assert result.returncode != 0 and "Choose profile and context" in result.stderr
    assert not events()


@pytest.mark.parametrize(
    "profile,mode,storage",
    [
        ("values-internal-local-path.yaml", "internal", "local-path"),
        ("values-internal-longhorn.yaml", "internal", "longhorn"),
        ("values-harbor.yaml", "internal", "longhorn"),
        ("values-external.yaml", "external", None),
    ],
)
def test_check_then_apply_in_new_shells_preserve_the_saved_profile(
    deployment, monkeypatch, profile, mode, storage
):
    workspace, run, events, configure = deployment
    configure(profile)
    monkeypatch.setenv("profile", "incorrect-inherited-profile.yaml")
    monkeypatch.setenv("context", "incorrect-inherited-context")
    config_before = (workspace / "deployment.env").read_bytes()
    checked = run("check")
    assert checked.returncode == 0, checked.stderr
    assert not any(event[1:3] == ["upgrade", "--install"] for event in events())
    applied = run("apply")
    assert applied.returncode == 0, applied.stderr
    assert f'database-mode: "{mode}"' in applied.stdout
    if storage:
        assert f'data-storage-class: "{storage}"' in applied.stdout
        assert f'backup-storage-class: "{storage}"' in applied.stdout
    else:
        assert "data-storage-class:" not in applied.stdout
    assert (workspace / "deployment.env").read_bytes() == config_before
    for event in events():
        if event[0] == "helm" and "--values" in event:
            values = [event[i + 1] for i, word in enumerate(event) if word == "--values"]
            assert values == [f"deploy/helm/mm-bot-manager/examples/{profile}", "values-site.yaml"]
    assert len([event for event in events() if event[1:3] == ["upgrade", "--install"]]) == 1


def test_summary_uses_site_overrides_and_omits_disabled_backup_storage(deployment):
    _, run, _, configure = deployment
    configure(
        overlay="\ndatabase:\n  internal:\n    storage:\n      storageClass: custom-data\n"
        "    backup:\n      enabled: false\n      storageClass: unused-backup\n"
    )
    result = run("check")
    assert result.returncode == 0, result.stderr
    assert 'data-storage-class: "custom-data"' in result.stdout
    assert 'backups-enabled: "false"' in result.stdout
    assert "backup-storage-class:" not in result.stdout


@pytest.mark.parametrize(
    "missing", ["deployment.env", "values-site.yaml", "profile", "profile-key", "context-key"]
)
def test_missing_configuration_stops_before_cluster_calls(deployment, monkeypatch, missing):
    workspace, run, events, configure = deployment
    configure()
    if missing == "profile":
        (workspace / "deploy/helm/mm-bot-manager/examples/values-internal-local-path.yaml").unlink()
    elif missing.endswith("-key"):
        key = missing.removesuffix("-key")
        monkeypatch.setenv(key, "inherited-value-must-not-be-used")
        config = workspace / "deployment.env"
        config.write_text(
            "\n".join(x for x in config.read_text().splitlines() if not x.startswith(f"{key}="))
        )
    else:
        (workspace / missing).unlink()
    assert run("apply").returncode != 0
    assert not events()


def test_validation_uses_the_saved_chart_path_too(deployment):
    workspace, run, events, configure = deployment
    configure()
    original = workspace / "deploy/helm/mm-bot-manager"
    original.rename(workspace / "deploy/helm/custom-chart")
    config = workspace / "deployment.env"
    config.write_text(
        config.read_text().replace("deploy/helm/mm-bot-manager", "deploy/helm/custom-chart")
    )
    result = run("apply")
    assert result.returncode == 0, result.stderr
    lint = next(event for event in events() if event[:2] == ["helm", "lint"])
    assert lint[2] == "deploy/helm/custom-chart"


def test_changed_context_stops_before_any_render_or_install(deployment, monkeypatch):
    _, run, events, configure = deployment
    configure()
    monkeypatch.setenv("DEPLOY_TEST_CONTEXT", "other-cluster")
    result = run("apply")
    assert result.returncode != 0 and "Kube-context mismatch" in result.stderr
    assert events() == [["kubectl", "config", "current-context"]]


@pytest.mark.parametrize(
    "failure", ["version", "lint", "summary", "render", "dry-run", "placeholders"]
)
def test_failed_check_never_reaches_helm_install(deployment, monkeypatch, failure):
    workspace, run, events, configure = deployment
    configure()
    if failure == "placeholders":
        (workspace / "values-site.yaml").write_text(
            (CHART / "examples/values-site.yaml.example").read_text()
        )
    else:
        monkeypatch.setenv("DEPLOY_TEST_FAIL", failure)
    result = run("apply")
    assert result.returncode != 0
    assert not any(event[1:3] == ["upgrade", "--install"] for event in events())


@pytest.mark.parametrize("document", ["docs/kubernetes.md", "deploy/helm/mm-bot-manager/README.md"])
@pytest.mark.parametrize(
    "version,rollback_flag",
    [("v3.17.4", "--atomic"), ("v4.1.4", "--rollback-on-failure"), ("v2.17.0", None)],
)
def test_documented_install_uses_saved_profile_and_waits_for_jobs(
    deployment, monkeypatch, document, version, rollback_flag
):
    _, run, events, configure = deployment
    configure()
    monkeypatch.setenv("DEPLOY_TEST_VERSION", version)
    text = (ROOT / document).read_text()
    block = next(
        section.split("```", 1)[0]
        for section in text.split("```bash\n")[1:]
        if "./scripts/kubernetes-deploy.sh apply" in section.split("```", 1)[0]
    )
    result = run(block=block)
    installs = [event for event in events() if event[1:3] == ["upgrade", "--install"]]
    if rollback_flag is None:
        assert result.returncode != 0 and not installs
    else:
        assert result.returncode == 0, result.stderr
        assert len(installs) == 1
        assert {rollback_flag, "--wait", "--wait-for-jobs", "--timeout=10m"} <= set(installs[0])
        assert ("--atomic" in installs[0]) != ("--rollback-on-failure" in installs[0])


def test_documentation_has_no_install_profile_reset_or_overwriting_copy():
    text = (ROOT / "docs/kubernetes.md").read_text()
    install = text.split("## 4. Validate", 1)[1].split("## 5.", 1)[0]
    assert 'profile="' not in install
    assert "cp " not in install
    assert "cp deploy/helm/mm-bot-manager/examples/" not in text
    for section in text.split("```bash\n")[1:]:
        block = section.split("```", 1)[0]
        if "prepare-secret" in block:
            assert "kubectl" not in block  # An explicit edit step separates preparation and apply.


def test_local_deployment_file_is_excluded_from_git_and_builds():
    result = subprocess.run(
        ["git", "check-ignore", "deployment.env"], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "*.env" in (ROOT / ".dockerignore").read_text().splitlines()
    assert "*.env" in (CHART / ".helmignore").read_text().splitlines()
