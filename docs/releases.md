# Versioning and releases

## Version policy

`pyproject.toml` is the application version source. The project's own entry in
`uv.lock` and Helm `appVersion` must match it. Versions currently support stable
`MAJOR.MINOR.PATCH` only; prereleases and build suffixes are deliberately rejected
by the release helper. Git tags add a `v` prefix; package and image versions do not.

The public compatibility contract covers chat commands and their authorization,
configuration/environment variables, Helm values and supported database upgrades.
Patch releases fix bugs without intentional contract changes. Minor releases add
compatible functionality. From 1.0.0 onward, incompatible changes require a major
release. Before 1.0.0, use a minor bump for incompatible changes and describe them
explicitly in the changelog. Changes that tighten permissions or validation must
describe any effect on existing users even when they correct a security defect.

The chart's `version` is independent of the application version. Increase it when
any chart content changes, including `appVersion`; choose patch/minor/major based
on the chart's own compatibility. CI checks chart changes against the MR base and
rejects missing bumps. Ordinary application changes can accumulate in
`[Unreleased]` without changing application metadata on every commit.

The database migration number is independent of both versions. Release notes must
document schema changes, supported upgrade paths, backup requirements and downgrade
restrictions. A Helm rollback does not reverse a database migration. The existing
database upgrade restrictions in the README remain applicable.

## Prepare a release

Use a durable checkout and a virtual environment created with `uv venv`:

```sh
uv venv .venv
uv sync --frozen --extra postgres
```

1. Add user-facing changes under `[Unreleased]` in `CHANGELOG.md`. Include breaking
   changes, migrations and rollback restrictions when applicable. No `TODO` text
   may appear in release notes.
2. Choose the next application and chart versions explicitly. For example, after
   application 0.2.0/chart 0.2.1:

   ```sh
   uv run --no-sync python scripts/release.py prepare 0.3.0 --chart-version 0.2.2
   uv sync --frozen --extra postgres
   uv run --no-sync python scripts/release.py check
   ```

   The helper updates `pyproject.toml`, only the project's own lockfile version,
   chart metadata, and moves unreleased notes into a dated release section. It
   preserves dependency versions. Both numbers must increase; chart-only releases
   can update the chart version and changelog explicitly while keeping `appVersion`.
3. Review and commit the changes through an MR. CI validates metadata, lint, types,
   Helm and tests. Merge the verified changes into the default branch.
4. Fetch the default branch and tag the exact reviewed commit. Confirm that its
   version matches the tag before pushing. Example for the initial prepared release:

   ```sh
   git fetch origin main
   uv run --no-sync python scripts/release.py check --tag v0.2.0
   git merge-base --is-ancestor HEAD origin/main
   git tag -a v0.2.0 -m 'mm-bot-manager 0.2.0' HEAD
   git push origin v0.2.0
   ```

   Run these commands from the reviewed release commit. Creating or pushing tags
   publishes a release; preparation alone does not create a tag, push or deploy.
   Do not create a GitLab Release manually before the tag pipeline.

## Tag pipeline

All tag pipelines validate that the tag exactly equals `v<project.version>`, then
require the tagged commit to be an ancestor of the current default branch.
Only stable version tags enable the package and release jobs. The pipeline runs
the full verification job on the tagged SHA before building:

- A Python wheel and source distribution, built using the existing `uv_build`
  backend, plus a Helm archive and checksums.
- A Linux amd64 Docker Hub image tagged with the version, `v<version>` and
  `sha-<full commit SHA>`. The default repository is `levonti17/mm-bot-manager`;
  `latest` is not updated by this pipeline. The
  image includes OCI version/revision labels and `MM_BUILD_REVISION`.
- A GitLab Release with the changelog section, exact commit, container digest and
  an archive download link. The package artifact has no expiration configured.

Image publishing is serialized. Existing version tags are rejected, and registry
inspection errors other than a missing manifest fail closed. Enable registry tag
immutability as an additional server-side guarantee. Never move published Git
tags or overwrite release image tags. Use a new version for corrections.

The image job uses Docker 27.5.1 with TLS-enabled Docker-in-Docker. Before publishing:

- Configure a suitable Docker runner with privileged DinD and a shared
  `/certs/client` volume; route the image job with runner tags if required by the
  installation. An online runner alone does not prove these settings are present.
- Configure GitLab CI/CD variables `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`
  (a Docker Hub PAT with Read/Write permissions). Mark both Protected and the
  token Masked/Hidden. Do not put credentials in files or pass them in command
  arguments. Set `DOCKERHUB_IMAGE=levonti17/mm-bot-manager` (also the YAML default).
  The image job authenticates through `docker login --password-stdin`; the
  integrated GitLab Container Registry is not required.
