# Kubernetes deployment runbook

Deploy `mm-bot-manager` with Helm 3 or 4 on Kubernetes `>= 1.21.1`, using either an
external PostgreSQL service or one internal PostgreSQL StatefulSet. The chart has
no Helm dependencies. All commands below run in **Bash from the repository root**.
The helper scripts require Bash, Git, Helm and kubectl; no Python installation is
needed on the operator's workstation.

The internal database has **one replica and no HA failover**. Data and logical
backups use separate retained PVCs. The application connects outbound to Mattermost
REST/WebSocket and has no inbound user API, application Service or Ingress.
Port 8080 is a health interface bound to the Pod network, not a public endpoint.

Follow this route for a **first installation**:

1. [Choose and save the configuration](#1-prerequisites-and-site-configuration)
   in `.local/kubernetes/deployment.env`, then fill the adjacent `values-site.yaml`.
2. [Publish images and prepare registry access](#2-prepare-images-and-registry-authentication).
3. [Create the application and selected database Secrets](#3-applicationdatabase-secrets-and-mattermost-ca).
4. [Check and install the saved configuration](#4-validate-and-install-the-exact-configuration).
5. [Complete acceptance](#5-acceptance-and-diagnosis), including a real backup verification.

For an **existing installation**, keep the saved files in place, take a verified backup and
follow [section 8](#8-upgrade-rollback-retained-storage-and-disaster-recovery).
Do not repeat initial setup or recreate Secrets for an ordinary image upgrade.
The `init` and `prepare-secret` helpers preserve existing files, including their
permissions. They create missing copies with mode 0600. Edit existing files in
place; do not replace them with the examples when resuming work.

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
kubectl get nodes -o custom-columns=NAME:.metadata.name,ARCH:.status.nodeInfo.architecture,RUNTIME:.status.nodeInfo.containerRuntimeVersion
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

**First installation only — prepare local files:**

```bash
./scripts/kubernetes-deploy.sh init
```

Edit `.local/kubernetes/deployment.env` **once**: select `profile` from the table above and set
`context` to the exact output of `kubectl config current-context`. There is no
usable default profile: an unedited selection stops check/apply. For example,
the single-node/test choice in that file is (edit the file, not the terminal):

```dotenv
profile="$chart/examples/values-internal-local-path.yaml"
```

All new persistent operator files go into **`.local/kubernetes/`**:

```text
.local/kubernetes/
  deployment.env
  values-site.yaml
  buildkitd.toml                    # optional dedicated BuildKit configuration
  app-secret.env
  internal-db-secret.env OR external-db-secret.env
  images/images.tar
  images/images.tar.sha256
  backups/                         # dumps exported from the cluster
```

`init` creates the local directory plus `images/` and `backups/` with private
permissions, and creates missing configuration files with mode 0600. Secret
files are prepared separately in section 3. The entire directory, including
editor backups, is excluded from Git, Docker build contexts and Helm packaging.
Templates under `deploy/helm/mm-bot-manager/examples/` remain tracked and packaged.
Preserve the local configuration in approved storage: it is not part of a Git
clone or image. Transfer required image archives explicitly to a closed contour.

Keep `chart` and `site` as generated unless you deliberately use other paths.
The saved configuration is an operator-owned Bash file, never a downloaded or
untrusted env file. Paths are resolved from the repository root, even when the
configuration itself is elsewhere. The helpers check paths **before reading or
creating local files**: a path inside a Git checkout must be ignored and absent
from its index. An ignored file that was previously added (including `git add -f`)
is rejected. The same checks apply to another Git checkout containing an external
path. Symlink paths are rejected; dot segments cannot bypass the checks. A path
outside all Git checkouts is allowed. For a custom path inside the build context,
also add its exclusion to `.dockerignore`, and to `.helmignore` if it is under
the chart. Keeping files in the standard local directory avoids those extra rules.

Git is required for these checks. Transferred source archives without `.git` use
a disposable Git directory to evaluate the supplied ignore rules; the helper does
not initialize or modify Git metadata in the transferred sources.

**Compatibility with existing installations — no automatic migration:**

- If only the old root `deployment.env` exists, it remains active. Its `site`
  setting and existing files are used in place, without copying, moving or chmod.
- Existing root Secret env files are reused. `prepare-secret` prints the selected
  path; `secret-path` returns that same path for Secret creation/rotation.
- If both configuration files exist, stop and explicitly select one for **every**
  command. Two copies of the same Secret also require an explicit layout selection.

```bash
./scripts/kubernetes-deploy.sh --config deployment.env check
# Or select the new layout explicitly:
./scripts/kubernetes-deploy.sh --config .local/kubernetes/deployment.env check
```

Use the same `--config FILE` on `init`, `prepare-secret`, `secret-path` and `apply`
when selecting explicitly. Nothing is deleted or moved to resolve duplicates.
Legacy root working files, known image archive/checksum names, dump names and
`.bak`/`~` copies remain excluded too. Keep new exports in the local directory.
If the helper reports a tracked file, preserve its local copy and review removal
from Git's index separately; the helper never untracks files automatically.

**Required for every profile — fill the site overlay:**

Edit the selected site overlay (new default `.local/kubernetes/values-site.yaml`)
and replace **every `CHANGE_ME`**: application repository and
immutable tag/digest, PostgreSQL repository, real `mattermost.url`, approver and
catalog viewer usernames. Set `defaultLanguage` to `ru` or `en`; users can later
select `!language ru|en|auto` and `!catalog --format cards|table`. Keep
`verifySSL: true`. Set `imagePullSecrets` to your registry Secret, or `[]` only for
images that all nodes can pull without authentication. For external mode the
internal image settings are unused. No token, password or `DB_URL` belongs in
values. The default site overlay and legacy root `values-site.yaml` are ignored by Git
and excluded from Docker builds; preserve the selected file in approved
configuration storage for future upgrades.

## 2. Prepare images and registry authentication

### 2.1 Select HTTP or HTTPS and configure each client

Complete transport configuration **before login, build or push** on the staging
and destination workstations, and before pulling on Kubernetes nodes. Record the
actual registry authority (`host[:port]`) and externally reachable scheme/port:

| Harbor endpoint | Required client configuration |
| --- | --- |
| HTTPS, publicly trusted certificate | Normal HTTPS; no insecure-registry exception |
| HTTPS, private CA | Install the Harbor CA chain in each relevant client/runtime; keep certificate verification enabled |
| HTTP | Explicit HTTP configuration for this exact authority in Docker, the selected BuildKit builder and every relevant node runtime |

HTTP sends credentials, tokens and image traffic without TLS protection. Use it
only for an approved isolated test/development contour; prefer HTTPS in production.
See [Harbor's HTTPS guidance](https://goharbor.io/docs/2.14.0/install-config/configure-https/).
The examples use `harbor.internal.example:8080` for HTTP and
`harbor.internal.example` for HTTPS. Replace the authority **consistently** in
runtime configuration, login, image names and pull credentials, including any
explicit port. A browser opening the Harbor UI does not prove `/v2/` or the token
service is reachable from a builder or node. Behind a proxy, the Harbor operator
must also verify its advertised external URL and authentication endpoint.

Image names and `registry` variables contain **no `http://` or `https://`**:
`harbor.internal.example:8080/platform/mm-bot-manager:TAG`. Schemes belong in
transport endpoint settings. `imagePullSecrets` supply authentication; they do
not select HTTP or install CA trust. `mattermost.verifySSL`, `mattermost.caBundle`
and the application's `SSL_CERT_FILE` apply to Mattermost, not registry pulls.
The chart uses normal image references with the selected authority; configure
registry transport in the clients/runtimes above.

The following are **configuration fragments**, not replacement files. Preserve
existing daemon settings, mirrors and registry entries. Inspect the selected
Docker context/builder and node runtime version first. Back up the affected
configuration and plan any required service refresh with the cluster operator;
do not restart all nodes together. A failed check requires restoring the previous
configuration and following that runtime's maintenance procedure.

**Docker Engine / Docker-backed nodes:** for HTTP, merge this entry into the
selected daemon's configuration (Linux default `/etc/docker/daemon.json`):

```json
{
  "insecure-registries": ["harbor.internal.example:8080"]
}
```

Apply it using that platform's Docker maintenance procedure. With Docker Desktop
or a remote Docker context, configure the actual backend daemon, not an unrelated
host file. For HTTPS with a private CA, instead install the CA chain at
`/etc/docker/certs.d/harbor.internal.example/ca.crt` on a Linux Docker daemon;
include `:port` in the directory name when the authority includes it. Keep HTTPS
registries out of the HTTP exception list. Verify the active daemon/context with
`docker context show` and `docker info`. See
[Docker insecure registries](https://docs.docker.com/reference/cli/dockerd/#insecure-registries)
and [registry certificates](https://docs.docker.com/engine/security/certificates/).

**Buildx / standalone BuildKit:** a `docker-container` builder has its own
registry transport configuration; configuring Docker Engine alone is insufficient.
After `init`, save the selected fragment as `.local/kubernetes/buildkitd.toml`.
For HTTP:

```toml
[registry."harbor.internal.example:8080"]
  http = true
```

For HTTPS with a private CA, use this instead (the CA file must exist on the
workstation creating the builder):

```toml
[registry."harbor.internal.example"]
  ca = ["/protected/config/harbor-ca.crt"]
```

Do not combine `http = true` with `insecure = true`; the latter bypasses HTTPS
certificate verification and is not the HTTP setting. For publicly trusted HTTPS,
no registry override is needed (an empty TOML file is sufficient when creating a
builder for that route). Inspect `docker buildx ls` first; create the following
dedicated builder only if its name is unused:

```bash
docker buildx create --name mm-bot-harbor --driver docker-container \
  --buildkitd-config .local/kubernetes/buildkitd.toml
docker buildx inspect mm-bot-harbor --bootstrap
```

Select it explicitly with `docker buildx build --builder mm-bot-harbor ...` in
the authenticated publishing session below. Bootstrap needs the BuildKit image;
prepare an approved mirrored builder image if staging is disconnected. For an
existing builder, review/recreate it through its own lifecycle so it actually
uses the changed configuration. A standalone `buildkitd` uses its own config
path. See [BuildKit registry settings](https://docs.docker.com/build/buildkit/toml-configuration/)
and [Buildx builder configuration](https://docs.docker.com/build/buildkit/configure/).

**Standalone containerd 1.5+ / 2.x nodes:** use the CRI plugin's registry
configuration, not just Docker or `ctr` command-line flags. In the node's existing
`/etc/containerd/config.toml`, enable the directory using the matching format.
For containerd 1.x with config version 2:

```toml
[plugins."io.containerd.grpc.v1.cri".registry]
  config_path = "/etc/containerd/certs.d"
```

For containerd 2.x with config version 3:

```toml
[plugins."io.containerd.cri.v1.images".registry]
  config_path = "/etc/containerd/certs.d"
```

For HTTP, place the following in
`/etc/containerd/certs.d/harbor.internal.example:8080/hosts.toml` on **each node
that may run the application, PostgreSQL or maintenance Jobs**:

```toml
server = "http://harbor.internal.example:8080"
[host."http://harbor.internal.example:8080"]
  capabilities = ["pull", "resolve"]
```

For HTTPS with a private CA, use
`/etc/containerd/certs.d/harbor.internal.example/hosts.toml` and install the CA
chain at the referenced node-local path:

```toml
server = "https://harbor.internal.example"
[host."https://harbor.internal.example"]
  capabilities = ["pull", "resolve"]
  ca = "/etc/containerd/certs.d/harbor.internal.example/ca.crt"
```

`skip_verify = true` only disables HTTPS certificate checks; it does not enable
HTTP. Once `config_path` is active, changes under that hosts directory do not
require a containerd restart; changing `config.toml` requires the platform's
runtime refresh procedure. Do not mix `config_path` with legacy registry mirror
configuration. Kubernetes `>= 1.21.1` does not imply containerd `>= 1.5`:
older 1.3/1.4 installations using config version 2 need their legacy HTTP entry
in `config.toml`, followed by a planned runtime refresh:

```toml
[plugins."io.containerd.grpc.v1.cri".registry.mirrors."harbor.internal.example:8080"]
  endpoint = ["http://harbor.internal.example:8080"]
```

For legacy HTTPS/private-CA settings, use the version-matched
[containerd 1.4 registry reference](https://github.com/containerd/cri/blob/release/1.4/docs/registry.md).
See the modern [containerd hosts reference](https://github.com/containerd/containerd/blob/main/docs/hosts.md)
for `config_path` and supported formats. Keep credentials in the Kubernetes pull
Secret rather than adding passwords to these node configuration fragments.

**K3s nodes:** use `/etc/rancher/k3s/registries.yaml` instead of editing generated
containerd files. Merge the selected fragment on every relevant agent and on
servers that run workloads. For HTTP:

```yaml
mirrors:
  "harbor.internal.example:8080":
    endpoint:
      - "http://harbor.internal.example:8080"
```

For HTTPS with a private CA, use:

```yaml
mirrors:
  "harbor.internal.example":
    endpoint:
      - "https://harbor.internal.example"
configs:
  "harbor.internal.example":
    tls:
      ca_file: /etc/rancher/k3s/harbor-ca.crt
```

Install that CA chain on each node. `insecure_skip_verify` is not an HTTP switch.
K3s requires a planned restart of the affected `k3s`/`k3s-agent` service for these
changes to take effect. Follow the installed K3s version's
[private registry instructions](https://docs.k3s.io/installation/private-registry);
newer fallback-control flags are not available in all older releases. Other
managed distributions need their own supported configuration mechanism.

### 2.2 Namespace and pull credentials

Create the namespace idempotently:

```bash
kubectl create namespace mm-bot-manager --dry-run=client -o yaml | kubectl apply -f -
```

**Private registry only:** create or rotate the pull Secret using an isolated
Docker configuration and a Harbor account with pull access to both repositories.
The password is read at a hidden prompt and passed only on stdin; it never becomes
a process argument. The temporary credential file is mode 0600 inside a private
directory and is removed on exit. Do not enable shell tracing (`set -x`). Complete
section 2.1 first; changing `--config` does not configure daemon transport.

```bash
(
  set -euo pipefail
  umask 077
  registry="harbor.internal.example" # exact authority; HTTP example: harbor.internal.example:8080
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
For unauthenticated public repositories, keep `imagePullSecrets: []` and omit
the pull Secret. Transport configuration is still required for HTTP/private CA.

### 2.3 Publish or transfer the exact images

Build on a connected staging machine for the **cluster architecture**, not
implicitly for the build laptop. For AMD64 nodes, use a publisher account with
push access to the destination projects in the isolated session below. For a mixed
cluster, use the multi-platform replacement immediately after this example.

```bash
(
  set -euo pipefail
  umask 077
  registry="harbor.internal.example" # exact authority; HTTP example: harbor.internal.example:8080
  auth_dir="$(mktemp -d)"
  trap 'rm -rf "$auth_dir"' EXIT
  export BUILDX_CONFIG="${BUILDX_CONFIG:-${DOCKER_CONFIG:-$HOME/.docker}/buildx}"
  export DOCKER_CONFIG="$auth_dir"
  read -r -p 'Harbor publisher username: ' registry_user
  read -r -s -p 'Harbor publisher password: ' registry_password
  printf '\n'
  printf '%s' "$registry_password" | docker login "$registry" \
    --username "$registry_user" --password-stdin
  unset registry_password
  image_tag="$(git rev-parse --short=12 HEAD)"
  docker build --platform linux/amd64 -t "$registry/platform/mm-bot-manager:$image_tag" .
  docker push "$registry/platform/mm-bot-manager:$image_tag"
  docker pull --platform linux/amd64 postgres:15.18-alpine3.24
  docker tag postgres:15.18-alpine3.24 "$registry/library/postgres:15.18-alpine3.24"
  docker push "$registry/library/postgres:15.18-alpine3.24"
  docker image inspect "$registry/platform/mm-bot-manager:$image_tag" --format '{{json .RepoDigests}}'
  docker image inspect "$registry/library/postgres:15.18-alpine3.24" --format '{{json .RepoDigests}}'
)
```

The pull Secret session above deliberately does not retain workstation credentials.
This publisher session authenticates separately and removes its credentials on
exit too; it does not put push credentials in Kubernetes. With an isolated
`DOCKER_CONFIG`, Buildx would otherwise lose the configured builder's location;
the `BUILDX_CONFIG` assignment preserves that location while isolating Docker auth.
The isolated login/publishing examples assume the default local Docker context.
For a remote/custom context, have the operator supply an approved isolated Docker
configuration/endpoint with the required context metadata and TLS settings;
do not accidentally publish through a different daemon or copy cached credentials.

For a mixed cluster, replace the application's `docker build` and `docker push`
lines **inside that session** with the following. The configured builder must
support both platforms; verify its platform list before building:

```bash
docker buildx build --builder mm-bot-harbor --platform linux/amd64,linux/arm64 \
  --tag "$registry/platform/mm-bot-manager:$image_tag" \
  --metadata-file .local/kubernetes/images/application-build.json --push .
```

Also omit the application's `docker image inspect` line for this route: the
`docker-container` builder pushes directly and does not load the result into the
local Docker image store. After a successful build/push, take `containerimage.digest`
from the [Buildx metadata file](https://docs.docker.com/reference/cli/docker/buildx/build/#metadata-file)
and verify that published reference through Kubernetes in section 2.4.

Put these actual repositories and tags, preferably their published `sha256`
digests, into the site overlay. A digest takes precedence over a tag. The above
single-platform PostgreSQL mirror is only suitable for nodes of that architecture.
For a mixed cluster mirror its manifest list with the approved registry tool.

**Closed contour only:** transfer image archives and checksums through the
approved channel. From the staging source root, after `init`:

```bash
(
  set -euo pipefail
  umask 077
  docker save --output .local/kubernetes/images/images.tar IMAGE1 IMAGE2
  cd .local/kubernetes/images
  sha256sum images.tar >images.tar.sha256
)
```

Transfer both files to `.local/kubernetes/images/` on the destination. After
preparing that directory with `init`, verify and load:

```bash
(
  set -euo pipefail
  cd .local/kubernetes/images
  sha256sum --check images.tar.sha256
  docker load --input images.tar
)
```

Then configure transport and authenticate on the destination publishing workstation
as above, tag and **push into the destination registry**, and verify its published digests.
A loaded image on the operator's laptop is not present on Kubernetes nodes;
`RepoDigests` may be empty until the image is published. Transfer the source/chart,
Helm/kubectl binaries and any local validation schemas as well.

### 2.4 Verify pulls through Kubernetes on each eligible node

After publishing and configuring pull access, test **before installing/upgrading**.
A successful workstation `docker login`/`pull`, `ctr --plain-http` or Helm dry run
does not prove the kubelet's CRI transport and Secret work. Run the following
**once per image on each Ready node** where application, PostgreSQL or maintenance
Pods may run, including eligible control-plane nodes. Choose a real node name from
section 1 and an exact published image reference from the site overlay (prefer
immutable digests). Check the application image, then repeat with
`harbor.internal.example[:port]/library/postgres@sha256:ACTUAL_DIGEST` for internal
database mode. External-database mode only needs the application image.

The temporary Pod runs `/bin/sh -c 'exit 0'` from the selected image. It
starts neither the application nor PostgreSQL, mounts no PVC or application/DB
Secret, and disables the service-account token. `nodeName` targets the chosen
node directly; this checks runtime access, not scheduler placement. Adapt the
resource requests or scheduling constraints to site admission policies if needed.

```bash
(
  set -euo pipefail
  node="CHANGE_ME_NODE_NAME"
  image="CHANGE_ME_REGISTRY/platform/mm-bot-manager@sha256:CHANGE_ME_DIGEST"
  case "$node $image" in
    *CHANGE_ME*) printf '%s\n' 'Replace node and image placeholders first.' >&2; exit 2 ;;
  esac
  pod="$(kubectl -n mm-bot-manager create -f - -o jsonpath='{.metadata.name}' <<EOF
apiVersion: v1
kind: Pod
metadata:
  generateName: mm-bot-registry-check-
spec:
  nodeName: "$node"
  restartPolicy: Never
  activeDeadlineSeconds: 300
  automountServiceAccountToken: false
  imagePullSecrets:
    - name: harbor-pull
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: image-check
      image: "$image"
      imagePullPolicy: Always
      command: ["/bin/sh", "-c", "exit 0"]
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: ["ALL"]
      resources:
        requests: {cpu: 10m, memory: 16Mi}
        limits: {cpu: 100m, memory: 64Mi}
EOF
  )"
  printf 'Registry check Pod: %s on %s for %s\n' "$pod" "$node" "$image"
  phase=""
  for ((attempt=0; attempt<66; attempt++)); do
    phase="$(kubectl -n mm-bot-manager get "pod/$pod" -o jsonpath='{.status.phase}')"
    case "$phase" in Succeeded|Failed) break ;; esac
    sleep 5
  done
  if [[ "$phase" != Succeeded ]]; then
    kubectl -n mm-bot-manager describe "pod/$pod"
    printf 'Check failed; retain Pod %s for diagnosis, then delete it explicitly.\n' "$pod" >&2
    exit 1
  fi
  kubectl -n mm-bot-manager get "pod/$pod" \
    -o jsonpath='{range .status.containerStatuses[*]}{.name}{"\t"}{.imageID}{"\t"}{.state.terminated.exitCode}{"\n"}{end}'
  kubectl -n mm-bot-manager describe "pod/$pod"
  kubectl -n mm-bot-manager delete "pod/$pod" --wait=true
)
```

For public images remove `imagePullSecrets`; for another private Secret use its
actual name. After a failure, record the generated name, inspect its events, then
delete only that Pod with `kubectl -n mm-bot-manager delete pod/ACTUAL_CHECK_POD`.
Acceptance is `Succeeded`, exit code 0 for each selected image, expected image IDs
and successful image-pull events on **each tested node**. Record node, runtime,
image references/IDs and outcome without credential data. An index digest can
resolve to a different platform-specific digest in the reported image ID.

`Always` requires a registry resolution/pull attempt but can reuse cached layers.
It proves the current configured pull path; it does not prove all layers crossed
the network, or that the named Secret was the only credential source. To prove a
full download or credential rotation without cache/alternate credentials, use an
isolated test node with no relevant cached image/layers or node-level registry
credentials and a newly published immutable image. Test both repositories. Do not
clear a running node's image cache or restart application/DB Pods just for this
check. Untested nodes or repositories remain **NOT_TESTED**.

## 3. Application/database Secrets and Mattermost CA

Working env copies must contain real values only locally. Prepare missing copies
with the helper and edit them using an editor; never paste credentials into shell
commands, values, logs or tickets. Ensure existing env files have mode 0600 before
entering secrets. The Secret creation pipelines below do not print values.

**Required for every profile — application Secret:**

```bash
./scripts/kubernetes-deploy.sh prepare-secret app
```

Edit `MM_TOKEN` and `MM_BOT_TOKEN` in the path printed by the helper
(new default `.local/kubernetes/app-secret.env`). **After saving real values**,
apply the Secret in a separate step:

```bash
(
  set -euo pipefail
  secret_file="$(./scripts/kubernetes-deploy.sh secret-path app)"
  kubectl -n mm-bot-manager create secret generic mm-bot-manager-app \
    --from-env-file="$secret_file" --dry-run=client -o yaml | kubectl apply -f -
)
```

**Internal database only — skip for external mode:** use exactly `POSTGRES_USER`,
`POSTGRES_PASSWORD`, `POSTGRES_DB`.
Choose a dedicated database name, not `postgres`, `template0` or `template1`.
The chart passes these values as `DB_USER`, `DB_PASSWORD`, `DB_NAME` to the
application, with the internal Service as `DB_HOST`; it does not construct a URL.

```bash
./scripts/kubernetes-deploy.sh prepare-secret internal-db
```

Edit the password and, if necessary, the dedicated username/database name in
the printed path (new default `.local/kubernetes/internal-db-secret.env`).
**After saving real values**, apply:

```bash
(
  set -euo pipefail
  secret_file="$(./scripts/kubernetes-deploy.sh secret-path internal-db)"
  kubectl -n mm-bot-manager create secret generic mm-bot-manager-postgresql \
    --from-env-file="$secret_file" --dry-run=client -o yaml | kubectl apply -f -
)
```

**External database only — skip for internal mode:** use the operator-supplied
TLS-appropriate `DB_URL`.

```bash
./scripts/kubernetes-deploy.sh prepare-secret external-db
```

Edit `DB_URL` in the printed path (new default
`.local/kubernetes/external-db-secret.env`). **After saving the real value**, apply:

```bash
(
  set -euo pipefail
  secret_file="$(./scripts/kubernetes-deploy.sh secret-path external-db)"
  kubectl -n mm-bot-manager create secret generic mm-bot-manager-external-db \
    --from-env-file="$secret_file" --dry-run=client -o yaml | kubectl apply -f -
)
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

**Private Mattermost CA only — skip for a publicly trusted endpoint:** create a ConfigMap
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

**Every installation and upgrade:** use the selected saved configuration
(new default `.local/kubernetes/deployment.env`, legacy root file supported). Do not set
`profile` again here. The commands work in a new terminal because the helper
reloads the saved file on every invocation, discarding any inherited shell values.

First run the read-only checks:

```bash
./scripts/kubernetes-deploy.sh check
```

Review the printed **context, namespace, release, profile, site overlay, database
mode and effective data/backup StorageClasses**. The storage summary includes
site overrides; external mode has no internal StorageClasses. If a choice is
wrong, edit the saved files in section 1 and repeat `check`.

**Only after the summary and checks are correct — install/upgrade:**

```bash
./scripts/kubernetes-deploy.sh apply
```

`apply` repeats all checks before Helm changes anything. Both commands require
that the active kube-context matches the saved context; the server-side dry run
and installation also explicitly target that context. The namespace and release
are `mm-bot-manager`, as in the remaining commands in this runbook.

The helper runs with `set -euo pipefail`: missing/unconfigured files, unsupported
Helm, failed site validation, failed rendering or failed server-side dry run
stop the operation **before installation**. It always uses profile first, then
site overlay, with Helm 3 `--atomic` or Helm 4 `--rollback-on-failure`, plus
`--wait --wait-for-jobs --timeout=10m`. The site validator
(`scripts/verify-kubernetes-site.sh`) rejects built-in image/URL placeholders;
rendering alone does not test image availability, real credentials or
network/storage health.

Before deployment to a disconnected cluster, validate manifests with local
Kubernetes schemas using
`KUBECONFORM_SCHEMA_LOCATION=/path/to/1.21.1-schemas ./scripts/verify-kubernetes.sh`.
The server-side dry run also checks the destination cluster's admission policies
and creates no persistent objects. The explicit timeout allows initial storage
provisioning more time than Helm's default five minutes. See the
[PVC-check implementation notes](#appendix-pvc-check-and-upgrade-implementation-notes)
for the reasons behind the wait flags and Job lifecycle.

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
   Save any dumps exported to this workstation under `.local/kubernetes/backups/`.
   Export backups off-cluster and rehearse the complete restore procedure in a test
   release before relying on it for production recovery.

Application restart (also required after token/CA rotation):

```bash
kubectl -n mm-bot-manager rollout restart deployment/mm-bot-manager
kubectl -n mm-bot-manager rollout status deployment/mm-bot-manager --timeout=10m
```

For `Pending`/`CrashLoopBackOff`, inspect `kubectl describe pod`, namespace events
and application/PostgreSQL logs. Check PVC provisioning/capacity, image architecture,
registry transport/CA/authentication, Secret **key names**, database password consistency,
Mattermost account/PAT permissions, DNS, HTTPS and WSS. Do not print Secret data or
paste connection strings into reports. Custom release/fullname overrides require
substituting the actual Deployment name in the restart commands.

For `ErrImagePull`/`ImagePullBackOff`, find the affected node with
`kubectl -n mm-bot-manager get pods -o wide`, inspect that Pod's events and use this table:

| Symptom | Check / correction |
| --- | --- |
| `server gave HTTP response to HTTPS client` | The client is trying HTTPS against HTTP. Match the exact registry authority/port and configure HTTP on the daemon, selected builder or affected node as in section 2.1. |
| `x509: certificate signed by unknown authority` | HTTPS is in use. Install the CA chain in the failing client/runtime; a Pod's Mattermost CA bundle does not configure image pulls. |
| Certificate hostname mismatch / expired certificate | Fix the endpoint name/certificate or clock; keep HTTPS verification enabled. |
| `401` / `403` / `pull access denied` | Check the Harbor account, project/repository pull rights, Secret name/namespace and registry authority (including port). A working application repository does not prove PostgreSQL repository access. |
| `FailedToRetrieveImagePullSecret` | Verify that the referenced Secret exists in this Pod's namespace and is a valid `kubernetes.io/dockerconfigjson` Secret; do not print its contents. |
| `manifest unknown` / `not found` | Confirm the image was pushed into this destination registry with the requested repository/tag/digest. |
| `no matching manifest` / `exec format error` | Verify the node architecture and the published platform manifest. |
| DNS failure / timeout / refused connection | Check the node's DNS, route, registry listener/port and daemon proxy/`NO_PROXY` configuration. A workstation or Pod-network check does not prove node-runtime access. |
| Docker works, Kubernetes fails | Verify the kubelet runtime from section 1, its active configuration and the Pod's pull Secret; Docker/`ctr` transport flags do not configure the CRI plugin. |

For a transport-only probe from the relevant workstation/node, use the selected
endpoint without credentials (replace the URL):

```bash
curl --silent --show-error --connect-timeout 5 --max-time 15 \
  --dump-header - --output /dev/null http://harbor.internal.example:8080/v2/
# HTTPS with a private CA instead:
curl --silent --show-error --connect-timeout 5 --max-time 15 \
  --cacert /protected/config/harbor-ca.crt \
  --dump-header - --output /dev/null https://harbor.internal.example/v2/
```

HTTP `200` or an expected `401` with `WWW-Authenticate` establishes registry API
reachability, **not authenticated image-pull success**. Check that any advertised
token-service URL has the correct scheme/host/port and is reachable/trusted too;
HTML or an unexpected redirect can indicate a proxy/Harbor external-URL problem.
Do not use `curl -k` or disable TLS verification to repair an HTTPS deployment.
For K3s, also inspect the affected node's
`/var/lib/rancher/k3s/agent/containerd/containerd.log`: an endpoint fallback can
hide the first failure in the Pod event. After correction, repeat section 2.4;
do not use application/DB restarts as a registry test.

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

For an ordinary upgrade, keep the selected `deployment.env` in place and edit the immutable application
image tag/digest in the existing site overlay. Do not rerun initial file copying
or Secret creation. Check the application/PostgreSQL image changes and take a
verified backup first, then use the same commands as the initial installation:

```bash
./scripts/kubernetes-deploy.sh check
```

Review the saved selection and effective storage summary, then:

```bash
./scripts/kubernetes-deploy.sh apply
```

Do not change the database mode or StorageClasses as part of an ordinary image
upgrade. A deliberate storage/profile migration needs its own reviewed procedure.
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
states. Deployment workflow tests run the documented commands with real Helm
rendering and a fake cluster. They check all four saved profiles, reuse in a new
shell, site overrides, non-overwriting preparation, context mismatch and stopping
before installation after validation/render/dry-run failures. They also exercise
legacy files, duplicate selections, ignored/untracked paths, source archives and
Git staging/Helm packaging exclusions for generated artifacts.
PVC-check tests execute its rendered shell script against disposable local
files, checking success, storage failures, temporary-file cleanup and archive
preservation; render tests cover revisions and Harbor settings. These do not
exercise Kubernetes scheduling or dynamic provisioning. A separate test executes
the rendered PVC-check and PostgreSQL scripts in a disposable Docker server; it
restores real data, rejects a truncated data stream with a valid
archive catalog, and checks cleanup without touching an operator database.

```bash
uv sync --frozen --extra postgres
uv run --no-sync pytest -q tests/test_helm_chart.py tests/test_backup_pvc_check.py tests/test_kubernetes_runbook.py tests/test_kubernetes_deploy.py
MM_TEST_BACKUP_DOCKER=1 uv run --no-sync pytest -q tests/test_kubernetes_backup_roundtrip.py
```

The Docker drill is opt-in and skipped by the standard CI job, which has only a
PostgreSQL service. Report it separately from chart/schema tests and any live
cluster acceptance; none of these substitutes for storage/node-loss recovery.

## Appendix: PVC-check and upgrade implementation notes

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
