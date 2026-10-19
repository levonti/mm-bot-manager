#!/usr/bin/env bash
# Saved deployment selection and fail-fast installation. No Python dependency.
set -euo pipefail
umask 077

die() { printf '%s\n' "$*" >&2; exit 1; }
usage() {
  echo "Usage: $0 [--config FILE] init|check|apply|prepare-secret KIND|secret-path KIND" >&2
}
root="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$root"
local_dir=.local/kubernetes
explicit_config=false
deployment_config=""
if [ "${1:-}" = --config ]; then
  [ "$#" -ge 3 ] || { usage; exit 2; }
  deployment_config="$2"
  explicit_config=true
  shift 2
fi
operation="${1:-}"
case "$operation" in
  init|check|apply) [ "$#" -eq 1 ] || { usage; exit 2; } ;;
  prepare-secret|secret-path)
    [ "$#" -eq 2 ] || { usage; exit 2; }
    case "$2" in app|internal-db|external-db) ;; *) usage; exit 2 ;; esac ;;
  *) usage; exit 2 ;;
esac
guard_dir=""
temporary_dir=""
cleanup() {
  [ -z "$guard_dir" ] || rm -rf "$guard_dir"
  [ -z "$temporary_dir" ] || rm -rf "$temporary_dir"
  return 0
}
trap cleanup EXIT
# shellcheck source=scripts/kubernetes-local.sh
source "$root/scripts/kubernetes-local.sh"
init_local_git
path_exists() { [ -e "$1" ] || [ -L "$1" ]; }
assert_local_path "$local_dir" directory
# Inspect names/index only, never Secret contents. Ignore rules do not untrack a
# previously added file, including a forced addition inside the local directory.
legacy_files=(deployment.env values-site.yaml app-secret.env internal-db-secret.env external-db-secret.env
  images.tar images.tar.sha256 deployment.env.bak deployment.env~ values-site.yaml.bak values-site.yaml~)
for working_copy in "${legacy_files[@]}" mm-bot-manager-*.dump *-secret.env.bak *-secret.env~; do
  if path_exists "$working_copy"; then assert_local_path "$working_copy"; fi
done
if [ "$explicit_config" = false ]; then
  if path_exists "$local_dir/deployment.env" && path_exists deployment.env; then
    die 'Both local and legacy deployment.env exist; select one with --config FILE (neither file was changed)'
  elif path_exists deployment.env; then
    deployment_config=deployment.env
    echo 'Using legacy deployment.env; existing files stay in place' >&2
  else
    deployment_config="$local_dir/deployment.env"
  fi
fi
assert_local_path "$deployment_config"

copy_once() {
  local source="$1" destination="$2"
  assert_local_path "$destination"
  if path_exists "$destination"; then
    printf 'Keeping existing %s (not overwritten)\n' "$destination"
  else
    mkdir -p "$(dirname "$destination")"
    # noclobber also prevents overwriting a file created concurrently.
    (set -o noclobber; cat "$source" >"$destination")
    printf 'Created %s; edit it before continuing\n' "$destination"
  fi
}

if [ "$operation" = prepare-secret ] || [ "$operation" = secret-path ]; then
  preferred="$local_dir/$2-secret.env"
  alternate="$2-secret.env"
  if [ "$(local_path "$deployment_config")" = "$root/deployment.env" ]; then
    preferred="$2-secret.env"
    alternate="$local_dir/$2-secret.env"
  fi
  if [ "$explicit_config" = false ]; then
    if path_exists "$preferred" && path_exists "$alternate"; then
      die 'Both local and legacy Secret files exist; select their layout with --config FILE (no files changed)'
    elif ! path_exists "$preferred" && path_exists "$alternate"; then
      preferred="$alternate"
      printf 'Using existing %s; not moved or copied\n' "$preferred" >&2
    fi
  fi
  assert_local_path "$preferred"
  if [ "$operation" = secret-path ]; then
    [ -f "$preferred" ] || die "Secret file not found: $preferred; run prepare-secret $2 first"
    printf '%s\n' "$preferred"
  else
    copy_once "deploy/helm/mm-bot-manager/examples/$2-secret.env.example" "$preferred"
  fi
  exit 0
