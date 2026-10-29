# mm-bot-manager Helm chart

[English](../../../docs/en/operations.md) · [Русский](../../../docs/ru/operations.md) · [Deutsch](../../../docs/de/operations.md)

This self-contained Helm chart deploys one `mm-bot-manager` Pod to Kubernetes
`>= 1.21.1`. It has no chart dependencies and is suitable for a disconnected
cluster after the required images have been mirrored to its local registry.

The application maintains a Mattermost WebSocket connection; it does not expose
an application HTTP API. Consequently, the chart deliberately creates neither a
Service nor an Ingress. The Pod-local port `8080` is only used by Kubernetes
health probes.

## Prerequisites

- Kubernetes `>= 1.21.1` and Helm 3 or 4;
- an application Secret (`MM_TOKEN`, `MM_BOT_TOKEN`), provided manually or by ESO;
- either an external database Secret (`DB_URL`) or a
  PostgreSQL Secret (`POSTGRES_USER`, `POSTGRES_PASSWORD`,
  `POSTGRES_DB`);
- one application image for external mode; application and PostgreSQL images for
  internal mode. Both can be referenced by tag or immutable digest.

## Deployment modes

| Mode | Rendered resources | Required Secrets | Images |
| --- | --- | --- | --- |
| `database.mode=external` (default) | Deployment, ConfigMap, ServiceAccount | application, external database | application |
| `database.mode=internal` | external resources plus headless PostgreSQL Service, 1-replica PostgreSQL StatefulSet and data PVC, backup PVC, PVC-check Job and CronJob (when enabled), optional restore Job | application, PostgreSQL | application, PostgreSQL |

The application Deployment is always one replica with `Recreate` strategy. The
internal PostgreSQL StatefulSet is always one replica. Do not use the internal
mode as a high-availability database.

Use `longhorn` for the production database and backup PVCs. `local-path` is
supported for a single-node/test cluster only: data and backups live on one node
and disappear if that node is lost.

## Values and defaults

Passwords and tokens do not belong in values. Secret names below are contracts:
the chart reads the listed fixed keys and never renders Secret values. By default
it references existing Secrets; optional ESO resources create/update those same Secrets.

