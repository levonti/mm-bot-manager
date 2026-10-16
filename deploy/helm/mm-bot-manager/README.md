# mm-bot-manager Helm chart

This self-contained Helm 3 chart deploys one `mm-bot-manager` Pod to Kubernetes
`>= 1.21.1`. It has no chart dependencies and is suitable for a disconnected
cluster after the required images have been mirrored to its local registry.

The application maintains a Mattermost WebSocket connection; it does not expose
an application HTTP API. Consequently, the chart deliberately creates neither a
Service nor an Ingress. The Pod-local port `8080` is only used by Kubernetes
health probes.

## Prerequisites

- Kubernetes `>= 1.21.1` and Helm 3;
- an already-created application Secret (`MM_TOKEN`, `MM_BOT_TOKEN`);
- either an already-created external database Secret (`DB_URL`) or an
  already-created PostgreSQL Secret (`POSTGRES_USER`, `POSTGRES_PASSWORD`,
  `POSTGRES_DB`);
- one application image for external mode; application and PostgreSQL images for
  internal mode. Both can be referenced by tag or immutable digest.

## Deployment modes

| Mode | Rendered resources | Required Secrets | Images |
| --- | --- | --- | --- |
| `database.mode=external` (default) | Deployment, ConfigMap, ServiceAccount | application, external database | application |
| `database.mode=internal` | external resources plus headless PostgreSQL Service, 1-replica PostgreSQL StatefulSet and data PVC, backup PVC and CronJob (when enabled), optional restore Job | application, PostgreSQL | application, PostgreSQL |

The application Deployment is always one replica with `Recreate` strategy. The
internal PostgreSQL StatefulSet is always one replica. Do not use the internal
mode as a high-availability database.

Use `longhorn` for the production database and backup PVCs. `local-path` is
supported for a single-node/test cluster only: data and backups live on one node
and disappear if that node is lost.

## Values and defaults

Passwords and tokens do not belong in values. Secret names below are contracts:
the chart reads the listed fixed keys and does not create Secrets itself.

| Section | Default | Purpose |
| --- | --- | --- |
| `image` | `repository=mm-bot-manager`, `tag=""` (chart app version), `digest=""`, `pullPolicy=IfNotPresent` | Application image; a non-empty digest takes precedence over the tag. |
| `imagePullSecrets` | `[]` | Pod image pull Secret references, e.g. `[{name: harbor-pull}]`. |
| `nameOverride`, `fullnameOverride` | `""`, `""` | Override generated resource names. |
| `app.existingSecret` | `mm-bot-manager-app` | Secret with `MM_TOKEN` and `MM_BOT_TOKEN`. |
| `mattermost` | `url=https://mm.example.com`, `approvers=""`, `verifySSL=true` | Mattermost endpoint and approval/TLS settings. Imports require nonempty `approvers`. |
| `logLevel` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `health` | `host=0.0.0.0`, `port=8080` | Pod-local liveness/readiness listener. |
| `probes` | startup `5s/2s/30`; readiness `10s/2s/3`; liveness `10s/2s/6` | Probe period / timeout / failure threshold. |
| `serviceAccount` | `create=true`, `name=""` | Token-less Pod ServiceAccount. |
| `podAnnotations`, `podLabels` | `{}`, `{}` | Additional application Pod metadata. |
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
| `database.internal.backup` | enabled, `0 2 * * *`, 14 days, history `3/3`, `longhorn`, `ReadWriteOnce`, `5Gi`, requests `50m/64Mi`, limits `500m/256Mi` | Logical dump CronJob and retained backup PVC. |
| `database.internal.restore` | `enabled=false`, `file=""` | Opt-in one-time restore Job. The file must be a safe `.dump` basename. |

## Fixed Secret key contracts

| Secret value | Required keys |
| --- | --- |
| `app.existingSecret` | `MM_TOKEN`, `MM_BOT_TOKEN` |
| `database.external.existingSecret` | `DB_URL` |
| `database.internal.auth.existingSecret` | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` |

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
