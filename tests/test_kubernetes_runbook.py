"""Exercise operator workflows against a stateful CLI fake, including uncertain API outcomes."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.test_helm_chart import CHART, HELM, ROOT

SCRIPT = ROOT / "scripts/kubernetes-db.sh"

FAKE_KUBECTL = r"""
import json, os, sys
from pathlib import Path
import yaml
path = Path(os.environ['FAKE_KUBE_STATE'])
s = json.loads(path.read_text())
a = sys.argv[1:]
assert a[:2] == ['-n', 'test-space']
a = a[2:]
s['events'].append(a)
def done(value='', code=0):
    path.write_text(json.dumps(s))
    print(value, end='')
    raise SystemExit(code)
if a[:2] == ['create', 'configmap']:
    if s['lock']: done('lock already exists', 1)
    s['lock'] = True
    literals = [x for x in a if x.startswith('--from-literal=')]
    s['lock_data'] = dict(x.removeprefix('--from-literal=').split('=', 1) for x in literals)
    done()
if a[:2] == ['delete', 'configmap']:
    s['lock'] = False
    done()
if a[:2] == ['get', 'cronjob']:
    if '-l' in a: done('cronjob.batch/mm-bot-manager-backup')
    done('true' if s['suspended'] else 'false')
if a[:2] == ['patch', 'cronjob']:
    s['suspended'] = json.loads(a[a.index('-p') + 1])['spec']['suspend']
    if s.get('patch_unknown') and s['suspended']: done('response lost', 1)
    done()
if a[:2] == ['get', 'deployment']:
    done('deployment.apps/mm-bot-manager')
if a[0] == 'get' and a[1].startswith('deployment.apps/'):
    done(str(s['replicas']))
if a[:2] == ['get', 'jobs']:
    selector = a[a.index('-l') + 1]
    if 'in (' in selector: done(s.get('active_maintenance', ''))
    count = s.get('active_backup_polls', 0)
    s['active_backup_polls'] = max(0, count - 1)
    done('previous-backup' if count else '')
if a[0] == 'scale':
    s['replicas'] = int(a[-1].split('=')[1])
    if s['replicas']: s['pods'] = True
    done()
if a[:2] == ['rollout', 'status']:
    # Controller convergence intentionally does not mean the terminating Pod is gone.
    done()
if a[:2] == ['get', 'pods']:
    assert 'app.kubernetes.io/component=application' in a[a.index('-l') + 1]
    done('pod/mm-bot-manager-terminating' if s['pods'] else '')
if a[0] == 'wait' and '--for=delete' in a:
    assert 'app.kubernetes.io/component=application' in a[a.index('-l') + 1]
    if s.get('pod_wait_timeout'): done('Pod still terminating', 1)
    if not s.get('pod_still_present'): s['pods'] = False
    done()
if a[:2] == ['create', '-f']:
    doc = yaml.safe_load(Path(a[2]).read_text())
    kind = doc['metadata']['labels']['app.kubernetes.io/component']
    kind = {'inspection': 'inspect'}.get(kind, kind)
    name = doc['metadata']['name']
    s['rendered_jobs'].append(doc)
    s['jobs'][name] = kind
    if s.get('create_unknown') == kind: done('creation response lost', 1)
    done(name)
if a[:2] == ['create', 'job']:
    name = next(x for x in a[2:] if x.startswith('mm-bot-manager-manual-'))
    s['jobs'][name] = 'backup'
    done(name)
if a[0] == 'wait':
    name = next(x.removeprefix('job/') for x in a if x.startswith('job/'))
    kind = s['jobs'][name]
    state = s.get('job_results', {}).get(kind, 'success')
    done('complete' if state == 'success' else 'not complete', 0 if state == 'success' else 1)
if a[0] == 'get' and a[1].startswith('job/'):
    kind = s['jobs'][a[1].removeprefix('job/')]
    done('terminal' if s.get('job_results', {}).get(kind) == 'failed' else '')
if a[0] == 'logs': done('job logs\n')
if a[0] == 'delete' and a[1].startswith('job/'):
    assert '--cascade=foreground' in a and '--wait=true' in a
    name = a[1].removeprefix('job/')
    if s.get('delete_unknown') == s['jobs'][name]: done('deletion response lost', 1)
    del s['jobs'][name]
    done()
done('Unexpected kubectl command: ' + repr(a), 2)
"""

FAKE_HELM = r"""
import json, os, subprocess, sys
from pathlib import Path
path = Path(os.environ['FAKE_KUBE_STATE'])
s = json.loads(path.read_text())
s['events'].append(['helm', *sys.argv[1:]])
path.write_text(json.dumps(s))
if sys.argv[1] == 'get':
    print(json.dumps(s['installed']))