| Section | Default | Purpose |
| --- | --- | --- |
| `image` | `repository=mm-bot-manager`, `tag=""` (chart app version), `digest=""`, `pullPolicy=IfNotPresent` | Application image; a non-empty digest takes precedence over the tag. |
| `imagePullSecrets` | `[]` | Pod image pull Secret references, e.g. `[{name: harbor-pull}]`. |
| `nameOverride`, `fullnameOverride` | `""`, `""` | Override generated resource names. |
| `app.existingSecret` | `mm-bot-manager-app` | Secret with `MM_TOKEN` and `MM_BOT_TOKEN`. |
| `externalSecrets.enabled` | `false` | Render optional ExternalSecret resources; existing Secrets remain the default. |
| `externalSecrets.apiVersion` | `external-secrets.io/v1` | Explicit CRD API; `external-secrets.io/v1beta1` is also supported for compatible installations. |
| `externalSecrets.secretStoreRef` | `name=""`, `kind=SecretStore` | Existing store; name is required when ESO is enabled. Kind may be `ClusterSecretStore`. |
| `externalSecrets.refreshInterval` | `1h` | Positive duration using `h`, `m`, `s`, `ms` units (e.g. `30m`, `1h30m`); zero is rejected. |
| `externalSecrets.app` | `enabled=true`, `data={}` | When ESO is enabled, map both application keys to remote references, or set `enabled=false` to retain manual provisioning. |
| `externalSecrets.database` | `enabled=true`, `data={}` | Map the active database's keys, or set `enabled=false`. The database mode selects its existingSecret target. |
| `mattermost` | `url=https://mm.example.com`, `approvers=""`, `catalogViewers=""`, `feedbackHandlers=""`, `defaultLanguage=en`, `displayTimezone=UTC`, `verifySSL=true` | Imports and key attribute updates require `approvers`. Self-approval is forbidden. Global catalog readers are approvers plus `catalogViewers`. Nonempty `feedbackHandlers` enables DM-only feedback tickets with separate support access; no inbound HTTP endpoint is required. `defaultLanguage` accepts `en`, `ru` or `de`. Dates use an IANA `displayTimezone`, e.g. `Europe/Moscow`. |
| `mattermost.caBundle` | `existingConfigMap=""`, `key=ca-bundle.crt` | Optional complete PEM trust bundle for a private Mattermost CA. Mounted read-only; `SSL_CERT_FILE` applies to HTTP and WebSocket clients. Restart after changing its external ConfigMap. |
| `logLevel` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `health` | `host=0.0.0.0`, `port=8080` | Pod-local liveness/readiness listener. |
| `probes` | startup `5s/2s/30`; readiness `10s/2s/3`; liveness `10s/2s/6` | Probe period / timeout / failure threshold. |
| `serviceAccount` | `create=true`, `name=""` | Token-less Pod ServiceAccount. |
| `podAnnotations`, `podLabels` | `{}`, `{}` | Additional application Pod metadata. Reserved name/instance/component labels cannot be overridden. |
| `podSecurityContext` | UID/GID `1000`, `runAsNonRoot=true`, `RuntimeDefault` seccomp | Application Pod security context. |
| `containerSecurityContext` | privilege escalation disabled, read-only root filesystem, all capabilities dropped | Application container security context. |
| `resources` | requests `50m/64Mi`, limits `500m/256Mi` | Application resources. |
| `nodeSelector`, `tolerations`, `affinity` | `{}`, `[]`, `{}` | Application scheduling controls. |
| `terminationGracePeriodSeconds` | `65` | Application drain window plus time for cancellation and compensation. |
| `database.mode` | `external` | Select `external` or `internal`. |
| `database.external.existingSecret` | `mm-bot-manager-external-db` | External-mode Secret with `DB_URL`. |
| `database.internal.image` | `postgres:15.18-alpine3.24`, empty digest, `IfNotPresent` | PostgreSQL/backup/restore image; digest takes precedence. |
| `database.internal.auth.existingSecret` | `mm-bot-manager-postgresql` | Secret with `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`. |
| `database.internal.service.port` | `5432` | Headless PostgreSQL service port. |
| `database.internal.storage` | `longhorn`, `ReadWriteOnce`, `8Gi` | PostgreSQL data PVC. |
| `database.internal.podSecurityContext` | `RuntimeDefault` seccomp | PostgreSQL Pod security context. |
| `database.internal.containerSecurityContext` | `{}` | PostgreSQL container security context. |
| `database.internal.resources` | requests `100m/256Mi`, limits `1/1Gi` | PostgreSQL resources. |
| `database.internal.probes` | startup `5s/3s/30`; readiness `10s/3s/3`; liveness `10s/3s/6` | PostgreSQL probe settings. |
| `database.internal.backup` | enabled, `0 2 * * *`, 14 days, history `3/3`, `longhorn`, `ReadWriteOnce`, `5Gi`, requests `50m/64Mi`, limits `500m/256Mi` | Logical dump CronJob, retained backup PVC and automatic PVC-check Job. |
| `database.internal.restore` | `enabled=false`, `file=""` | Opt-in one-time restore Job. The file must be a safe `.dump` basename. |
| `database.internal.inspect` | `enabled=false` | Opt-in archive inspection Job with read-only backup PVC and inherited image pull Secrets. |
| `database.internal.verification` | `enabled=false`, `file=""` | Opt-in full trial restore into a temporary database, followed by registry-table queries and cleanup. |

Maintenance Jobs require internal mode and an enabled backup PVC. Enable only one
at a time; restore/verification require a safe existing `.dump` basename. They have
no automatic retries. Do not enable maintenance flags in the saved installation
values: the source checkout's `scripts/kubernetes-db.sh` renders a single Job from
the installed release values and coordinates locking, backups and application
shutdown. Follow `docs/kubernetes.md` in the source checkout for the full procedure.
The verification DB needs additional space on the PostgreSQL data PVC.

Application Pods have `app.kubernetes.io/component=application`; use this label
with the release instance to select them. This addition preserves the original
immutable Deployment selector when upgrading from 0.1.0. Both health paths check
the database; they do not establish Mattermost WebSocket readiness.

## Fixed Secret key contracts

