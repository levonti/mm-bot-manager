"""Check reviewed translation fingerprints and local documentation links."""

import argparse
import hashlib
import json
import re
from pathlib import Path
from string import Formatter

ROOT = Path(__file__).resolve().parents[1]


def fingerprints(root: Path) -> dict[str, str]:
    paths = [root / f"src/mm_bot_manager/locales/{lang}.json" for lang in ("en", "ru", "de")]
    paths += [root / name for name in ("README.md", "README.ru.md", "README.de.md")]
    paths += sorted((root / "docs").glob("??/*.md"))
    paths += [
        root / name
        for name in (
            ".env.example",
            "docs/kubernetes.md",
            "docs/releases.md",
            "deploy/helm/mm-bot-manager/README.md",
        )
    ]
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def unique_pairs(pairs: list[tuple[str, str]]) -> dict[str, str]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate translation key")
    return result


def check(root: Path) -> list[str]:
    errors = []
    catalogs = {
        lang: json.loads(
            (root / f"src/mm_bot_manager/locales/{lang}.json").read_text(),
            object_pairs_hook=unique_pairs,
        )
        for lang in ("en", "ru", "de")
    }
    expected_guides = {p.name for p in (root / "docs/en").glob("*.md")}
    for language in ("ru", "de"):
        if {p.name for p in (root / f"docs/{language}").glob("*.md")} != expected_guides:
            errors.append(f"{language}: documentation guide set differs")
    for lang, messages in catalogs.items():
        if messages.keys() != catalogs["en"].keys():
            errors.append(f"{lang}: catalog keys differ")
            continue
        for key, source in catalogs["en"].items():
            value = messages[key]
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{lang}/{key}: empty translation")
                continue

            def fields(text: str) -> list[tuple[str, str, str]]:
                return sorted(
                    (n, spec, conv or "")
                    for _, n, spec, conv in Formatter().parse(text)
                    if n is not None
                )

            protocol = r"![a-z]+|--[a-z]+(?:-[a-z]+)*"
            if set(re.findall(protocol, value)) != set(re.findall(protocol, source)):
                errors.append(f"{lang}/{key}: command or flag spelling differs")
            if fields(value) != fields(source):
                errors.append(f"{lang}/{key}: placeholders differ")
    current = fingerprints(root)
    manifest = root / "docs/translations.json"
    if not manifest.exists() or json.loads(manifest.read_text()) != current:
        errors.append(
            "Translation review fingerprints are stale; review all languages before --refresh"
        )
    for name in current:
        if not name.endswith(".md"):
            continue
        path = root / name
        text = path.read_text()
        for label in ("English", "Русский", "Deutsch"):
            if f"[{label}]" not in text:
                errors.append(f"{name}: missing language navigation {label}")
        for link in re.findall(r"\]\(([^)]+)\)", text):
            if ":" in link or link.startswith("#"):
                continue
            target = link.split("#", 1)[0]
            if target and not (path.parent / target).exists():
                errors.append(f"{name}: broken link {link}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh", action="store_true", help="Record fingerprints after semantic review"
    )
    args = parser.parse_args()
    if args.refresh:
        (ROOT / "docs/translations.json").write_text(
            json.dumps(fingerprints(ROOT), indent=2) + "\n"
        )
    errors = check(ROOT)
    if errors:
        raise SystemExit("\n".join(errors))
    print("EN/RU/DE catalogs, review fingerprints and documentation links verified")


if __name__ == "__main__":
    main()
