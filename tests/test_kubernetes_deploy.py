"""Execute the saved deployment workflow with real Helm rendering and a fake cluster."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from tests.test_helm_chart import CHART, HELM, ROOT

LOCAL = Path(".local/kubernetes")


def is_install(event):
    return (
        event[0] == "helm"
        and event[1:3] == ["upgrade", "--install"]
        and "--dry-run=server" not in event
    )


@pytest.fixture()
def deployment(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    scripts = workspace / "scripts"
    scripts.mkdir(parents=True)
    for name in ("kubernetes-deploy.sh", "kubernetes-local.sh", "verify-kubernetes-site.sh"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    for name in (".gitignore", ".dockerignore"):
        shutil.copy2(ROOT / name, workspace / name)
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True, capture_output=True)
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
        if os.environ.get('DEPLOY_TEST_LEGACY_ENV') and 'apply' in a:
            print('env[6].valueFrom: may not be specified when value is not empty', file=sys.stderr)
            raise SystemExit(1)
        raise AssertionError('Unexpected kubectl mutation/patch, even in dry run: ' + repr(a))
elif a[0] == 'version':
    if failure == 'version':
        print('v3.17.4')  # Usable stdout must not mask the failed command.
        raise SystemExit(1)
    print(os.environ.get('DEPLOY_TEST_VERSION', 'v3.17.4'))
elif a[:2] == ['upgrade', '--install']:
    assert a[a.index('--kube-context') + 1] == 'test-cluster'
    assert a[a.index('--namespace') + 1] == 'mm-bot-manager'
    if '--dry-run=server' in a:
        assert '--hide-secret' in a
        assert not {'--atomic', '--rollback-on-failure', '--wait', '--wait-for-jobs'} & set(a)
        print('PREVIEW_PAYLOAD_MUST_STAY_PRIVATE')
        if failure in {'dry-run', 'render'}:
            print('simulated Helm preview failure', file=sys.stderr)
            raise SystemExit(1)
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
        config = workspace / LOCAL / "deployment.env"
        if (workspace / "deployment.env").exists():
            config = workspace / "deployment.env"
        config.write_text(
            config.read_text()
            .replace("CHANGE_ME_PROFILE", profile.removesuffix(".yaml"))
            .replace("CHANGE_ME_KUBE_CONTEXT", "test-cluster")
        )
        site = workspace / LOCAL / "values-site.yaml"
        if config == workspace / "deployment.env":
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
    config = workspace / LOCAL / "deployment.env"
    config.write_text(
        config.read_text()
        .replace("CHANGE_ME_PROFILE", "values-internal-local-path")
        .replace('site=".local/kubernetes/values-site.yaml"', 'site="custom-site.yaml"')
    )
    with (workspace / ".gitignore").open("a") as ignored:
        ignored.write("\n/custom-site.yaml\n")
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
    secret = workspace / LOCAL / f"{name}-secret.env"
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
    (workspace / LOCAL).mkdir(parents=True, exist_ok=True)
    (workspace / LOCAL / destination).symlink_to(target)
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
    config_before = (workspace / LOCAL / "deployment.env").read_bytes()
    checked = run("check")
    assert checked.returncode == 0, checked.stderr
    assert not any(is_install(event) for event in events())
    applied = run("apply")
    assert applied.returncode == 0, applied.stderr
    assert f'database-mode: "{mode}"' in applied.stdout
    if storage:
        assert f'data-storage-class: "{storage}"' in applied.stdout
        assert f'backup-storage-class: "{storage}"' in applied.stdout
    else:
        assert "data-storage-class:" not in applied.stdout
    assert (workspace / LOCAL / "deployment.env").read_bytes() == config_before
    for event in events():
        if event[0] == "helm" and "--values" in event:
            values = [event[i + 1] for i, word in enumerate(event) if word == "--values"]
            assert values == [
                f"deploy/helm/mm-bot-manager/examples/{profile}",
                ".local/kubernetes/values-site.yaml",
            ]
    assert len([event for event in events() if is_install(event)]) == 1


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
        config = workspace / LOCAL / "deployment.env"
        config.write_text(
            "\n".join(x for x in config.read_text().splitlines() if not x.startswith(f"{key}="))
        )
    else:
        (workspace / LOCAL / missing).unlink()
    assert run("apply").returncode != 0
    assert not events()


def test_validation_uses_the_saved_chart_path_too(deployment):
    workspace, run, events, configure = deployment
    configure()
    original = workspace / "deploy/helm/mm-bot-manager"
    original.rename(workspace / "deploy/helm/custom-chart")
    config = workspace / LOCAL / "deployment.env"
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
        (workspace / LOCAL / "values-site.yaml").write_text(
            (CHART / "examples/values-site.yaml.example").read_text()
        )
    else:
        monkeypatch.setenv("DEPLOY_TEST_FAIL", failure)
    result = run("apply")
    assert result.returncode != 0
    assert not any(is_install(event) for event in events())


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
    installs = [event for event in events() if is_install(event)]
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


def test_generated_files_are_ignored_and_templates_still_can_be_tracked(deployment):
    workspace, run, _, _ = deployment
    assert run("init").returncode == 0
    for name in ("app", "internal-db", "external-db"):
        assert run("prepare-secret", name).returncode == 0
    assert stat.S_IMODE((workspace / LOCAL).stat().st_mode) == 0o700
    for name in (
        "images/images.tar",
        "images/images.tar.sha256",
        "backups/archive.dump",
        "app-secret.env.bak",
        "deployment.env~",
    ):
        (workspace / LOCAL / name).write_text("disposable test artifact\n")
    for path in (workspace / LOCAL).rglob("*"):
        if path.is_file():
            checked = subprocess.run(
                ["git", "check-ignore", "--no-index", "-q", str(path)], cwd=workspace
            )
            assert checked.returncode == 0, path
    staged = subprocess.run(
        ["git", "add", "--dry-run", "."], cwd=workspace, capture_output=True, text=True, check=True
    )
    assert ".local/kubernetes/" not in staged.stdout
    assert "examples/deployment.env.example" in staged.stdout
    assert "examples/values-site.yaml.example" in staged.stdout
    assert ".local/kubernetes/" in (ROOT / ".dockerignore").read_text().splitlines()


def test_helm_package_excludes_local_files_without_excluding_examples(deployment, tmp_path):
    if HELM is None:
        pytest.skip("helm is required")
    workspace, _, _, _ = deployment
    chart = workspace / "deploy/helm/mm-bot-manager"
    private = chart / LOCAL
    private.mkdir(parents=True)
    for name in ("values-site.yaml", "app-secret.env", "images.tar", "archive.dump"):
        (private / name).write_text("private test marker\n")
    subprocess.run(
        [HELM, "package", str(chart), "--destination", str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    with tarfile.open(next(tmp_path.glob("mm-bot-manager-*.tgz"))) as archive:
        names = archive.getnames()
    assert not any("/.local/kubernetes/" in name for name in names)
    assert "mm-bot-manager/examples/deployment.env.example" in names
    assert "mm-bot-manager/examples/values-site.yaml.example" in names


def legacy_setup(workspace):
    source = CHART / "examples/deployment.env.example"
    (workspace / "deployment.env").write_text(
        source.read_text().replace(
            'site=".local/kubernetes/values-site.yaml"', 'site="values-site.yaml"'
        )
    )
    (workspace / "values-site.yaml").write_text(
        (CHART / "examples/values-site.yaml.example").read_text()
    )


@pytest.mark.parametrize("name", ["app", "internal-db", "external-db"])
def test_legacy_secret_files_are_reused_without_copying_or_chmod(deployment, name):
    workspace, run, _, _ = deployment
    legacy = workspace / f"{name}-secret.env"
    legacy.write_text("# existing local contents\n")
    legacy.chmod(0o640)
    before = legacy.read_bytes(), legacy.stat().st_mode
    assert run("init").returncode == 0
    result = run("prepare-secret", name)
    assert result.returncode == 0, result.stderr
    selected = run("secret-path", name)
    assert selected.returncode == 0 and selected.stdout.strip() == legacy.name
    assert (legacy.read_bytes(), legacy.stat().st_mode) == before
    assert not (workspace / LOCAL / legacy.name).exists()


def test_legacy_config_and_overlay_continue_working_in_place(deployment):
    workspace, run, events, configure = deployment
    legacy_setup(workspace)
    configure()
    config = workspace / "deployment.env"
    site = workspace / "values-site.yaml"
    before = config.read_bytes(), site.read_bytes()
    assert run("init").returncode == 0
    result = run("apply")
    assert result.returncode == 0, result.stderr
    assert (config.read_bytes(), site.read_bytes()) == before
    assert not (workspace / LOCAL / "deployment.env").exists()
    assert not (workspace / LOCAL / "values-site.yaml").exists()
    install = next(event for event in events() if is_install(event))
    values = [install[i + 1] for i, word in enumerate(install) if word == "--values"]
    assert values[-1] == "values-site.yaml"


@pytest.mark.parametrize("operation", ["init", "check", "apply"])
def test_two_configurations_require_explicit_selection_without_touching_either(
    deployment, operation
):
    workspace, run, _, configure = deployment
    configure()
    legacy_setup(workspace)
    files = [
        workspace / "deployment.env",
        workspace / "values-site.yaml",
        workspace / LOCAL / "deployment.env",
        workspace / LOCAL / "values-site.yaml",
    ]
    before = [p.read_bytes() for p in files]
    result = run(operation)
    assert result.returncode != 0 and "Both local and legacy" in result.stderr
    assert [p.read_bytes() for p in files] == before
    selected = run("--config", ".local/kubernetes/deployment.env", operation)
    assert selected.returncode == 0, selected.stderr
    assert [p.read_bytes() for p in files] == before


def test_two_secret_copies_require_selection_and_are_never_overwritten(deployment):
    workspace, run, _, configure = deployment
    configure()
    assert run("prepare-secret", "app").returncode == 0
    local = workspace / LOCAL / "app-secret.env"
    legacy = workspace / "app-secret.env"
    legacy.write_text("legacy local data\n")
    before = local.read_bytes(), legacy.read_bytes()
    assert run("prepare-secret", "app").returncode != 0
    assert run("secret-path", "app").returncode != 0
    for config, expected in [
        ("deployment.env", "app-secret.env"),
        (".local/kubernetes/deployment.env", ".local/kubernetes/app-secret.env"),
    ]:
        selected = run("--config", config, "secret-path", "app")
        assert selected.returncode == 0 and selected.stdout.strip() == expected
    assert (local.read_bytes(), legacy.read_bytes()) == before


@pytest.mark.parametrize("operation", ["init", "check", "apply"])
@pytest.mark.parametrize("path", ["custom-site.yaml", ".local/kubernetes/../../outside.yaml"])
def test_unignored_site_is_rejected_before_creation_or_cluster_calls(deployment, operation, path):
    workspace, run, events, configure = deployment
    configure()
    config = workspace / LOCAL / "deployment.env"
    config.write_text(
        config.read_text().replace('site=".local/kubernetes/values-site.yaml"', f'site="{path}"')
    )
    result = run(operation)
    assert result.returncode != 0 and "not ignored by Git" in result.stderr
    assert not (workspace / path).exists()
    assert not events()


def test_unignored_custom_config_is_rejected_before_creation(deployment):
    workspace, run, events, _ = deployment
    result = run("--config", "custom.env", "init")
    assert result.returncode != 0 and "not ignored by Git" in result.stderr
    assert not (workspace / "custom.env").exists()
    assert not events()


def test_explicit_ignored_custom_config_is_supported(deployment):
    workspace, run, _, _ = deployment
    with (workspace / ".gitignore").open("a") as ignore:
        ignore.write("\n/operator/config.env\n")
    result = run("--config", "operator/config.env", "init")
    assert result.returncode == 0, result.stderr
    before = (workspace / "operator/config.env").read_bytes()
    assert run("--config", "operator/config.env", "init").returncode == 0
    assert (workspace / "operator/config.env").read_bytes() == before


@pytest.mark.parametrize("tracked", ["deployment.env", "values-site.yaml", "app-secret.env"])
def test_tracked_local_files_are_rejected_even_when_ignore_rules_match(deployment, tracked):
    workspace, run, events, configure = deployment
    configure()
    if tracked == "app-secret.env":
        assert run("prepare-secret", "app").returncode == 0
    path = workspace / LOCAL / tracked
    subprocess.run(
        ["git", "add", "--force", str(path)], cwd=workspace, check=True, capture_output=True
    )
    before = path.read_bytes(), path.stat().st_mode
    operations = (
        [("prepare-secret", "app"), ("secret-path", "app")]
        if tracked == "app-secret.env"
        else [("init",), ("check",), ("apply",)]
    )
    for operation in operations:
        result = run(*operation)
        assert result.returncode != 0 and "already tracked by Git" in result.stderr
    assert (path.read_bytes(), path.stat().st_mode) == before
    assert not events()


def test_symlink_parent_cannot_redirect_new_files(deployment, tmp_path):
    workspace, run, _, _ = deployment
    target = tmp_path / "outside"
    target.mkdir()
    (workspace / ".local").mkdir()
    (workspace / LOCAL).symlink_to(target, target_is_directory=True)
    result = run("init")
    assert result.returncode != 0 and "Symlink paths" in result.stderr
    assert not list(target.iterdir())


def test_external_site_outside_any_checkout_is_supported(deployment, tmp_path):
    workspace, run, _, configure = deployment
    configure()
    config = workspace / LOCAL / "deployment.env"
    external = tmp_path / "protected config" / "site.yaml"
    config.write_text(
        config.read_text().replace(
            'site=".local/kubernetes/values-site.yaml"', f'site="{external}"'
        )
    )
    result = run("init")
    assert result.returncode == 0, result.stderr
    assert external.is_file() and stat.S_IMODE(external.stat().st_mode) == 0o600


@pytest.mark.parametrize("state", ["unignored", "tracked", "ignored"])
def test_external_site_inside_another_repo_obeys_its_index_and_ignore_rules(
    deployment, tmp_path, state
):
    workspace, run, events, configure = deployment
    configure()
    external_repo = tmp_path / "other-checkout"
    external_repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(external_repo)], check=True, capture_output=True)
    external = external_repo / "site.yaml"
    external.write_text("# existing operator file\n")
    if state != "unignored":
        (external_repo / ".gitignore").write_text("/site.yaml\n")
    if state == "tracked":
        subprocess.run(["git", "add", "--force", "site.yaml"], cwd=external_repo, check=True)
    config = workspace / LOCAL / "deployment.env"
    config.write_text(
        config.read_text().replace(
            'site=".local/kubernetes/values-site.yaml"', f'site="{external}"'
        )
    )
    result = run("init")
    assert (result.returncode == 0) == (state == "ignored"), result.stderr
    assert external.read_text() == "# existing operator file\n"
    assert not events()


def test_source_archive_uses_disposable_git_metadata_without_initializing_the_sources(deployment):
    workspace, run, _, _ = deployment
    shutil.rmtree(workspace / ".git")
    result = run("init")
    assert result.returncode == 0, result.stderr
    assert (workspace / LOCAL / "deployment.env").is_file()
    assert not (workspace / ".git").exists()
    assert run("prepare-secret", "app").returncode == 0


def test_secret_path_missing_file_fails_without_creating_it(deployment):
    workspace, run, events, _ = deployment
    result = run("secret-path", "app")
    assert result.returncode != 0 and "run prepare-secret app first" in result.stderr
    assert not (workspace / LOCAL / "app-secret.env").exists()
    assert not events()


def test_legacy_exports_and_editor_backups_stay_ignored_and_in_place(deployment):
    workspace, run, _, _ = deployment
    names = [
        "images.tar",
        "images.tar.sha256",
        "mm-bot-manager-old.dump",
        "app-secret.env.bak",
        "internal-db-secret.env~",
        "deployment.env.bak",
        "deployment.env~",
        "values-site.yaml.bak",
        "values-site.yaml~",
    ]
    for name in names:
        (workspace / name).write_text("existing operator data\n")
        ignored = subprocess.run(["git", "check-ignore", "--no-index", "-q", name], cwd=workspace)
        assert ignored.returncode == 0, name
    before = [(workspace / name).read_bytes() for name in names]
    assert run("init").returncode == 0
    assert [(workspace / name).read_bytes() for name in names] == before
    assert not (workspace / LOCAL / "images/images.tar").exists()


def test_forced_archive_in_local_directory_blocks_install_without_untracking_it(deployment):
    workspace, run, events, configure = deployment
    configure()
    archive = workspace / LOCAL / "images/images.tar"
    archive.write_text("private operator export\n")
    subprocess.run(["git", "add", "--force", str(archive)], cwd=workspace, check=True)
    result = run("apply")
    assert result.returncode != 0 and "already tracked by Git" in result.stderr
    assert archive.read_text() == "private operator export\n"
    assert not events()


@pytest.mark.parametrize("damage", ["index", "git-link"])
def test_git_damage_stops_writing_instead_of_treating_checkout_as_source_archive(
    deployment, damage
):
    workspace, run, _, _ = deployment
    if damage == "index":
        (workspace / ".git/index").write_bytes(b"broken disposable test index")
    else:
        shutil.rmtree(workspace / ".git")
        (workspace / ".git").symlink_to(workspace / "missing-git-metadata")
    result = run("init")
    assert result.returncode != 0
    assert "preserve this checkout" in result.stderr
    assert not (workspace / LOCAL).exists()


@pytest.mark.parametrize("kind", ["app", "internal-db", "external-db"])
@pytest.mark.parametrize("legacy", [False, True])
def test_documented_secret_commands_resolve_new_and_legacy_files(
    deployment, monkeypatch, kind, legacy
):
    workspace, run, _, _ = deployment
    if legacy:
        legacy_setup(workspace)
    assert run("init").returncode == 0
    assert run("prepare-secret", kind).returncode == 0
    selected = run("secret-path", kind)
    assert selected.returncode == 0
    path = selected.stdout.strip()
    expected = f"{kind}-secret.env" if legacy else f".local/kubernetes/{kind}-secret.env"
    assert path == expected
    before = (workspace / path).read_bytes()
    monkeypatch.setenv("DOC_SECRET_EXPECTED_PATH", expected)
    kubectl = workspace.parent / "bin/kubectl"
    kubectl.write_text(
        f"#!{sys.executable}\n"
        + r"""