| Secret value | Required keys |
| --- | --- |
| `app.existingSecret` | `MM_TOKEN`, `MM_BOT_TOKEN` |
| `database.external.existingSecret` | `DB_URL` |
| `database.internal.auth.existingSecret` | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` |

## External Secrets Operator (optional)

ESO is opt-in and does not change Deployment, PostgreSQL or maintenance Job
Secret references. Install ESO and its CRDs separately, and configure a ready
`SecretStore` in the release namespace or an approved `ClusterSecretStore`.
The chart creates neither the operator, stores, Vault authentication credentials
nor extra RBAC. The application ServiceAccount remains token-less.
The base chart's Kubernetes `>=1.21.1` declaration does **not** establish ESO
compatibility: check the selected ESO release, CRDs and real cluster version.
Select an API version actually served by the installed CRDs; there is no automatic
discovery or fallback, so offline `helm template` still works.

Use `examples/values-eso-external.yaml` after the external PostgreSQL profile,
or `examples/values-eso-internal.yaml` after any internal profile. These are
overlays containing remote **references**, not credentials. From this chart directory:

```bash
helm lint . -f examples/values-external.yaml -f examples/values-eso-external.yaml
helm template mm-bot-manager . --namespace mm-bot-manager \
  -f examples/values-external.yaml -f examples/values-eso-external.yaml
helm template mm-bot-manager . --namespace mm-bot-manager \
  -f examples/values-internal-longhorn.yaml -f examples/values-eso-internal.yaml
```

`data` maps each fixed Secret key to `remoteRef` fields: required `key`, optional
`property` and optional string `version`. For Vault KV v2, `key` is relative to
the mount configured in the store (do not add the API's `/data/` segment), and
`property` selects the field containing that credential. No literal value fields
or arbitrary output keys are accepted. The active targets require all their keys;
internal database mappings cannot be used with external mode or vice versa.

For mixed provisioning, set `externalSecrets.app.enabled=false` or
`externalSecrets.database.enabled=false` and provision that existing Secret
manually. With ESO globally enabled at least one target must be enabled, and the
application/database Secret names must differ. Keep target names unique to the
release; do not let multiple controllers write the same Secret.

ExternalSecret names are release-scoped with `-app` and `-database` suffixes;
their targets are exactly `app.existingSecret` and the selected database's
`existingSecret`. Targets use `creationPolicy: Orphan` and `deletionPolicy: Retain`:
they remain if the ExternalSecret/release is removed, ESO is disabled, or provider
data disappears. A failed synchronization can therefore leave old credentials;
monitor `Ready` and refresh status. Disabling ESO stops future synchronization
and leaves manual management possible. Removing an older ExternalSecret with a
different ownership policy is a separate migration; these settings do not change
other controllers' owner references.

Before enabling ESO for an existing Secret, ensure Vault contains the **current**
working values and the target has no competing controller. ESO will reconcile
its managed keys, so do not keep applying local env files to ESO-managed targets.
To return to manual provisioning, disable the relevant ESO target first, verify
its ExternalSecret was removed and its Kubernetes Secret remains, then manage
that Secret manually. Retained Secrets require explicit cleanup after uninstall.

On first install, the Helm readiness wait can include the time ESO needs to
create the Secrets. After install, check the ExternalSecrets' `Ready` conditions
and key names, then perform application acceptance. On rotation, ESO updates
Secrets but does not refresh running environment variables: coordinate credential
replacement, wait for synchronization and restart the application. The chart does
not install a restart controller. Its single-replica `Recreate` strategy entails
an interruption. For an initialized internal database, changing Vault or a Secret
does not change the PostgreSQL role password; coordinate the database change and
client restarts in a maintenance window. Never regenerate the database/user name
for an existing data PVC.

Secrets still exist in Kubernetes/etcd: use narrow RBAC, encryption at rest and
protected backups. This integration supplies startup credentials; it does not
save newly minted Mattermost bot tokens in Vault. See the source checkout's
`docs/kubernetes.md` section 3.1 for setup, synchronization and rotation commands.
Provider setup and behavior are documented by
[ESO's Vault provider](https://external-secrets.io/latest/provider/hashicorp-vault/)
and [ExternalSecret API](https://external-secrets.io/latest/api/externalsecret/).

## Validate and render

The following commands are chart-local: run them after changing into this chart
directory (for example, `cd mm-bot-manager` after unpacking the packaged chart).
They do not require the surrounding source repository.

```bash
helm lint .

helm template mm-bot-manager . \
  --namespace mm-bot-manager --kube-version 1.21.1 \
  --values examples/values-external.yaml
helm template mm-bot-manager . \
  --namespace mm-bot-manager --kube-version 1.21.1 \
  --values examples/values-internal-longhorn.yaml
helm template mm-bot-manager . \
  --namespace mm-bot-manager --kube-version 1.21.1 \
  --values examples/values-internal-local-path.yaml