- For an internal GitLab CA, configure the runner to provide
  `CI_SERVER_TLS_CA_FILE`. Verification fetches use this CA file without disabling
  TLS certificate checks; public CAs use the system trust bundle.
- Make the GitLab CLI image and other pinned build images reachable by the runner.
  The release job uses GitLab's rolling official CLI image; pin an approved
  version/digest if the installation requires fully pinned tooling.
- Protect `v*` Git tags, restrict who can publish, and configure immutable version
  tags in Docker Hub when available for the repository. Existing repository settings are not changed by this work.
- Retain release artifacts and registry images in cleanup policies. The download
  link relies on the package job's artifacts; deleting that job/pipeline removes
  those assets despite `expire_in: never`.

If packaging succeeds but the image job fails, retry after fixing the runner. If
the image was already pushed, the overwrite guard intentionally blocks a rebuild;
retry only the failed publish job when both build jobs succeeded. Investigate
partial pushes before taking recovery action. Do not remove guards to reuse an
existing version.

Publishing does not deploy. Transfer the built image to Harbor when needed,
preserving its digest, and follow the Kubernetes runbook using `image.digest`.
Multi-architecture images remain a separate build concern; the release pipeline
currently produces Linux amd64 only.

## Vulnerability scanning

The [2026-10-26 v0.3.0 assessment](security/trivy-2026-10-26.md) records the initial
findings and proposed remediation, with sanitized scan evidence.

CI pins `aquasec/trivy:0.76.1` and caches its vulnerability database. The runner
needs access to the scanner image, Trivy's database registries and Docker Hub.
`scan-dependencies` scans lockfiles (including development dependencies) in every
branch, merge request and tag pipeline. It excludes local virtual environments
and tool caches. `scan-release-image` scans the exact `RELEASE_IMAGE` digest
produced by `build-release-image`, using remote registry access without DinD.
The release image must be publicly readable; private repositories need separately
configured read credentials for the scanner.

Both jobs preserve full JSON and text reports as ordinary job artifacts for 30
days. They then fail on HIGH/CRITICAL vulnerabilities for which Trivy records a
fix. Unfixed and lower-severity findings remain visible in the full reports;
they require review even when the gate passes. Scanner or database download
errors fail the job. Reports are not GitLab's native container-scanning format.

`publish-release` explicitly depends on a successful image scan. This prevents
GitLab Release creation but does not undo Docker Hub tags already published by
the build job. A failed scan requires investigation; do not overwrite the
immutable release tags to repair it. Publish a corrected version instead.

To reproduce the image scan with Trivy 0.76.1:

```sh
trivy image --image-src remote --platform linux/amd64 --scanners vuln \
  --format json --output trivy-image.json \
  levonti17/mm-bot-manager@sha256:<release-digest>
trivy convert --scanners vuln --format table --output trivy-image.txt trivy-image.json
trivy image --image-src remote --platform linux/amd64 --scanners vuln \
  --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 \
  levonti17/mm-bot-manager@sha256:<release-digest>
```

## Docker Compose upgrades

Set `MM_BOT_MANAGER_IMAGE=levonti17/mm-bot-manager@sha256:<release-digest>` in the
existing ignored `.env` file. Keep credentials, handlers and database settings in
place. Pull the exact image before stopping the manager, retain the current image
and configuration, and take a verified database backup while the application is
stopped. Recreate only the application with
`docker compose up -d --no-deps --no-build --pull never mm-bot-manager`; do not
recreate PostgreSQL or remove its volume. Confirm health, schema version, feedback
configuration and build revision after startup. Keep the previous image for an
application rollback; do not restore an older database over newer writes.

## Runtime diagnostics

`mm-bot-manager --version` and `python -m mm_bot_manager --version` work without
Mattermost credentials or a database connection. Chat command `!version` and the
`starting` log event report the same installed package version. Release containers
also report the full build SHA. Local/package-only installs report `unknown` when
no valid 40-character lowercase hexadecimal build revision was supplied. Revision
is provenance, not a replacement for image digest verification.

## Validation

```sh
uv run --no-sync python scripts/release.py check
uv run --no-sync ruff check src tests scripts/release.py
uv run --no-sync ruff format --check src tests scripts/release.py
uv run --no-sync mypy src
helm lint deploy/helm/mm-bot-manager
uv run --no-sync pytest -q --cov=mm_bot_manager --cov-branch --cov-fail-under=85
uv build --no-sources
```

References: [SemVer](https://semver.org/spec/v2.0.0.html),
[Helm chart metadata](https://helm.sh/docs/topics/charts/),
[GitLab tag releases](https://docs.gitlab.com/user/project/releases/release_cicd_examples/),
[GitLab Docker builds](https://docs.gitlab.com/ci/docker/using_docker_build/).
