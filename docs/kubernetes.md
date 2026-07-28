# Kubernetes deployment runbook

This runbook installs `mm-bot-manager` with Helm 3 on Kubernetes `>= 1.21.1`.
It supports an external PostgreSQL server or one in-cluster PostgreSQL StatefulSet
with persistent logical backups. The chart has no external Helm dependencies, so
it is usable in a closed contour once its images are available locally.

The application keeps an outbound Mattermost WebSocket connection. It does not
serve an end-user HTTP application, therefore it intentionally has no application
Service and no Ingress. An ingress-nginx installation is not used by this chart.
Port 8080 is only a Pod-local health endpoint for Kubernetes probes.

## 1. Prerequisites and profiles

Install Helm 3 and ensure the target cluster is Kubernetes `1.21.1` or newer.
Choose one of these profiles before creating database credentials:

| Profile | Values file | Database resources | Storage guidance |
| --- | --- | --- | --- |
| External PostgreSQL | `examples/values-external.yaml` | Application Deployment only | Managed by the external database operator. |
| Internal production PostgreSQL | `examples/values-internal-longhorn.yaml` | Application Deployment, one PostgreSQL StatefulSet, data PVC, backup PVC and CronJob | Use `longhorn` for both PVCs. |
| Internal single-node/test | `examples/values-internal-local-path.yaml` | Same as internal production | `local-path` is node-local and is not disaster recovery. |
| Closed contour | `examples/values-harbor.yaml` | Internal profile with images from Harbor | Use after mirroring both application and PostgreSQL images. |

The internal mode has exactly one PostgreSQL replica and is not an HA database.
All Helm values are non-secret; Kubernetes Secrets must be created before install.

## 2. Create namespace and application Secret

Copy the example, replace both `CHANGE_THIS_...` values in the local copy only,
and never commit or paste those values into Helm values, shell history, tickets,
or logs.

```bash
kubectl create namespace mm-bot-manager

cp deploy/helm/mm-bot-manager/examples/app-secret.env.example app-secret.env
chmod 0600 app-secret.env
kubectl -n mm-bot-manager create secret generic mm-bot-manager-app \
  --from-env-file=app-secret.env \
  --dry-run=client -o yaml | kubectl apply -f -
```

The Secret must contain these exact keys: `MM_TOKEN` (a system-admin Mattermost
PAT) and `MM_BOT_TOKEN` (the manager bot PAT). Check only the key names; this
does not decode or print any secret values:

```bash
kubectl -n mm-bot-manager get secret mm-bot-manager-app \
  -o go-template='{{range $key, $value := .data}}{{printf "%s\n" $key}}{{end}}'
```

## 3. Create the database Secret

Use exactly one database mode for a release.

### External PostgreSQL

Fill `DB_URL` with the complete TLS-appropriate PostgreSQL connection URL supplied
by the database operator. The URL is the only required key in the Secret.

```bash
cp deploy/helm/mm-bot-manager/examples/external-db-secret.env.example \
  external-db-secret.env
chmod 0600 external-db-secret.env
kubectl -n mm-bot-manager create secret generic mm-bot-manager-external-db \
  --from-env-file=external-db-secret.env \
  --dry-run=client -o yaml | kubectl apply -f -
```

### Internal PostgreSQL

Fill the local copy with a strong unique password. The three fixed keys are
`POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB`. The application reads
the latter three values as component database settings; it does not need a
constructed `DB_URL` in internal mode.

```bash
cp deploy/helm/mm-bot-manager/examples/internal-db-secret.env.example \
  internal-db-secret.env
chmod 0600 internal-db-secret.env
kubectl -n mm-bot-manager create secret generic mm-bot-manager-postgresql \
  --from-env-file=internal-db-secret.env \
  --dry-run=client -o yaml | kubectl apply -f -
```

For either Secret, list keys without disclosing values by substituting its name in
the following command:

```bash
kubectl -n mm-bot-manager get secret mm-bot-manager-app \
  -o go-template='{{range $key, $value := .data}}{{printf "%s\n" $key}}{{end}}'
```

Filled `*.env` working copies are ignored by Git. Keep them at mode `0600` and,
after creation, delete them or move them to the organization's approved secret
vault. Do not leave copies in the deployment directory.

### Rotation

For an application token or external `DB_URL`, update the protected local env
file and repeat the same `kubectl create secret ... --dry-run=client -o yaml |
kubectl apply -f -` command for that Secret. Then restart and wait for the
application Pod:

```bash
kubectl -n mm-bot-manager rollout restart deployment/mm-bot-manager
kubectl -n mm-bot-manager rollout status deployment/mm-bot-manager
```

Changing only `POSTGRES_PASSWORD` in the internal Secret does **not** change the
password stored by PostgreSQL. Coordinate an `ALTER ROLE` with the database
administrator first, apply the matching Secret, and restart the PostgreSQL
StatefulSet and application during a maintenance window. Verify recovery before
declaring the rotation complete.

## 4. Harbor and closed-contour preparation

