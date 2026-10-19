#!/usr/bin/env bash
# Saved deployment selection and fail-fast installation. No Python dependency.
set -euo pipefail
umask 077

die() { printf '%s\n' "$*" >&2; exit 1; }
usage() {
  echo "Usage: $0 init|check|apply|prepare-secret app|internal-db|external-db" >&2
}
root="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$root"
operation="${1:-}"
case "$operation" in
  init|check|apply) [ "$#" -eq 1 ] || { usage; exit 2; } ;;
  prepare-secret) [ "$#" -eq 2 ] || { usage; exit 2; } ;;
  *) usage; exit 2 ;;
esac

copy_once() {
  local source="$1" destination="$2"
  if [ -e "$destination" ] || [ -L "$destination" ]; then
    [ -f "$destination" ] && [ ! -L "$destination" ] || die "Expected a regular file: $destination"
    printf 'Keeping existing %s (not overwritten)\n' "$destination"
  else
    # noclobber also prevents overwriting a file created concurrently.
    (set -o noclobber; cat "$source" >"$destination")
    printf 'Created %s; edit it before continuing\n' "$destination"
  fi
}

if [ "$operation" = prepare-secret ]; then
  case "$2" in app|internal-db|external-db) ;; *) usage; exit 2 ;; esac
  copy_once "deploy/helm/mm-bot-manager/examples/$2-secret.env.example" "$2-secret.env"
  exit 0
fi
if [ "$operation" = init ]; then
  copy_once deploy/helm/mm-bot-manager/examples/deployment.env.example deployment.env
fi
[ -f deployment.env ] || die 'Missing deployment.env; run scripts/kubernetes-deploy.sh init and edit it first'
# Do not inherit a stale profile or other settings from the calling terminal.
unset chart profile site context
# This is an operator-owned Bash file, never a downloaded/untrusted env file.
# shellcheck disable=SC1091
source ./deployment.env
: "${chart:?Set chart in deployment.env}" "${site:?Set site in deployment.env}"
[ -f "$chart/Chart.yaml" ] || die "Chart not found: $chart"
if [ "$operation" = init ]; then
  copy_once "$chart/examples/values-site.yaml.example" "$site"
  exit 0
fi
: "${profile:?Choose profile in deployment.env}" "${context:?Set context in deployment.env}"
[[ "$profile" != *CHANGE_ME* && "$context" != *CHANGE_ME* ]] || die 'Choose profile and context in deployment.env first'
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
trap 'rm -rf "$temporary_dir"' EXIT
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
