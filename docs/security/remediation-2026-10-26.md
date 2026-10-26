# Dependency remediation and image rescan, 2026-10-26

Follow-up to the [initial assessment](trivy-2026-10-26.md). The fixes are
implemented locally; no release tag, registry publication or deployment was
performed. The published v0.3.0 digest is unchanged.

## Implemented changes

| Dependency | Before | Locked after remediation |
| --- | --- | --- |
| anyio | 4.13.0 | 4.15.1 |
| certifi | 2026.4.22 | 2026.10.23 |
| idna | 3.13 | 3.20 |
| pydantic-settings | 2.14.0 | 2.16.1 |
| urllib3 (development) | 2.6.3 | 2.8.0 |

The resolver also updated typing-extensions to 4.16.0 for Python below 3.15.
`pyproject.toml` enforces minimum fixed versions through the direct
pydantic-settings requirement and uv constraints for transitive dependencies.
Other application dependencies were not broadly upgraded.

The runtime stage uninstalls the base Python's pip, including its vendored
msgpack, setuptools and urllib3. The builder retains pip and uv. The image still
runs as UID 1000. Its application virtual environment is created by uv and does
not contain pip.

## Built artifact and evidence

- Local image: `mm-bot-manager:security-20261026`, Linux amd64.
- Docker image ID:
  `sha256:08dd767894c6d7a40f5da48875e6aefd2adbf2fe55c2bc4f8b3bdb2bfbdb3249`.
- Build used `--pull`; base image manifest:
  `sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8`.
- Scan: Trivy 0.76.1, same database as the baseline,
  updated at `2026-10-26T14:38:04.845305817Z`; rescan at 22:45 Moscow time.
- [Sanitized rescan evidence](remediated-trivy-2026-10-26.json) includes every
  remaining package/advisory record, installed Python versions, zero-findings
  lockfile result and SHA-256 fingerprints of the three changed build inputs.

This is a local verification image with version 0.3.0 and revision label
`unknown`, built from the modified working tree. Its ID is not a published
registry digest. Source fingerprints tie the tested image inputs to the
committed Dockerfile, pyproject.toml and uv.lock. A real release needs a new
version and a build labeled with its reviewed commit.

## Before and after

Counts are package/advisory records, not proven exploit paths.

| Severity / scope | Published v0.3.0 | Remediated local image |
| --- | ---: | ---: |
| CRITICAL | 2 | 0 |
| HIGH | 48 | 44 |
| MEDIUM | 73 | 68 |
| LOW | 83 | 82 |
| UNKNOWN | 1 | 1 |
| Total | 207 | 195 |
| Distinct advisory identifiers | 95 | 83 |
| Python-package findings | 12 | 0 |
| Debian-package findings | 195 | 195 |
| Fixable HIGH/CRITICAL records | 6 | 0 |
| Lockfile findings, including development | 10 | 0 |

Comparison of `(advisory, package, installed version)` sets confirms that all
12 Python records disappeared, no new records appeared, and the Debian findings
are unchanged. Both dependency and image gates pass with exit code 0. They gate
fixable HIGH/CRITICAL findings, so a passing image gate does not mean zero CVEs.

## Remaining HIGH findings

| IDs | Reported component and installed version | Records | Fixed version in captured Trixie scan |
| --- | --- | ---: | --- |
| CVE-2026-76642, CVE-2026-78408, CVE-2026-78409, CVE-2026-78410 | util-linux 2.41.5-0+deb13u1, across 9 binary packages | 36 | None |
| CVE-2025-69720 | ncurses 6.5+20250216-2, across 4 binary packages | 4 | None |
| CVE-2026-16742 | libsystemd0 / libudev1 257.13-1~deb13u1 | 2 | None |
| CVE-2026-54369 | libacl1 2.3.2-2+b1 | 1 | None |
| CVE-2026-9538 | perl-base 5.40.1-6+deb13u1 | 1 | None; fix deferred |

The evidence JSON also lists all remaining MEDIUM, LOW and UNKNOWN records.
No suppressions, broad ignore rules or insecure repository changes were added.
Debian source-package matching does not establish that the affected executable
is present or reachable: ncurses' advisory concerns infocmp, and the systemd
advisory concerns systemd-homed. Their applicability still requires component
and deployment review. Upgrading Trixie packages alone did not remove these
findings in this build.

Trivy did not report vulnerabilities in the installed Python packages after
the updates. This does not prove that CPython or native libraries bundled in
wheels have no vulnerabilities; scanner coverage and exploitability remain
separate questions.

## Verification

- Frozen dependency synchronization and offline lockfile consistency check.
- Release metadata check, Ruff lint and formatting, strict mypy, Helm lint.
- Full pytest suite with `MM_TEST_BACKUP_DOCKER=1`, including real PostgreSQL and
  the optional backup/restore drill: **903 passed, none skipped**, in 69.67s;
  statement/branch coverage **94.87%** (required minimum 85%).
- Linux amd64 image build; CLI version output and UID 1000 verified.
- pip absence checked in the application environment and base interpreter.
- Pillow and psycopg imports verified inside the image.
- A separate temporary PostgreSQL 15 server on an internal Docker network
  accepted a connection and a create/insert/select transaction from the image.
  The temporary server and network were removed afterward.
- Image and lockfile Trivy rescans; fixable HIGH/CRITICAL gates both passed.

The first full test run exposed an outdated CI topology assertion that expected
the pre-Trivy release pipeline. It was updated to require the security stage and
the release-image scan dependency before the final full-suite run.
