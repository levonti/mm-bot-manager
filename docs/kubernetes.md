# Kubernetes deployment runbook

Deploy `mm-bot-manager` with Helm 3 or 4 on Kubernetes `>= 1.21.1`, using either an
external PostgreSQL service or one internal PostgreSQL StatefulSet. The chart has
no Helm dependencies. All commands below run in **Bash from the repository root**.
The helper scripts require Bash, Helm and kubectl; no Python installation is
needed on the operator's workstation.

The internal database has **one replica and no HA failover**. Data and logical
backups use separate retained PVCs. The application connects outbound to Mattermost
REST/WebSocket and has no inbound user API, application Service or Ingress.
Port 8080 is a health interface bound to the Pod network, not a public endpoint.

## 1. Prerequisites and site configuration

Before creating resources:

- Complete [Mattermost setup](../README.md#mattermost-setup): enable bot accounts
  and PATs, create an active human system-admin PAT (`MM_TOKEN`), and a separate
  manager bot token (`MM_BOT_TOKEN`). Add the bot to any channels it will serve.
- Choose existing Mattermost usernames for approvers and catalog viewers. Unknown
  usernames prevent startup. Empty `mattermost.approvers` **disables imports and
  makes creation immediate**. Approvers and `catalogViewers` can read `!catalog`
  in DM; ordinary users cannot read the global catalog.
- Confirm the intended kube-context, namespace/RBAC permissions and node image
  architecture. The operator needs Helm resource/Secret access plus permissions
  for Jobs, Pod logs/waits, Deployment scaling and ConfigMap maintenance locks.
- Verify DNS and egress: Pods need Mattermost HTTPS/WSS, internal PostgreSQL TCP
  5432 and cluster DNS; nodes need access to the image registry. Proxies must
  support the Mattermost WebSocket upgrade and long-lived connections.
- Ensure the chosen StorageClass exists, provisions PVCs and has sufficient free
  capacity. Longhorn is recommended; `local-path` is single-node/test storage.
  Two PVCs on one storage cluster are not independent disaster recovery.

```bash
kubectl config current-context
kubectl get nodes -o custom-columns=NAME:.metadata.name,ARCH:.status.nodeInfo.architecture
kubectl get storageclass
```

Choose one database profile, then create a **mandatory site overlay**. Profiles
are reusable storage examples; their example image/URL defaults are not an
installation configuration.

| Profile | Values file | Required images/storage |
| --- | --- | --- |
| Internal production | `values-internal-longhorn.yaml` | Application + PostgreSQL; Longhorn data/backup PVCs |
| Internal single-node/test | `values-internal-local-path.yaml` | Application + PostgreSQL; node-local PVCs |
| Internal Harbor/closed contour | `values-harbor.yaml` | Both images mirrored to Harbor; Longhorn PVCs |
| External PostgreSQL | `values-external.yaml` | Application image; database managed externally |

```bash
chart="deploy/helm/mm-bot-manager"
profile="$chart/examples/values-internal-longhorn.yaml"
site="values-site.yaml"
cp "$chart/examples/values-site.yaml.example" "$site"
```

Edit the copy and replace **every `CHANGE_ME`**: application repository and
immutable tag/digest, PostgreSQL repository, real `mattermost.url`, approver and
catalog viewer usernames. Set `defaultLanguage` to `ru` or `en`; users can later
select `!language ru|en|auto` and `!catalog --format cards|table`. Keep
`verifySSL: true`. Set `imagePullSecrets` to your registry Secret, or `[]` only for
images that all nodes can pull without authentication. For external mode the
internal image settings are unused. No token, password or `DB_URL` belongs in
values. The root `values-site.yaml` is ignored by Git and excluded from Docker
builds; preserve it in approved configuration storage for future upgrades.

## 2. Prepare images and registry authentication

Build on a connected staging machine for the **cluster architecture**, not
implicitly for the build laptop. For a mixed cluster publish a multi-platform
image with `docker buildx build --platform linux/amd64,linux/arm64 --push` instead.
For example, for AMD64 nodes:

```bash
registry="harbor.internal.example" # replace with the actual registry
image_tag="$(git rev-parse --short=12 HEAD)"
docker build --platform linux/amd64 -t "$registry/platform/mm-bot-manager:$image_tag" .
docker push "$registry/platform/mm-bot-manager:$image_tag"
docker pull --platform linux/amd64 postgres:15.18-alpine3.24
docker tag postgres:15.18-alpine3.24 "$registry/library/postgres:15.18-alpine3.24"
docker push "$registry/library/postgres:15.18-alpine3.24"
docker image inspect "$registry/platform/mm-bot-manager:$image_tag" --format '{{json .RepoDigests}}'
docker image inspect "$registry/library/postgres:15.18-alpine3.24" --format '{{json .RepoDigests}}'
```

Put these actual repositories and tags, preferably their published `sha256`
digests, into the site overlay. A digest takes precedence over a tag. The above
single-platform PostgreSQL mirror is only suitable for nodes of that architecture.
For a mixed cluster mirror its manifest list with the approved registry tool.

In a closed contour, transfer image archives and checksums through the approved
channel. Run `docker save --output images.tar IMAGE1 IMAGE2` in staging and
`sha256sum images.tar >images.tar.sha256`. On the destination run
`sha256sum --check images.tar.sha256`, then `docker load --input images.tar`, tag
and **push into the destination registry**, and verify its published digests.
A loaded image on the operator's laptop is not present on Kubernetes nodes;
`RepoDigests` may be empty until the image is published. Transfer the source/chart,
Helm/kubectl binaries and any local validation schemas as well.

For a private Harbor CA, configure trust in the container runtime on **every
node**, and on the build/mirroring workstation. `imagePullSecrets` authenticate
pulls and do not install registry CA trust. Runtime refreshes are platform-specific
maintenance operations.

Create the namespace idempotently:

```bash
kubectl create namespace mm-bot-manager --dry-run=client -o yaml | kubectl apply -f -
```

Create or rotate the pull Secret using an isolated Docker configuration. The
password is read at a hidden prompt and passed only on stdin; it never becomes a
process argument. The temporary credential file is mode 0600 inside a private
directory and is removed on exit. Do not enable shell tracing (`set -x`).

```bash
(
  set -euo pipefail
  umask 077
  registry="harbor.internal.example" # replace with the actual registry
  auth_dir="$(mktemp -d)"
  trap 'rm -rf "$auth_dir"' EXIT
  read -r -p 'Harbor username: ' registry_user
  read -r -s -p 'Harbor password: ' registry_password
  printf '\n'
  printf '%s' "$registry_password" | docker --config "$auth_dir" login "$registry" \
    --username "$registry_user" --password-stdin
  unset registry_password
  kubectl -n mm-bot-manager create secret generic harbor-pull \
    --type=kubernetes.io/dockerconfigjson \
    --from-file=".dockerconfigjson=$auth_dir/config.json" \
    --dry-run=client -o yaml | kubectl -n mm-bot-manager apply -f -
)
```

Repeat this block for rotation; update in place rather than deleting the Secret.
A restart with `IfNotPresent` can reuse cached images and does not prove new
registry credentials work. Validate rotation with a planned upgrade to a new
immutable digest that requires a pull; test PostgreSQL repository permissions
separately during an approved maintenance window.

## 3. Application/database Secrets and Mattermost CA

Working env copies must contain real values only locally. With `umask 077`, copy
and edit the examples using an editor; never paste credentials into shell commands,
values, logs or tickets. The Secret creation pipelines below do not print values.

```bash
umask 077
cp deploy/helm/mm-bot-manager/examples/app-secret.env.example app-secret.env
chmod 0600 app-secret.env
# Edit MM_TOKEN and MM_BOT_TOKEN before continuing.
kubectl -n mm-bot-manager create secret generic mm-bot-manager-app \
  --from-env-file=app-secret.env --dry-run=client -o yaml | kubectl apply -f -
```

**Internal mode:** use exactly `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`.
Choose a dedicated database name, not `postgres`, `template0` or `template1`.
The chart passes these values as `DB_USER`, `DB_PASSWORD`, `DB_NAME` to the
application, with the internal Service as `DB_HOST`; it does not construct a URL.

```bash
cp deploy/helm/mm-bot-manager/examples/internal-db-secret.env.example internal-db-secret.env
chmod 0600 internal-db-secret.env
# Edit the password and, if necessary, the dedicated username/database name.
kubectl -n mm-bot-manager create secret generic mm-bot-manager-postgresql \
  --from-env-file=internal-db-secret.env --dry-run=client -o yaml | kubectl apply -f -
```

**External mode only:** use the operator-supplied TLS-appropriate `DB_URL`.

```bash
cp deploy/helm/mm-bot-manager/examples/external-db-secret.env.example external-db-secret.env
chmod 0600 external-db-secret.env
# Edit DB_URL before continuing.
kubectl -n mm-bot-manager create secret generic mm-bot-manager-external-db \
  --from-env-file=external-db-secret.env --dry-run=client -o yaml | kubectl apply -f -
```

Check **key names only**, substituting the relevant Secret name:

```bash
kubectl -n mm-bot-manager get secret mm-bot-manager-postgresql \
  -o go-template='{{range $key, $value := .data}}{{printf "%s\n" $key}}{{end}}'
```

Filled env copies are ignored by Git and excluded from image/chart packaging.
Remove them after Secret creation or move them into the approved vault. For PAT
or external `DB_URL` rotation, repeat the relevant creation/apply pipeline, then
restart the application and complete acceptance checks. Updating the internal
`POSTGRES_PASSWORD` Secret **does not alter the existing PostgreSQL role**: first
coordinate `ALTER ROLE`, then update the matching Secret and restart clients and
the StatefulSet in a maintenance window. Never change `POSTGRES_USER`/`POSTGRES_DB`
on an existing data PVC as if it were an initialization procedure.

For a private **Mattermost** CA (separate from registry trust), create a ConfigMap
containing a complete PEM trust bundle: the organization's CA chain plus any
public roots required by your endpoint. Configure `mattermost.caBundle.existingConfigMap`
as `mm-bot-manager-ca` in the site overlay, keeping `verifySSL: true`.

```bash
kubectl -n mm-bot-manager create configmap mm-bot-manager-ca \
  --from-file=ca-bundle.crt=/protected/config/mattermost-trust-bundle.pem \
  --dry-run=client -o yaml | kubectl apply -f -
```

The chart mounts the bundle read-only and sets `SSL_CERT_FILE` for both HTTP and
WebSocket clients. Restart the application after changing the external ConfigMap;
its contents are not part of the chart's rollout checksum. This CA contains no
private key. Publicly trusted Mattermost needs no additional bundle.

## 4. Validate and install the exact configuration

Use the same **profile followed by site overlay** for validation, installation and
upgrades. The site validator rejects built-in image/URL placeholders; rendering
alone does not test image availability, real credentials or network/storage health.

```bash
chart="deploy/helm/mm-bot-manager"
profile="$chart/examples/values-internal-longhorn.yaml"
site="values-site.yaml"
./scripts/verify-kubernetes-site.sh "$profile" "$site"
helm template mm-bot-manager "$chart" --namespace mm-bot-manager \
  --values "$profile" --values "$site" | kubectl -n mm-bot-manager apply --dry-run=server -f -
case "$(helm version --template '{{.Version}}')" in
  v3.*) rollback_flag=--atomic ;;
  v4.*) rollback_flag=--rollback-on-failure ;;
  *) printf '%s\n' 'Helm 3 or 4 is required' >&2; exit 1 ;;
esac
helm upgrade --install mm-bot-manager "$chart" --namespace mm-bot-manager \
  --values "$profile" --values "$site" \
  "$rollback_flag" --wait --wait-for-jobs --timeout=10m
```

For `local-path`, Harbor or external mode change `profile` to the selected file
from section 1. Never install an unedited example directly. Before deployment to
a disconnected cluster, validate manifests with local Kubernetes schemas using
`KUBECONFORM_SCHEMA_LOCATION=/path/to/1.21.1-schemas ./scripts/verify-kubernetes.sh`.
The server-side dry run also catches the destination cluster's admission policies;
it creates no persistent objects. Initial storage provisioning can need more than
Helm's default five minutes, hence the explicit timeout.

Chart 0.1.2 adds an ordinary `backup-pvc-check-<revision>` Job whenever internal
backups are enabled. It mounts the backup PVC immediately, allowing
`WaitForFirstConsumer` storage to provision before the first scheduled backup.
The Job writes, reads back and deletes a unique temporary file with restrictive
permissions. It does not connect to PostgreSQL, read database Secrets, create a
dump or change existing archives. It inherits the backup CronJob's PostgreSQL
image/digest, pull policy, image pull Secrets, resources and writable mount.

Keep `--wait --wait-for-jobs` and the version-appropriate rollback flag together:
`--wait` alone can accept a Bound PVC before its write check completes. Do not
move this consumer into a `post-install` hook, which runs after the readiness
wait. A new Helm revision creates a new check Job, so image/spec changes do not
patch an immutable Job template. Helm removes the previous check Job during
upgrade; the latest Job/logs remain for diagnosis until the next upgrade or
uninstall. There is no TTL cleanup or `keep` policy on this Job. The check has
`backoffLimit: 0` and `activeDeadlineSeconds: 600`; fix the cause and retry the
upgrade after failure. Automatic rollback can remove the failed revision's Job.

For `local-path`, the first consumer determines the backup volume's node. The
check and backup CronJob currently have no explicit scheduling constraints; keep
them compatible if adding node selectors, affinity or tolerations later. Increasing
the timeout cannot supply a missing consumer. Disabling backups or selecting
external PostgreSQL creates no PVC-check Job.

Upgrading from chart 0.1.0 adds the `application` component label without changing
the immutable Deployment selector. Wait for that rollout before using the database
maintenance helper. Helper operations render Jobs using **`helm get values --all`
from the installed release**, preserving its image digests, pull Secrets, database
Secret names and PVC overrides. They do not fall back to a different example file.

## 5. Acceptance and diagnosis

Select application Pods explicitly; the release also contains PostgreSQL and
backup Pods. These commands must identify exactly one running application Pod:

```bash
app_selector='app.kubernetes.io/instance=mm-bot-manager,app.kubernetes.io/component=application'
kubectl -n mm-bot-manager get pods -l "$app_selector" -o wide
kubectl -n mm-bot-manager get pvc -l app.kubernetes.io/instance=mm-bot-manager
kubectl -n mm-bot-manager logs -l "$app_selector" -c mm-bot-manager --tail=100
app_pod="$(kubectl -n mm-bot-manager get pods -l "$app_selector" -o jsonpath='{.items[0].metadata.name}')"
test -n "$app_pod"
kubectl -n mm-bot-manager port-forward "pod/$app_pod" 8080:8080
```

Run in another terminal:

```bash
curl --fail http://127.0.0.1:8080/health/live
curl --fail http://127.0.0.1:8080/health/ready
```

Both endpoints currently check the database connection. A 200 response proves DB
reachability, **not a connected/authenticated Mattermost WebSocket**.

With internal backups enabled, also inspect the completed PVC-check Job:

```bash
check_selector='app.kubernetes.io/instance=mm-bot-manager,app.kubernetes.io/component=backup-pvc-check'
kubectl -n mm-bot-manager get jobs -l "$check_selector"
kubectl -n mm-bot-manager logs -l "$check_selector" -c backup-pvc-check --tail=20
```

Acceptance requires all of the following:

1. All intended PVCs are Bound; PostgreSQL and application Pods are Ready, with no
   repeated restarts, image-pull, TLS, permission or volume-attachment errors.
   With internal backups enabled, the PVC-check Job is Complete and its log says
   `backup PVC write/read/delete check passed`. This proves mount access only;
   the real backup and isolated restore in step 4 are still required.
2. Application logs contain `logged_in` and `ws_authenticated`. In a DM, `!help`
   responds. An approver/viewer can read `!catalog`; an ordinary user is denied.
3. Set `!language ru` and `!catalog --format table` as a permitted viewer, restart
   the application with the commands below, then verify `!language`, `!help` and
   `!catalog` retain those preferences. Switch back to your desired language/format.
4. In internal mode run an on-demand backup, inspect its archive and run **verify**
   from section 6. Record the dump filename and successful isolated restore result.
   Export backups off-cluster and rehearse the complete restore procedure in a test
   release before relying on it for production recovery.

Application restart (also required after token/CA rotation):

```bash
kubectl -n mm-bot-manager rollout restart deployment/mm-bot-manager
kubectl -n mm-bot-manager rollout status deployment/mm-bot-manager --timeout=10m
```

For `Pending`/`CrashLoopBackOff`, inspect `kubectl describe pod`, namespace events
and application/PostgreSQL logs. Check PVC provisioning/capacity, image architecture,
registry CA/authentication, Secret **key names**, database password consistency,
Mattermost account/PAT permissions, DNS, HTTPS and WSS. Do not print Secret data or
paste connection strings into reports. Custom release/fullname overrides require
substituting the actual Deployment name in the restart commands.

## 6. Internal PostgreSQL backup, inspection and full verification

Internal mode schedules custom-format dumps at `0 2 * * *` by default, with 14-day
retention. The schedule uses the controller's timezone (not necessarily local time).
Each filename includes a Pod UID so concurrent/manual backups cannot overwrite
another archive created in the same second. A separate retained backup PVC stores
files; `pg_restore --list` checks the archive catalog before publication.

Use the checked helper from this source checkout:

```bash
./scripts/kubernetes-db.sh backup --namespace mm-bot-manager --release mm-bot-manager
./scripts/kubernetes-db.sh inspect --namespace mm-bot-manager --release mm-bot-manager
./scripts/kubernetes-db.sh verify --namespace mm-bot-manager --release mm-bot-manager \
  --file mm-bot-manager-TIMESTAMP-POD_UID.dump
```

Replace the sample basename with an actual filename printed by backup/inspect.
The helper takes a per-release ConfigMap lock, preserves the CronJob's previous
suspend state, pauses scheduling and waits up to ten minutes for existing backup
Jobs (including manual ones). Do not start manual Jobs or concurrent maintenance
outside this helper. It uses the installed image/digest and pull Secrets for all
Jobs, including the inspector; waits for completion; prints logs; and deletes
terminal Jobs **with foreground deletion of their Pods** before releasing storage.
No PVC is deleted. RWO inspection therefore does not race a backup on another node.

`verify` mounts the archive read-only, restores it fully into a uniquely named
`mm_verify_...` database on the same PostgreSQL instance and queries the bot, token
and pending-request tables. It never drops or writes to the live application DB.
The temporary database is removed on success, restore failure or a handled signal.
Budget enough PostgreSQL data space for this additional database. A node crash or
SIGKILL can leave a temporary database behind: the operator must check that its
verification Job/Pods are gone before removing that exact temporary database.
This is a logical recovery test; it is not independent recovery from loss of the
PostgreSQL node/storage cluster.

If no dump exists, `inspect` reports the directory contents and `verify` fails.
A failed/timed-out/unknown Job is not reported as a successful backup. The helper
leaves a possibly running Job and maintenance lock in place, with scheduling
suspended, instead of automatically resuming conflicting work. See section 7.
If a suspend API response itself was lost, inspect the CronJob's actual suspend
state before any recovery action; the helper has not started a maintenance Job.

## 7. Destructive restore and failure recovery

Test the procedure in an isolated release first and export a current backup.
`restore` replaces the target application database. Select a reviewed archive and
explicitly acknowledge replacement:

```bash
./scripts/kubernetes-db.sh restore --namespace mm-bot-manager --release mm-bot-manager \
  --file mm-bot-manager-TIMESTAMP-POD_UID.dump --confirm-replace-database
```

The helper uses installed values, locks maintenance, suspends backup scheduling,
waits for backup Jobs, then performs a **full isolated verification** of the archive.
Only after that succeeds does it scale the application to zero, wait for the
Deployment controller and then **wait for deletion of all application Pods**.
A final Pod check must be empty before the restore Job is created. PostgreSQL
and backup Pods are excluded by the component selector. No `dropdb` is attempted
if verification or application termination fails.

The restore Job validates the archive, refuses system database names, replaces
the dedicated DB and restores its contents. It runs once (`backoffLimit: 0`),
without repeatedly dropping the DB on failure. On success the helper waits for
foreground Job/Pod deletion, restores the application's original replica count
(0 or 1), waits for rollout and restores the previous CronJob suspend state.
Repeat the acceptance checks after recovery. Helm readiness is not functional
acceptance, and Helm rollback does not undo database contents/schema changes.

On failure, the application is **not automatically restarted**. A failed destructive
restore or an unknown/running Job leaves the CronJob suspended and ConfigMap lock
retained. A failure before destructive restore may leave the application at zero;
check it explicitly. Do not blindly delete the lock, resume backups or start the
application against an incomplete database.

Inspect non-secret recovery information and all candidate Jobs:

```bash
kubectl -n mm-bot-manager get configmap mm-bot-manager-db-maintenance -o yaml
kubectl -n mm-bot-manager get jobs \
  -l 'app.kubernetes.io/instance=mm-bot-manager,app.kubernetes.io/component in (backup,inspection,verification,restore)'
```

If creation was interrupted and the helper could not obtain a Job name, identify
it from these labels. Wait for each candidate to become terminal, or, after an
explicit decision to abandon it, delete **that exact Job** with
`kubectl delete job JOB_NAME --cascade=foreground --wait=true --timeout=10m`.
Verify no maintenance Pods remain. A failed restore can leave a missing/partial
database even after the Job is gone; keep the application stopped until a successful
reviewed restore or other database recovery.

Record `originalReplicas` and `backupWasSuspended` from the lock. Once all conflicting
Jobs/Pods are gone and the recovery plan is agreed, remove only the maintenance
ConfigMap lock and rerun restore if needed. A CronJob that is already suspended is
left suspended by the next run. After successful DB/application acceptance, resume
it manually only if the saved `backupWasSuspended` was `false`:

```bash
# Run only after the recovery conditions above have been met.
kubectl -n mm-bot-manager delete configmap mm-bot-manager-db-maintenance
# If the saved state was false and recovery/acceptance are complete:
kubectl -n mm-bot-manager patch cronjob mm-bot-manager-backup \
  --type=merge -p '{"spec":{"suspend":false}}'
```

## 8. Upgrade, rollback, retained storage and disaster recovery

Upgrade with the same profile and site overlay after validation from section 4.
Check the application/PostgreSQL image changes and take a verified backup first.
Do not change PostgreSQL major versions on the existing PVC without a separate
migration procedure. Inspect Helm history before rolling back an application/chart
revision; rollback cannot reverse a database migration or restore.

```bash
helm -n mm-bot-manager history mm-bot-manager
helm -n mm-bot-manager rollback mm-bot-manager REVISION --wait --wait-for-jobs --timeout=10m
```

Before uninstalling, stop maintenance/backup activity and export verified backups.
List and record the exact data/backup PVC names. Uninstall retains both:

```bash
kubectl -n mm-bot-manager get pvc -l app.kubernetes.io/instance=mm-bot-manager -o wide
helm -n mm-bot-manager uninstall mm-bot-manager
```

Do not delete retained PVCs as part of routine uninstall. Their eventual removal
requires a separate explicit operator decision after exports and recovery have
been validated. Longhorn backups must also reach a separate S3/NFS target and be
restore-tested. With `local-path`, copy verified archives off the node: losing that
node loses both local volumes.

## 9. Repository verification

Chart/workflow regression tests run with the normal test suite. They render actual
Jobs and simulate API failures, terminating Pods, locks and previous suspend/replica
states. PVC-check tests execute its rendered shell script against disposable local
files, checking success, storage failures, temporary-file cleanup and archive
preservation; render tests cover revisions and Harbor settings. These do not
exercise Kubernetes scheduling or dynamic provisioning. A separate test executes
the rendered PVC-check and PostgreSQL scripts in a disposable Docker server; it
restores real data, rejects a truncated data stream with a valid
archive catalog, and checks cleanup without touching an operator database.

```bash
uv sync --frozen --extra postgres
uv run --no-sync pytest -q tests/test_helm_chart.py tests/test_backup_pvc_check.py tests/test_kubernetes_runbook.py
MM_TEST_BACKUP_DOCKER=1 uv run --no-sync pytest -q tests/test_kubernetes_backup_roundtrip.py
```

The Docker drill is opt-in and skipped by the standard CI job, which has only a
PostgreSQL service. Report it separately from chart/schema tests and any live
cluster acceptance; none of these substitutes for storage/node-loss recovery.