else:
    result = subprocess.run([os.environ['REAL_HELM'], *sys.argv[1:]], check=False)
    raise SystemExit(result.returncode)
"""


@pytest.fixture()
def workflow(tmp_path, monkeypatch):
    if HELM is None:
        pytest.skip("helm is required")
    tools = tmp_path / "bin"
    tools.mkdir()
    for name, source in (("kubectl", FAKE_KUBECTL), ("helm", FAKE_HELM)):
        executable = tools / name
        executable.write_text(f"#!{sys.executable}\n" + source)
        executable.chmod(0o755)
    (tools / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n")
    (tools / "sleep").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setenv("REAL_HELM", HELM)
    state_file = tmp_path / "state.json"
    monkeypatch.setenv("FAKE_KUBE_STATE", str(state_file))

    def invoke(operation, *, extra=(), **changes):
        installed = yaml.safe_load((CHART / "examples/values-harbor.yaml").read_text())
        installed["image"]["digest"] = "sha256:" + "a" * 64
        installed["database"]["internal"]["image"]["digest"] = "sha256:" + "b" * 64
        installed["database"]["internal"]["auth"]["existingSecret"] = "site-postgresql"
        state = {
            "installed": installed,
            "events": [],
            "lock": False,
            "suspended": False,
            "replicas": 1,
            "pods": True,
            "jobs": {},
            "rendered_jobs": [],
            **changes,
        }
        state_file.write_text(json.dumps(state))
        args = [
            "bash",
            str(SCRIPT),
            operation,
            "--namespace",
            "test-space",
            "--release",
            "mm-bot-manager",
        ]
        if operation in {"verify", "restore"}:
            args.extend(["--file", "mm-bot-manager-20261017T020000Z-uid.dump"])
        if operation == "restore":
            args.append("--confirm-replace-database")
        result = subprocess.run([*args, *extra], capture_output=True, text=True, timeout=15)
        return result, json.loads(state_file.read_text())

    return invoke


def event_index(state, prefix):
    return next(i for i, event in enumerate(state["events"]) if event[: len(prefix)] == prefix)


def test_restore_waits_for_pod_deletion_and_preserves_installed_site_configuration(workflow):
    result, state = workflow("restore", active_backup_polls=1)
    assert result.returncode == 0, result.stderr
    created = state["rendered_jobs"]
    assert [x["metadata"]["labels"]["app.kubernetes.io/component"] for x in created] == [
        "verification",
        "restore",
    ]
    for doc in created:
        pod = doc["spec"]["template"]["spec"]
        assert pod["imagePullSecrets"] == [{"name": "harbor-pull"}]
        assert pod["containers"][0]["image"].endswith("@sha256:" + "b" * 64)
        env = {x["name"]: x for x in pod["containers"][0]["env"]}
        assert env["PGPASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == "site-postgresql"
    assert event_index(state, ["delete", "job/mm-bot-manager-verify"]) < event_index(
        state, ["scale", "deployment.apps/mm-bot-manager", "--replicas=0"]
    )
    deletion = event_index(state, ["wait", "--for=delete"])
    creations = [i for i, e in enumerate(state["events"]) if e[:2] == ["create", "-f"]]
    assert creations[0] < deletion < creations[1]
    assert state["replicas"] == 1 and not state["suspended"] and not state["lock"]
    assert not state["jobs"]
    assert state["lock_data"]["backupWasSuspended"] == "false"


@pytest.mark.parametrize("operation", ["backup", "inspect", "verify"])
def test_maintenance_operations_wait_finish_and_release_only_their_lock(workflow, operation):
    result, state = workflow(operation)
    assert result.returncode == 0, result.stderr
    assert not state["suspended"] and not state["lock"] and not state["jobs"]
    assert not any(e[0] == "scale" for e in state["events"])
    assert event_index(state, ["wait", "--for=condition=complete"]) < event_index(state, ["logs"])
    if operation == "inspect":
        pod = state["rendered_jobs"][0]["spec"]["template"]["spec"]
        assert pod["imagePullSecrets"] == [{"name": "harbor-pull"}]
        assert pod["volumes"][0]["persistentVolumeClaim"]["readOnly"]


@pytest.mark.parametrize("condition", ["pod_wait_timeout", "pod_still_present"])
def test_restore_never_starts_while_an_application_pod_remains(workflow, condition):
    result, state = workflow("restore", **{condition: True})
    assert result.returncode != 0
    assert len(state["rendered_jobs"]) == 1  # Verification only.
    assert state["replicas"] == 0
    assert not state["lock"] and not state["suspended"]  # No destructive work started.


def test_failed_verification_never_stops_application_or_starts_restore(workflow):
    result, state = workflow("restore", job_results={"verification": "failed"})
    assert result.returncode != 0
    assert not any(e[0] == "scale" for e in state["events"])
    assert state["replicas"] == 1 and not state["lock"] and not state["suspended"]


@pytest.mark.parametrize(
    "changes",
    [
        {"job_results": {"restore": "running"}},
        {"job_results": {"restore": "failed"}},
        {"create_unknown": "restore"},
        {"delete_unknown": "restore"},
    ],
)
def test_uncertain_or_failed_restore_keeps_application_stopped_backups_paused_and_lock(
    workflow, changes
):
    result, state = workflow("restore", **changes)
    assert result.returncode != 0
    assert state["replicas"] == 0 and state["suspended"] and state["lock"]
    assert "lock mm-bot-manager-db-maintenance retained" in result.stderr


def test_existing_suspension_and_zero_replicas_are_preserved(workflow):
    result, state = workflow("restore", suspended=True, replicas=0, pods=False)
    assert result.returncode == 0, result.stderr
    assert state["suspended"] and state["replicas"] == 0 and not state["lock"]
    assert not any(e[:2] == ["patch", "cronjob"] for e in state["events"])
    assert state["lock_data"]["backupWasSuspended"] == "true"


def test_unknown_suspend_patch_does_not_release_recovery_lock(workflow):
    result, state = workflow("inspect", patch_unknown=True)
    assert result.returncode != 0 and state["lock"] and state["suspended"]
    assert not state["rendered_jobs"]


def test_another_operators_lock_is_never_removed(workflow):
    result, state = workflow("inspect", lock=True)
    assert result.returncode != 0 and state["lock"]
    assert not any(
        e[:2] in (["patch", "cronjob"], ["delete", "configmap"]) for e in state["events"]
    )


def test_existing_maintenance_job_is_not_deleted_or_overlapped(workflow):
    result, state = workflow("inspect", active_maintenance="old-restore")
    assert result.returncode != 0 and not state["rendered_jobs"]
    assert not state["suspended"] and not state["lock"]


@pytest.mark.parametrize("archive", ["../archive.dump", "archive", "x.dump;echo bad", ""])
def test_unsafe_archive_is_rejected_before_any_api_call(workflow, archive):
    result, state = workflow("verify", extra=["--file", archive])
    assert result.returncode != 0 and not state["events"]


def test_restore_requires_explicit_acknowledgement(tmp_path):
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "restore",
            "--namespace",
            "test",
            "--release",
            "test",
            "--file",
            "safe.dump",
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode != 0 and "--confirm-replace-database" in result.stderr


def test_site_overlay_must_replace_example_defaults(tmp_path):
    if HELM is None:
        pytest.skip("helm is required")
    site = tmp_path / "site.yaml"
    site.write_text((CHART / "examples/values-site.yaml.example").read_text())
    command = [
        str(ROOT / "scripts/verify-kubernetes-site.sh"),
        str(CHART / "examples/values-internal-longhorn.yaml"),
        str(site),
    ]
    rejected = subprocess.run(command, capture_output=True, text=True)
    assert rejected.returncode != 0 and "Replace all example" in rejected.stderr
    text = (
        site.read_text()
        .replace("CHANGE_ME_REGISTRY", "registry.test")
        .replace("CHANGE_ME_IMMUTABLE_TAG", "build-0123456789ab")
        .replace("CHANGE_ME_MATTERMOST", "mattermost.test")
        .replace("CHANGE_ME_APPROVER_USERNAME", "reviewer")
        .replace("CHANGE_ME_CATALOG_VIEWER_USERNAME", "auditor")
    )
    site.write_text(text)
    accepted = subprocess.run(command, capture_output=True, text=True)
    assert accepted.returncode == 0, accepted.stderr


def test_runbook_commands_have_valid_bash_and_no_password_argument():
    text = (ROOT / "docs/kubernetes.md").read_text()
    assert "--docker-password" not in text
    assert "--password-stdin" in text and ".dockerconfigjson" in text
    for section in text.split("```bash\n")[1:]:
        result = subprocess.run(
            ["bash", "-n"], input=section.split("```", 1)[0], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
    assert "verify-kubernetes-site.sh" in text and "kubernetes-db.sh restore" in text
    assert "health" in text and "not a connected/authenticated" in text


@pytest.mark.parametrize("document", ["docs/kubernetes.md", "deploy/helm/mm-bot-manager/README.md"])
@pytest.mark.parametrize(
    "version,rollback_flag",
    [("v3.17.4", "--atomic"), ("v4.1.4", "--rollback-on-failure"), ("v2.17.0", None)],
)
def test_documented_install_waits_for_jobs_with_the_correct_helm_rollback_flag(
    tmp_path, monkeypatch, document, version, rollback_flag
):
    tools = tmp_path / "bin"
    tools.mkdir()
    audit = tmp_path / "helm-upgrade.json"
    monkeypatch.setenv("INSTALL_TEST_VERSION", version)
    monkeypatch.setenv("INSTALL_TEST_AUDIT", str(audit))
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    helm = tools / "helm"
    helm.write_text(
        f"#!{sys.executable}\n"
        + r"""
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[0] == 'version':
    print(os.environ['INSTALL_TEST_VERSION'])