For a connected staging environment, build and mirror both internal-mode images
to Harbor. External mode needs only the application image.

```bash
docker build -t harbor.internal.example/platform/mm-bot-manager:0.1.0 .
docker push harbor.internal.example/platform/mm-bot-manager:0.1.0
docker pull postgres:15.18-alpine3.24
docker tag postgres:15.18-alpine3.24 \
  harbor.internal.example/library/postgres:15.18-alpine3.24
docker push harbor.internal.example/library/postgres:15.18-alpine3.24
```

For physically separated contours, export the already-mirrored images, transfer
the tarball and checksum through the approved channel, verify the checksum in the
destination, load the images into the destination registry workflow, and inspect
the resolved digest before publishing or deploying it:

```bash
docker save \
  --output mm-bot-manager-images.tar \
  harbor.internal.example/platform/mm-bot-manager:0.1.0 \
  harbor.internal.example/library/postgres:15.18-alpine3.24
sha256sum mm-bot-manager-images.tar >mm-bot-manager-images.tar.sha256
sha256sum --check mm-bot-manager-images.tar.sha256
docker load --input mm-bot-manager-images.tar
docker image inspect harbor.internal.example/platform/mm-bot-manager:0.1.0 \
  --format '{{json .RepoDigests}}'
```

Create the Harbor pull Secret without placing the password in the command line or
shell history. Enter the password at the hidden prompt. The Harbor values profile
already refers to this Secret as `harbor-pull`.

```bash
read -r HARBOR_USERNAME
read -rs HARBOR_PASSWORD
kubectl -n mm-bot-manager create secret docker-registry harbor-pull \
  --docker-server=harbor.internal.example \
  --docker-username="$HARBOR_USERNAME" \
  --docker-password="$HARBOR_PASSWORD"
unset HARBOR_PASSWORD
```

`imagePullSecrets` authenticates image pulls; it does not make a private Harbor
CA trusted. Install the Harbor CA in containerd, CRI-O, or Docker configuration
on **every cluster node** and restart/reload that runtime as required by the
platform before deployment.

## 5. Validate and install

Render before changing cluster state:

```bash
helm lint deploy/helm/mm-bot-manager
helm template mm-bot-manager deploy/helm/mm-bot-manager \
  --namespace mm-bot-manager --kube-version 1.21.1 \
  --values deploy/helm/mm-bot-manager/examples/values-external.yaml
helm template mm-bot-manager deploy/helm/mm-bot-manager \
  --namespace mm-bot-manager --kube-version 1.21.1 \
  --values deploy/helm/mm-bot-manager/examples/values-internal-longhorn.yaml
helm template mm-bot-manager deploy/helm/mm-bot-manager \
  --namespace mm-bot-manager --kube-version 1.21.1 \
  --values deploy/helm/mm-bot-manager/examples/values-internal-local-path.yaml
helm template mm-bot-manager deploy/helm/mm-bot-manager \
  --namespace mm-bot-manager --kube-version 1.21.1 \
  --values deploy/helm/mm-bot-manager/examples/values-harbor.yaml
```

Install external PostgreSQL mode:

```bash
helm upgrade --install mm-bot-manager deploy/helm/mm-bot-manager \
  --namespace mm-bot-manager \
  --values deploy/helm/mm-bot-manager/examples/values-external.yaml \
  --atomic --wait
```

Install the recommended internal production mode:

```bash
helm upgrade --install mm-bot-manager deploy/helm/mm-bot-manager \
  --namespace mm-bot-manager \
  --values deploy/helm/mm-bot-manager/examples/values-internal-longhorn.yaml \
  --atomic --wait
```

For the internal single-node/test deployment, use
`examples/values-internal-local-path.yaml`. For a closed contour, use the same
internal setup but replace the last values argument with
`deploy/helm/mm-bot-manager/examples/values-harbor.yaml` after its Harbor
repositories, tags or digests, and Mattermost URL have been set for the contour.

## 6. Verify health and diagnose Pods

Use labels rather than assuming a generated Pod name:

```bash
kubectl -n mm-bot-manager get pods \
  -l app.kubernetes.io/instance=mm-bot-manager
kubectl -n mm-bot-manager describe pod \
  -l app.kubernetes.io/instance=mm-bot-manager
```

To inspect the Pod-local health listener, choose the application Pod, then run
the port-forward in one terminal and `curl` in another:

```bash
pod_name="$(kubectl -n mm-bot-manager get pod \
  -l app.kubernetes.io/instance=mm-bot-manager \
  -o jsonpath='{.items[0].metadata.name}')"
kubectl -n mm-bot-manager port-forward "pod/$pod_name" 8080:8080
curl --fail http://127.0.0.1:8080/health/live
curl --fail http://127.0.0.1:8080/health/ready
```

There is no Ingress because this port is a health interface, not a user-facing
application endpoint; ingress-nginx has no traffic to route for this workload.

## 7. Internal PostgreSQL backup and restore