helm template mm-bot-manager . \
  --namespace mm-bot-manager --kube-version 1.21.1 \
  --values examples/values-harbor.yaml

helm package .
```

From a source checkout's repository root, set `chart=deploy/helm/mm-bot-manager`
and use `"$chart"` in place of `.` and `"$chart"/examples/...` in place of
`examples/...`.

The packaged chart contains all templates, values, examples, and this README;
it does not download dependencies. Use `examples/values-external.yaml` for an
external PostgreSQL service, `examples/values-internal-longhorn.yaml` for the
production internal profile, `examples/values-internal-local-path.yaml` only for
a single-node/test cluster, and `examples/values-harbor.yaml` after mirroring
both images to Harbor.

Harbor may use HTTP or HTTPS. Keep image repositories as `host[:port]/project/image`
without a URL scheme. Configure the selected transport and any private CA on the
publishing daemon/BuildKit builder and every eligible Kubernetes node;
`imagePullSecrets` only provide authentication. In a source checkout, follow
`docs/kubernetes.md` sections 2.1–2.4 for Docker, BuildKit, containerd/K3s examples
and per-node pull verification before installation or credential rotation.

## Installation and acceptance

Do not install an example profile without a site overlay. For a source checkout,
follow `docs/kubernetes.md`: run `./scripts/kubernetes-deploy.sh init`, then edit
`.local/kubernetes/deployment.env` once to choose the profile and kube-context, and fill the site
overlay. `init` preserves existing files. Prepare images, namespace and Secrets
before checking the installation.

New configuration, Secret working copies and image/backup exports use
`.local/kubernetes/`, excluded from Git, Docker contexts and Helm packages.
Existing root configuration and Secret files continue to work in place. If both
layouts contain a configuration, select one explicitly with `--config FILE` on
every helper command. `secret-path KIND` resolves the selected Secret working file.
Custom paths inside a Git checkout must be ignored and untracked. See the runbook
for duplicate handling and custom build-context exclusions. Git is required.

```bash
./scripts/kubernetes-deploy.sh check
```

Review the context, profile, database mode and effective StorageClasses, then:

```bash
./scripts/kubernetes-deploy.sh apply
```

Both commands reload the saved configuration, including in a new terminal. There
is no second profile assignment. `apply` repeats site validation and the
Helm preview (`upgrade --install --dry-run=server --hide-secret`), stopping on any
failure before installation. The preview reads release/cluster state but does not
perform actual upgrade PATCHes or establish admission-policy acceptance. It avoids
client-side kubectl apply against Helm-owned resources and suppresses rendered
manifest output. It uses the
Helm-version-appropriate rollback flag and waits for Jobs. For an upgrade, edit
the existing overlay, take a verified backup, then repeat these two commands.
The helper belongs to the source checkout; a standalone packaged chart does not
include repository scripts or the operator's local configuration.

With internal backups enabled, an ordinary `backup-pvc-check-<revision>` Job
mounts the backup PVC immediately. This supplies the first consumer required by
`WaitForFirstConsumer` storage such as `local-path`, without waiting for the backup
schedule. It writes, reads back and deletes one uniquely named temporary file;
existing archives are untouched. It uses the same PostgreSQL image, pull policy,
pull Secrets, resources and writable mount as the backup CronJob, with no database
credentials or connection. It has a ten-minute deadline and no retries.

Keep both `--wait` and `--wait-for-jobs`: Helm must wait for the successful file
check as well as PVC binding. Helm 3 uses `--atomic`; Helm 4 uses
`--rollback-on-failure`. The Job is not a hook: a `post-install` hook would run
after Helm's readiness wait and could not unblock the PVC. A new revision creates
a new Job, avoiding immutable-template updates. Helm removes the previous check
Job during upgrade; the latest Job and its logs remain until the next upgrade or
uninstall. The check Job has no `keep` annotation; the backup PVC remains retained.
On `local-path`, the
first consumer selects the volume's node; keep the check and backup CronJob's
scheduling compatible. Neither currently sets explicit scheduling constraints.

This block runs from the source root; from an unpacked chart, use `.` for the
chart and the equivalent edited overlay. Scripts and the full runbook live in the
source checkout, not the chart archive. A successful PVC check does not create a
database backup or prove the bot answers commands. Verify PVCs, authenticated
WebSocket logs, `!help`/catalog permissions, persistent preferences and an actual backup plus
isolated restore as described in the runbook.