elif args[:2] == ['upgrade', '--install']:
    Path(os.environ['INSTALL_TEST_AUDIT']).write_text(json.dumps(args))
elif args[0] != 'template':
    raise SystemExit('Unexpected Helm command: ' + repr(args))
"""
    )
    helm.chmod(0o755)
    kubectl = tools / "kubectl"
    kubectl.write_text("#!/bin/sh\ncat >/dev/null\n")
    kubectl.chmod(0o755)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    validator = scripts / "verify-kubernetes-site.sh"
    validator.write_text("#!/bin/sh\nexit 0\n")
    validator.chmod(0o755)
    text = (ROOT / document).read_text()
    block = next(
        section.split("```", 1)[0]
        for section in text.split("```bash\n")[1:]
        if "helm upgrade --install" in section.split("```", 1)[0]
    )
    result = subprocess.run(
        ["bash", "-c", block], cwd=tmp_path, capture_output=True, text=True, timeout=10
    )
    if rollback_flag is None:
        assert result.returncode != 0 and not audit.exists()
        return
    assert result.returncode == 0, result.stderr
    arguments = json.loads(audit.read_text())
    assert {rollback_flag, "--wait", "--wait-for-jobs", "--timeout=10m"} <= set(arguments)
    assert ("--atomic" in arguments) != ("--rollback-on-failure" in arguments)


@pytest.mark.parametrize(
    "operation,kind", [("backup", "backup"), ("inspect", "inspect"), ("verify", "verification")]
)
def test_unknown_maintenance_job_does_not_resume_conflicting_backup(workflow, operation, kind):
    result, state = workflow(operation, job_results={kind: "running"})
    assert result.returncode != 0 and state["suspended"] and state["lock"]
    assert not any(e[0] == "scale" for e in state["events"])


@pytest.mark.parametrize("login_fails", [False, True])
def test_documented_registry_secret_uses_stdin_and_removes_temporary_credentials(
    tmp_path, monkeypatch, login_fails
):
    tools = tmp_path / "bin"
    tools.mkdir()
    audit = tmp_path / "audit.json"
    monkeypatch.setenv("AUTH_TEST_AUDIT", str(audit))
    monkeypatch.setenv("AUTH_TEST_FAIL", str(int(login_fails)))
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    docker = tools / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        + r"""
import base64, json, os, stat, sys
from pathlib import Path
args = sys.argv[1:]
assert '--password-stdin' in args
password = sys.stdin.read()
assert password == 'test-only-password' and password not in ' '.join(args)
auth_dir = Path(args[args.index('--config') + 1])
Path(os.environ['AUTH_TEST_AUDIT']).write_text(json.dumps({'auth_dir': str(auth_dir)}))
if os.environ['AUTH_TEST_FAIL'] == '1': raise SystemExit(1)
auth = base64.b64encode(b'test-user:test-only-password').decode()
payload = {'auths': {'registry': {'auth': auth}}}
config = auth_dir / 'config.json'
config.write_text(json.dumps(payload))
assert stat.S_IMODE(config.stat().st_mode) == 0o600
assert stat.S_IMODE(auth_dir.stat().st_mode) == 0o700
"""
    )
    docker.chmod(0o755)
    kubectl = tools / "kubectl"
    kubectl.write_text(
        f"#!{sys.executable}\n"
        + r"""
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
assert 'test-only-password' not in ' '.join(args)
if 'create' in args:
    filename = next(x.split('=', 2)[2] for x in args if x.startswith('--from-file='))
    assert 'auths' in json.loads(Path(filename).read_text())
    print('kind: Secret')
else:
    assert sys.stdin.read().strip() == 'kind: Secret'
    audit = Path(os.environ['AUTH_TEST_AUDIT'])
    data = json.loads(audit.read_text())
    data['applied'] = True
    audit.write_text(json.dumps(data))
"""
    )
    kubectl.chmod(0o755)
    text = (ROOT / "docs/kubernetes.md").read_text()
    block = next(
        section.split("```", 1)[0]
        for section in text.split("```bash\n")[1:]
        if "--password-stdin" in section.split("```", 1)[0]
    )
    result = subprocess.run(
        ["bash", "-c", block],
        input="test-user\ntest-only-password\n",
        capture_output=True,
        text=True,
    )
    data = json.loads(audit.read_text())
    assert (result.returncode != 0) == login_fails
    assert not Path(data["auth_dir"]).exists()
    assert bool(data.get("applied")) != login_fails
    assert "test-only-password" not in result.stdout + result.stderr
