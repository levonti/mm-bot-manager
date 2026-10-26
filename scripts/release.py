"""Prepare stable SemVer releases and validate their checked-in metadata."""

import argparse
import datetime
import os
import re
import subprocess
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = Path("deploy/helm/mm-bot-manager/Chart.yaml")
STABLE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def semver(value: str) -> tuple[int, ...]:
    if not STABLE.fullmatch(value):
        raise ValueError(f"Expected stable MAJOR.MINOR.PATCH, got {value!r}")
    return tuple(map(int, value.split(".")))


def metadata(root: Path) -> tuple[str, str]:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    chart = yaml.safe_load((root / CHART).read_text())
    version = project["version"]
    semver(version)
    semver(chart["version"])
    if chart["appVersion"] != version:
        raise ValueError("Helm appVersion differs from project.version")
    packages = tomllib.loads((root / "uv.lock").read_text())["package"]
    own = [p for p in packages if p["name"] == project["name"]]
    if len(own) != 1 or own[0]["version"] != version:
        raise ValueError("uv.lock project version differs from project.version")
    return version, chart["version"]


def release_notes(root: Path, version: str) -> str:
    text = (root / "CHANGELOG.md").read_text()
    matches = list(
        re.finditer(rf"^## \[{re.escape(version)}\] - (\d{{4}}-\d{{2}}-\d{{2}})$", text, re.M)
    )
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one dated changelog section for {version}")
    datetime.date.fromisoformat(matches[0][1])
    notes = re.split(r"^## ", text[matches[0].end() :], maxsplit=1, flags=re.M)[0].strip()
    if not notes or "TODO" in notes:
        raise ValueError("Release notes must be nonempty and contain no TODO")
    return notes + "\n"


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def check(root: Path, tag: str = "", base: str = "") -> str:
    version, chart_version = metadata(root)
    notes = release_notes(root, version)
    if tag and tag != f"v{version}":
        raise ValueError(f"Tag {tag!r} must equal v{version}")
    if base:
        old_project = tomllib.loads(git(root, "show", f"{base}:pyproject.toml"))["project"]
        old_chart = yaml.safe_load(git(root, "show", f"{base}:{CHART}"))
        if semver(version) < semver(old_project["version"]):
            raise ValueError("Application version must not decrease")
        changed = git(root, "diff", "--name-only", base, "--", str(CHART.parent))
        if changed and semver(chart_version) <= semver(old_chart["version"]):
            raise ValueError("Chart changes require a higher chart version")
        if semver(chart_version) < semver(old_chart["version"]):
            raise ValueError("Chart version must not decrease")
    return notes


def replace_once(pattern: str, replacement: str, text: str) -> str:
    text, count = re.subn(pattern, replacement, text, flags=re.M)
    if count != 1:
        raise ValueError(f"Expected one metadata field, found {count}")
    return text


def prepare(root: Path, version: str, chart_version: str) -> None:
    old, old_chart = metadata(root)
    if semver(version) <= semver(old) or semver(chart_version) <= semver(old_chart):
        raise ValueError("Both application and chart versions must increase")
    changelog = (root / "CHANGELOG.md").read_text()
    if f"## [{version}]" in changelog:
        raise ValueError("Changelog already contains this version")
    unreleased = re.search(r"^## \[Unreleased\]\n(.*?)(?=^## |\Z)", changelog, re.M | re.S)
    if not unreleased or not unreleased[1].strip() or "TODO" in unreleased[1]:
        raise ValueError("Fill in [Unreleased] notes before preparing a release")
    project = (root / "pyproject.toml").read_text()
    project = replace_once(
        r'^version = "' + re.escape(old) + r'"$', f'version = "{version}"', project
    )
    lock = (root / "uv.lock").read_text()
    lock = replace_once(
        r'(\[\[package\]\]\nname = "mm-bot-manager"\nversion = ")[^"]+("$)',
        rf"\g<1>{version}\g<2>",
        lock,
    )
    chart = (root / CHART).read_text()
    chart = replace_once(r"^version: .+$", f"version: {chart_version}", chart)
    chart = replace_once(r"^appVersion: .+$", f'appVersion: "{version}"', chart)
    heading = f"## [Unreleased]\n\n## [{version}] - {datetime.date.today().isoformat()}\n"
    changelog = (
        changelog[: unreleased.start()]
        + heading
        + changelog[unreleased.start() + len("## [Unreleased]\n") :]
    )
    # Resolve and validate every replacement before writing any file.
    for path, text in [
        ("pyproject.toml", project),
        ("uv.lock", lock),
        (str(CHART), chart),
        ("CHANGELOG.md", changelog),
    ]:
        (root / path).write_text(text)
    check(root)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("check")
    verify.add_argument("--tag", default=os.environ.get("CI_COMMIT_TAG", ""))
    verify.add_argument("--base", default="")
    bump = commands.add_parser("prepare")
    bump.add_argument("version")
    bump.add_argument("--chart-version", required=True)
    notes = commands.add_parser("notes")
    notes.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(ROOT, args.version, args.chart_version)
        elif args.command == "notes":
            args.output.write_text(check(ROOT, os.environ.get("CI_COMMIT_TAG", "")))
        else:
            check(ROOT, args.tag, args.base)
    except (ValueError, KeyError) as exc:
        parser.exit(1, f"Release metadata error: {exc}\n")


if __name__ == "__main__":
    main()
