# Debian HIGH applicability and runtime hardening, 2026-10-26

This follows the [dependency remediation](remediation-2026-10-26.md). It reviews
all eight remaining HIGH advisory identifiers against the image contents,
application source and repository-default Compose/Helm application profiles.
It is not a live deployment audit and does not apply to root/privileged
overrides, added capabilities, extra executables or host mounts.

## Findings and disposition

| Advisory | Actual affected code / preconditions | Verified image evidence and action | Disposition |
| --- | --- | --- | --- |
| CVE-2026-76642 | Privileged mount helper failure followed by X-mount post-hooks | Removed mount package and mount/umount executables. No fstab authorizations, setuid files or capabilities; application has no mount/libmount invocation. | Privileged path closed in supported profile; util-linux library version is still affected. |
| CVE-2026-78408 | Root nsenter --join-cgroup leaks a privileged cgroup descriptor across exec | Removed nsenter. Runtime is UID/GID 1000 with no capabilities or privilege escalation; application never invokes nsenter. | Privileged path closed in supported profile. |
| CVE-2026-78409 | Privileged X-mount.subdir operation with authorized fstab entry; vulnerable detached-tree handling on Linux 6.15+ | Removed mount entry point; no configured fstab entry, mount capability or setuid helper. | Privileged path closed in supported profile; kernel version is not used as an exclusion. |
| CVE-2026-78410 | Restricted SUID bind mount followed by ownership/mode changes after path replacement | Removed mount package; no setuid/setgid files, capabilities or fstab authorizations. | Privileged path closed in supported profile. |
| CVE-2025-69720 | Buffer overflow in infocmp's analyze_string, progs/infocmp.c | infocmp was present and unused; removed it. Essential ncurses packages and shared libraries remain. | Affected executable removed. |
| CVE-2026-16742 | systemd-homed privileged group management for logged-in homed-managed users | systemd-homed is absent from PATH and /usr/lib/systemd and /lib/systemd. The image runs Python, not systemd services. libsystemd0/libudev1 version matches do not supply homed. | Affected component absent before and after hardening. |
| CVE-2026-54369 | A privileged libacl caller uses a pathname controlled by a local attacker | libacl remains for base utilities. No libacl, ACL API, subprocess, exec or shell invocation in application source; libacl is not mapped after importing the application/runtime libraries. UID 1000, capabilities zero, no setuid and read-only root prevent the identified privileged-caller scenario. | Not reached and mitigated in supported profile; library code is not patched or removed. |
| CVE-2026-9538 | Archive::Tar reads an attacker-controlled tar size and exhausts memory | perl-base is installed, but importing Archive::Tar fails; no Archive/Tar.pm exists under /usr. Application does not invoke Perl. | Affected module absent before and after hardening. |

These conclusions concern the specific published advisory mechanisms. They do
not prove the absence of every possible vulnerability in those source packages.
The application source was unchanged by this task. Any future ACL functionality,
shell helpers, user-supplied executables, root entrypoint or broadened runtime
permissions requires a fresh applicability review.

## Implemented protection

- Removed the non-Essential `mount` package through apt, without forcing removal
  of Essential packages. Its binaries and package records disappear together.
- Stopped installing unnecessary system `libpq5`: psycopg[binary] bundles its
  own client libraries. Eleven OS packages disappear compared with the previous
  image, including mount and the libpq dependency chain.
- Removed unused `/usr/bin/nsenter` and `/usr/bin/infocmp`, keeping the Essential
  util-linux/ncurses package metadata intact for accurate version-based scans.
- Stripped all setuid/setgid bits from regular files under `/usr` (merged-usr
  also covers /bin and /sbin). Set the image account to UID/GID 1000.
- Hardened the Compose application service with explicit UID/GID 1000,
  capabilities drop ALL, no-new-privileges, read-only root and a 64 MiB writable
  /tmp tmpfs with noexec/nosuid/nodev. PostgreSQL service/volumes are unchanged.