fi
if [ "$operation" = init ]; then
  copy_once deploy/helm/mm-bot-manager/examples/deployment.env.example "$deployment_config"
fi
[ -f "$deployment_config" ] || die 'Missing deployment configuration; run scripts/kubernetes-deploy.sh init and edit the printed file first'
# Do not inherit a stale profile or other settings from the calling terminal.
unset chart profile site context
# This is an operator-owned Bash file, never a downloaded/untrusted env file.
# shellcheck disable=SC1090
source "$deployment_config"
: "${chart:?Set chart in the selected deployment.env}" "${site:?Set site in the selected deployment.env}"
[ -f "$chart/Chart.yaml" ] || die "Chart not found: $chart"
assert_local_path "$site"
if [ "$operation" = init ]; then
  copy_once "$chart/examples/values-site.yaml.example" "$site"
  for directory in "$local_dir/images" "$local_dir/backups"; do
    assert_local_path "$directory" directory
    mkdir -p "$directory"
  done
  printf 'Configuration: %s\nSite overlay: %s\n' "$deployment_config" "$site"
  exit 0
fi
: "${profile:?Choose profile in the selected deployment.env}" "${context:?Set context in the selected deployment.env}"
[[ "$profile" != *CHANGE_ME* && "$context" != *CHANGE_ME* ]] || die 'Choose profile and context in the selected deployment.env first'
[ -f "$profile" ] || die "Profile not found: $profile"
[ -f "$site" ] || die "Site overlay not found: $site (run init to prepare a missing copy)"
namespace=mm-bot-manager
release=mm-bot-manager
actual_context="$(kubectl config current-context)"
[ "$actual_context" = "$context" ] || die "Kube-context mismatch: expected $context, current $actual_context; select the saved context first"
printf 'Context: %s\nNamespace: %s\nRelease: %s\nProfile: %s\nSite overlay: %s\n' \
  "$context" "$namespace" "$release" "$profile" "$site"
helm_version="$(helm version --template '{{.Version}}')"
case "$helm_version" in
  v3.*) rollback_flag=--atomic ;;
  v4.*) rollback_flag=--rollback-on-failure ;;
  *) die 'Helm 3 or 4 is required' ;;
esac
./scripts/verify-kubernetes-site.sh "$profile" "$site" "$release" "$namespace" "$chart"

temporary_dir="$(mktemp -d)"
# Render a small read-only summary with the same Helm values merge as the chart.
# This avoids parsing YAML in Bash or requiring Python/yq on the workstation.
mkdir "$temporary_dir/templates"
printf 'apiVersion: v2\nname: deployment-summary\nversion: 0.0.0\n' >"$temporary_dir/Chart.yaml"
cp "$chart/values.yaml" "$temporary_dir/values.yaml"
cat >"$temporary_dir/templates/summary.yaml" <<'SUMMARY'
apiVersion: v1
kind: ConfigMap
metadata:
  name: deployment-summary
data:
  database-mode: {{ .Values.database.mode | quote }}
{{ if eq .Values.database.mode "internal" }}
  data-storage-class: {{ .Values.database.internal.storage.storageClass | quote }}
  backups-enabled: {{ .Values.database.internal.backup.enabled | toString | quote }}
{{ if .Values.database.internal.backup.enabled }}
  backup-storage-class: {{ .Values.database.internal.backup.storageClass | quote }}
{{ end }}
{{ end }}
SUMMARY
helm template "$release" "$temporary_dir" --values "$profile" --values "$site" >"$temporary_dir/summary.yaml"
awk '/^data:$/ { show=1; next } show && NF { print }' "$temporary_dir/summary.yaml"

# pipefail prevents an unsuccessful render from being masked by kubectl.
helm template "$release" "$chart" --namespace "$namespace" \
  --values "$profile" --values "$site" | \
  kubectl --context "$context" -n "$namespace" apply --dry-run=server -f -
if [ "$operation" = check ]; then
  echo 'Checks passed; run scripts/kubernetes-deploy.sh apply to install/upgrade this saved configuration'
  exit 0
fi
helm upgrade --install "$release" "$chart" --kube-context "$context" --namespace "$namespace" \
  --values "$profile" --values "$site" \
  "$rollback_flag" --wait --wait-for-jobs --timeout=10m
