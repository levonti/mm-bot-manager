import os
import runpy
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
CURRENT = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
RELEASE = runpy.run_path(str(ROOT / "scripts/release.py"))


@pytest.fixture
def release_tree(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "mm-bot-manager"\nversion = "0.2.0"\n'
    )
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "mm-bot-manager"\nversion = "0.2.0"\n')
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [0.2.0] - 2026-10-26\n\n- Baseline.\n"
    )
    chart = tmp_path / "deploy/helm/mm-bot-manager/Chart.yaml"
    chart.parent.mkdir(parents=True)
    chart.write_text('version: 0.1.3\nappVersion: "0.2.0"\n')
    return tmp_path


@pytest.mark.parametrize("value", ["01.2.3", "1.2", "v1.2.3", "1.2.3-rc.1", "1.2.3+build"])
def test_rejects_unsupported_versions(value):
    with pytest.raises(ValueError, match="stable"):
        RELEASE["semver"](value)


def test_prepare_updates_all_metadata_and_moves_notes(release_tree):
    changelog = release_tree / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text().replace("## [Unreleased]", "## [Unreleased]\n\n- New feature.")
    )
    RELEASE["prepare"](release_tree, "0.3.0", "0.1.4")
    assert RELEASE["metadata"](release_tree) == ("0.3.0", "0.1.4")
    assert "New feature" in RELEASE["check"](release_tree, "v0.3.0")
    assert changelog.read_text().split("## [0.3.0]")[0].endswith("## [Unreleased]\n\n")


@pytest.mark.parametrize("tag", ["v0.1.0", "0.2.0", "v0.2.0-rc.1"])
def test_tag_mismatch_fails(release_tree, tag):
    with pytest.raises(ValueError, match="Tag"):
        RELEASE["check"](release_tree, tag)


@pytest.mark.parametrize("target", ["lock", "chart", "notes"])
def test_inconsistent_metadata_fails(release_tree, target):
    if target == "lock":
        path = release_tree / "uv.lock"
        text = path.read_text().replace(
            'name = "mm-bot-manager"\nversion = "0.2.0"',
            'name = "mm-bot-manager"\nversion = "9.0.0"',
        )
    elif target == "chart":
        path = release_tree / "deploy/helm/mm-bot-manager/Chart.yaml"
        text = path.read_text().replace('appVersion: "0.2.0"', 'appVersion: "9.0.0"')
    else:
        path = release_tree / "CHANGELOG.md"
        text = path.read_text().replace("## [0.2.0]", "## [9.0.0]")
    path.write_text(text)
    with pytest.raises(ValueError):
        RELEASE["check"](release_tree)


def test_bad_preparation_preserves_files(release_tree):
    before = {p: p.read_bytes() for p in release_tree.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="Unreleased"):
        RELEASE["prepare"](release_tree, "0.3.0", "0.1.4")
    assert all(p.read_bytes() == value for p, value in before.items())


def test_chart_change_requires_bump_and_versions_cannot_decrease(release_tree):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=release_tree, text=True).strip()

    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "baseline")
    base = git("rev-parse", "HEAD")
    assert RELEASE["check"](release_tree, base=base)
    chart = release_tree / "deploy/helm/mm-bot-manager/Chart.yaml"
    chart.write_text(chart.read_text() + "\n# Chart change\n")
    with pytest.raises(ValueError, match="higher chart"):
        RELEASE["check"](release_tree, base=base)
    chart.write_text(chart.read_text().replace("version: 0.1.3", "version: 0.1.4"))
    assert RELEASE["check"](release_tree, base=base)
    for name in (
        "pyproject.toml",
        "uv.lock",
        "CHANGELOG.md",
        "deploy/helm/mm-bot-manager/Chart.yaml",
    ):
        path = release_tree / name
        path.write_text(path.read_text().replace("0.2.0", "0.1.0"))
    with pytest.raises(ValueError, match="must not decrease"):
        RELEASE["check"](release_tree, base=base)


def test_cli_version_needs_no_settings_or_database():
    result = subprocess.run(
        [sys.executable, "-m", "mm_bot_manager", "--version"],
        env={"PATH": os.environ["PATH"], "MM_BUILD_REVISION": "a" * 40},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == f"mm-bot-manager {CURRENT} (revision {'a' * 40})"


def test_build_revision_is_sanitized(monkeypatch):
    from mm_bot_manager.version import build_info

    monkeypatch.setenv("MM_BUILD_REVISION", "untrusted\nvalue")
    assert build_info() == (CURRENT, "unknown")


async def test_chat_version():
    from mm_bot_manager.commands import REGISTRY
    from mm_bot_manager.commands.parser import ParsedArgs

    handler = REGISTRY[("version",)]
    assert f"mm-bot-manager {CURRENT}" in await handler(None, ParsedArgs([], {}))
    assert "Usage" in await handler(None, ParsedArgs(["extra"], {}))


def test_release_jobs_are_gated_and_publish_waits_for_both_builds():
    config = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
    assert config["verify"]["rules"] == [{"when": "on_success"}]
    assert config["stages"] == ["verify", "package", "release"]
    assert {need["job"] for need in config["publish-release"]["needs"]} == {
        "package-release",
        "build-release-image",
    }
    for name in ("package-release", "build-release-image", "publish-release"):
        assert config[name]["extends"] == ".release-tag"