- Confirmed Helm already has runAsNonRoot, UID/GID 1000, RuntimeDefault seccomp,
  capabilities drop ALL, allowPrivilegeEscalation=false and readOnlyRootFilesystem.
  No chart change or chart version bump was needed.
- Added a reusable runtime check and made release image builds execute it on
  the exact pushed digest. Python isolated mode prevents PYTHONOPTIMIZE from
  disabling its assertions through environment variables.

No Debian sid packages, forced Essential removals, CVE ignore rules or VEX
suppression were introduced. Shell and basic base-OS tools remain available,
but privileged diagnostics and package installation require a separate
diagnostic container rather than changing the application security profile.

## Artifact and raw scan comparison

Local Linux amd64 image: `mm-bot-manager:debian-high-20261026`.

Image ID:
`sha256:1040993863057194c7df8857f5ac2fd898a59ff026301fefe8c096bd0d7ee7ad`.

Version/revision labels are `0.3.0`/`unknown`: this is a local verification image,
not a published release. [Sanitized evidence](debian-high-trivy-2026-10-26.json)
records image identity, source-input fingerprints, every remaining finding and
the successful runtime check. The scanner is Trivy 0.76.1 with the same database
as both earlier scans, updated at 2026-10-26 14:38:04 UTC.

| Raw severity / count | Before this hardening | After |
| --- | ---: | ---: |
| CRITICAL | 0 | 0 |
| HIGH | 44 | 40 |
| MEDIUM | 68 | 57 |
| LOW | 82 | 60 |
| UNKNOWN | 1 | 1 |
| Total records | 195 | 158 |
| Unique advisory identifiers | 83 | 71 |
| OS packages | 97 | 86 |
| Python-package findings | 0 | 0 |

All 37 removed records come from removed packages; there are no newly introduced
records. The raw 40 HIGH records still represent eight unique advisories:
32 util-linux records across eight remaining binary packages, four ncurses,
two systemd-library, one libacl and one perl-base record. No finding has been
hidden or treated as a patched package. The applicability review above is
separate from this raw scanner output. Fixed Trixie versions remain absent in
the captured report.

## Verification

- Runtime check passes with the supported profile: UID/GID 1000, capabilities
  and capability bounding set zero, no-new-privileges, read-only root, no setid
  files, no listed CLI entry points/Archive::Tar and successful application
  imports, TLS CA loading and temporary-file creation.
- Negative runtime check omitting no-new-privileges fails as intended.
- Hardened image connects to a separate temporary PostgreSQL 15 server, migrates
  the schema, creates/inserts/reads a temporary table and serves HTTP 200 on both
  /health/live and /health/ready. Temporary Docker network/server are removed.
- CLI version output succeeds under the hardened profile.
- Compose is rendered with dummy values and --env-file /dev/null; private .env
  contents are not read. GitLab CI Lint accepts the release-tag configuration.
- Ruff lint/format, strict mypy, release metadata check and Helm lint pass.
- Full suite with MM_TEST_BACKUP_DOCKER=1: **903 passed**, none skipped, in
  78.85s, with **94.87%** statement/branch coverage (minimum 85%).

## Primary sources

- [util-linux mount post-hooks](https://security-tracker.debian.org/tracker/CVE-2026-76642).
- [nsenter cgroup descriptor](https://security-tracker.debian.org/tracker/CVE-2026-78408).
- [X-mount.subdir](https://security-tracker.debian.org/tracker/CVE-2026-78409).
- [SUID restricted bind mounts](https://security-tracker.debian.org/tracker/CVE-2026-78410).
- [infocmp](https://security-tracker.debian.org/tracker/CVE-2025-69720).
- [systemd-homed](https://security-tracker.debian.org/tracker/CVE-2026-16742).
- [libacl pathname APIs](https://security-tracker.debian.org/tracker/CVE-2026-54369).
- [Perl Archive::Tar](https://security-tracker.debian.org/tracker/CVE-2026-9538).