Internal mode creates a daily logical PostgreSQL custom-format dump by default
(`0 2 * * *`, retained for 14 days) on a separate retained backup PVC. Adjust the
schedule, retention, storage class, size, and resources under
`database.internal.backup` before install where needed.

Create an on-demand backup from the CronJob:

```bash
kubectl -n mm-bot-manager create job \
  --from=cronjob/mm-bot-manager-backup \
  "mm-bot-manager-backup-manual-$(date -u +%Y%m%d%H%M%S)"
```

List archives by mounting the existing backup PVC with the same image used by the
CronJob. This never hard-codes a public PostgreSQL repository:

```bash
backup_image="$(kubectl -n mm-bot-manager get cronjob mm-bot-manager-backup \
  -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].image}')"
backup_claim="$(kubectl -n mm-bot-manager get pvc \
  -l app.kubernetes.io/instance=mm-bot-manager,app.kubernetes.io/component=backup \
  -o jsonpath='{.items[0].metadata.name}')"
kubectl -n mm-bot-manager run backup-inspector \
  --restart=Never \
  --image="$backup_image" \
  --overrides="$(
    printf '{"spec":{"automountServiceAccountToken":false,"containers":[{"name":"backup-inspector","image":"%s","command":["sh","-ec","ls -lh /backups"],"volumeMounts":[{"name":"backups","mountPath":"/backups","readOnly":true}]}],"volumes":[{"name":"backups","persistentVolumeClaim":{"claimName":"%s","readOnly":true}}]}}' \
      "$backup_image" "$backup_claim"
  )"
kubectl -n mm-bot-manager logs backup-inspector
kubectl -n mm-bot-manager delete pod backup-inspector
```

To restore, first list archives with the preceding temporary Pod and choose an
existing safe basename. Stop the application so it cannot write during restore,
render only the opt-in restore Job, wait for it, inspect its logs, delete it, and
only then return the Deployment to one replica. The chart rejects restore names
outside the safe `.dump` basename format.

```bash
kubectl -n mm-bot-manager scale deployment/mm-bot-manager --replicas=0
kubectl -n mm-bot-manager rollout status deployment/mm-bot-manager

helm template mm-bot-manager deploy/helm/mm-bot-manager \
  --namespace mm-bot-manager \
  --kube-version 1.21.1 \
  --values deploy/helm/mm-bot-manager/examples/values-internal-longhorn.yaml \
  --show-only templates/restore-job.yaml \
  --set database.internal.restore.enabled=true \
  --set-string database.internal.restore.file=mm-bot-manager-20260728T020000Z.dump \
  >restore-job.yaml
kubectl -n mm-bot-manager create -f restore-job.yaml
kubectl -n mm-bot-manager wait \
  --for=condition=complete job/mm-bot-manager-restore \
  --timeout=30m
kubectl -n mm-bot-manager logs job/mm-bot-manager-restore
kubectl -n mm-bot-manager delete job/mm-bot-manager-restore

kubectl -n mm-bot-manager scale deployment/mm-bot-manager --replicas=1
kubectl -n mm-bot-manager rollout status deployment/mm-bot-manager
```

Replace the sample filename with a filename shown by the archive-inspection
command, retaining its schema-safe basename format. A restore replaces the target
database, so test it in an isolated environment and ensure a current backup exists
before starting.

## 8. Upgrade, rollback, uninstall, and retained storage

Preview upgrades with `helm template` from section 5, then run the same
`helm upgrade --install` command with the selected profile. If the release is
unhealthy after an upgrade, inspect Pods and Helm history, then roll back to a
known revision:

```bash
helm -n mm-bot-manager history mm-bot-manager
helm -n mm-bot-manager rollback mm-bot-manager REVISION --wait
```

Uninstalling the release retains the internal data and backup PVCs. Inspect the
actual resource names first; do not delete data merely because a Helm release was
removed:

```bash
data_claim="$(kubectl -n mm-bot-manager get pvc \
  -l app.kubernetes.io/instance=mm-bot-manager,app.kubernetes.io/component=postgresql \
  -o jsonpath='{.items[0].metadata.name}')"
backup_claim="$(kubectl -n mm-bot-manager get pvc \
  -l app.kubernetes.io/instance=mm-bot-manager,app.kubernetes.io/component=backup \
  -o jsonpath='{.items[0].metadata.name}')"
kubectl -n mm-bot-manager get pvc "$data_claim" "$backup_claim" -o wide
helm -n mm-bot-manager uninstall mm-bot-manager
```

After explicit operator confirmation that retained backups are exported and the
data is no longer needed, delete only the names resolved above:

```bash
printf '%s' 'Type DELETE to remove the resolved data and backup PVCs: '
read -r confirm
test "$confirm" = DELETE
kubectl -n mm-bot-manager delete pvc "$data_claim" "$backup_claim"
```

Two PVCs in the same Longhorn cluster are not complete disaster recovery. Configure
Longhorn backup/export to a separate S3 or NFS target and test restores from it.
With `local-path`, copy validated dump files off the node: both the data volume and
the backup volume are lost when that node is lost.