import os, sys
args = sys.argv[1:]
if 'create' in args:
    assert '--from-env-file=' + os.environ['DOC_SECRET_EXPECTED_PATH'] in args
    print('kind: Secret')
else:
    assert args == ['apply', '-f', '-']
    assert sys.stdin.read().strip() == 'kind: Secret'
"""
    )
    text = (ROOT / "docs/kubernetes.md").read_text()
    block = next(
        section.split("```", 1)[0]
        for section in text.split("```bash\n")[1:]
        if f"secret-path {kind})" in section.split("```", 1)[0]
    )
    result = run(block=block)
    assert result.returncode == 0, result.stderr
    assert (workspace / path).read_bytes() == before


def test_nested_checkout_cannot_hide_tracked_configuration_behind_outer_ignore(deployment):
    workspace, run, events, configure = deployment
    configure()
    nested = workspace / LOCAL
    subprocess.run(["git", "init", "--quiet", str(nested)], check=True, capture_output=True)
    subprocess.run(["git", "add", "deployment.env"], cwd=nested, check=True)
    result = run("apply")
    assert result.returncode != 0 and "already tracked by Git" in result.stderr
    assert not events()


def test_external_damaged_checkout_is_not_treated_as_unversioned_storage(deployment, tmp_path):
    workspace, run, _, configure = deployment
    configure()
    damaged = tmp_path / "damaged-external-repo"
    damaged.mkdir()
    (damaged / ".git").write_text("not valid Git metadata\n")
    site = damaged / "site.yaml"
    config = workspace / LOCAL / "deployment.env"
    config.write_text(
        config.read_text().replace('site=".local/kubernetes/values-site.yaml"', f'site="{site}"')
    )
    result = run("init")
    assert result.returncode != 0 and "preserve this checkout" in result.stderr
    assert not site.exists()


@pytest.mark.parametrize("operation", ["check", "apply"])
@pytest.mark.parametrize("preview_fails", [False, True])
def test_helm_preview_handles_legacy_env_without_kubectl_apply_or_manifest_output(
    deployment, monkeypatch, operation, preview_fails
):
    workspace, run, events, configure = deployment
    configure("values-external.yaml")
    monkeypatch.setenv("DEPLOY_TEST_LEGACY_ENV", "1")
    if preview_fails:
        monkeypatch.setenv("DEPLOY_TEST_FAIL", "dry-run")
    local_files = {
        name: (workspace / LOCAL / name).read_bytes()
        for name in ("deployment.env", "values-site.yaml")
    }

    result = run(operation)

    assert (result.returncode != 0) == preview_fails, result.stderr
    assert "PREVIEW_PAYLOAD_MUST_STAY_PRIVATE" not in result.stdout + result.stderr
    assert "env[6].valueFrom" not in result.stderr
    assert [event for event in events() if event[0] == "kubectl"] == [
        ["kubectl", "config", "current-context"]
    ]
    previews = [event for event in events() if event[0] == "helm" and "--dry-run=server" in event]
    assert len(previews) == 1
    assert "--hide-secret" in previews[0]
    assert previews[0][previews[0].index("--namespace") + 1] == "mm-bot-manager"
    assert previews[0][previews[0].index("--kube-context") + 1] == "test-cluster"
    installs = [event for event in events() if is_install(event)]
    assert len(installs) == (1 if operation == "apply" and not preview_fails else 0)
    for name, contents in local_files.items():
        assert (workspace / LOCAL / name).read_bytes() == contents
