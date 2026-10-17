#!/usr/bin/env bash
# Operator-only internal PostgreSQL maintenance. Never prints Secret values.
set -euo pipefail
umask 077

usage() {
  echo "Usage: $0 backup|inspect|verify|restore --namespace NAME --release NAME [--file ARCHIVE.dump] [--confirm-replace-database]" >&2
}
die() { printf '%s\n' "$*" >&2; exit 1; }
operation="${1:-}"
test "$#" -gt 0 || { usage; exit 2; }
shift
namespace=""
release=""
archive=""
confirmed=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --namespace|--release|--file)
      test "$#" -ge 2 || { usage; exit 2; }
      case "$1" in
        --namespace) namespace="$2" ;;
        --release) release="$2" ;;
        --file) archive="$2" ;;
      esac
      shift 2 ;;
    --confirm-replace-database) confirmed=true; shift ;;
    *) usage; exit 2 ;;
  esac
done
case "$operation" in backup|inspect|verify|restore) ;; *) usage; exit 2 ;; esac
[[ "$namespace" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || die 'An explicit namespace is required'
[[ "$release" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || die 'An explicit release is required'
test "${#release}" -le 53 || die 'Helm release name is too long'
if [ "$operation" = verify ] || [ "$operation" = restore ]; then
  [[ "$archive" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*\.dump$ ]] || die 'Select an existing safe .dump basename'
else
  test -z "$archive" || die '--file applies only to verify or restore'
fi
if [ "$operation" = restore ]; then
  [ "$confirmed" = true ] || die 'Restore replaces the database; require --confirm-replace-database'
fi

chart="$(cd "$(dirname "$0")/../deploy/helm/mm-bot-manager" && pwd -P)"
temporary_dir="$(mktemp -d)"
lock_name="${release}-db-maintenance"
lock_owned=false
resume_backup=false
safe_to_resume=true
current_job=""
current_kind=""
backup_cronjob=""
selector="app.kubernetes.io/instance=$release"
application_selector="$selector,app.kubernetes.io/component=application"
# Variables belong to the Go template, not the shell.
# shellcheck disable=SC2016
terminal_template='{{range .items}}{{ $terminal := false }}{{range .status.conditions}}{{if and (or (eq .type "Complete") (eq .type "Failed")) (eq .status "True")}}{{ $terminal = true }}{{end}}{{end}}{{if not $terminal}}{{.metadata.name}}{{"\n"}}{{end}}{{end}}'

cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  rm -rf "$temporary_dir"
  if [ "$safe_to_resume" = true ]; then
    if [ "$resume_backup" = true ]; then
      if ! kubectl -n "$namespace" patch cronjob "$backup_cronjob" --type=merge -p '{"spec":{"suspend":false}}'; then
        echo 'Could not resume backup CronJob; keep the maintenance lock and recover manually' >&2
        exit 1
      fi
    fi
    if [ "$lock_owned" = true ]; then
      kubectl -n "$namespace" delete configmap "$lock_name" --wait=true || status=1
    fi
  else
    printf '%s\n' "Maintenance is incomplete: Job ${current_job:-unknown} may still run or the database requires review. Keep backups suspended and check CronJob state; lock $lock_name retained. See docs/kubernetes.md." >&2
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM

# Read and render before suspending backups or stopping any application Pod.
# Installed values include the site's exact image digests, Secret/PVC names and pull Secrets.
helm get values "$release" --namespace "$namespace" --all --output yaml >"$temporary_dir/installed.yaml"
render_job() {
  kind="$1"
  template="$2"
  helm template "$release" "$chart" --namespace "$namespace" --kube-version 1.21.1 \
    --values "$temporary_dir/installed.yaml" --show-only "templates/$template" \
    --set database.internal.restore.enabled=false \
    --set database.internal.verification.enabled=false \
    --set database.internal.inspect.enabled=false \
    --set "database.internal.$kind.enabled=true" \
    --set-string "database.internal.$kind.file=$archive" >"$temporary_dir/$kind.yaml"
}
case "$operation" in
  inspect)
    # Inspect has no filename setting.
    helm template "$release" "$chart" --namespace "$namespace" --kube-version 1.21.1 \
      --values "$temporary_dir/installed.yaml" --show-only templates/backup-inspector-job.yaml \
      --set database.internal.restore.enabled=false --set database.internal.verification.enabled=false \
      --set database.internal.inspect.enabled=true >"$temporary_dir/inspect.yaml" ;;
  verify|restore) render_job verification backup-verification-job.yaml ;;
esac
if [ "$operation" = restore ]; then render_job restore restore-job.yaml; fi

backup_cronjob="$(kubectl -n "$namespace" get cronjob -l "$selector,app.kubernetes.io/component=backup" -o name)"
[[ "$backup_cronjob" == cronjob.batch/* && "$backup_cronjob" != *$'\n'* ]] || die 'Expected one internal backup CronJob'
backup_cronjob="${backup_cronjob#*/}"
original_replicas=0
if [ "$operation" = restore ]; then
  deployment="$(kubectl -n "$namespace" get deployment -l "$application_selector" -o name)"
  [[ "$deployment" == deployment.apps/* && "$deployment" != *$'\n'* ]] || die 'Expected one application Deployment; upgrade to chart 0.1.1 first'
  original_replicas="$(kubectl -n "$namespace" get "$deployment" -o jsonpath='{.spec.replicas}')"
  [[ "$original_replicas" =~ ^[01]$ ]] || die 'Expected zero or one application replica'
fi
backup_was_suspended="$(kubectl -n "$namespace" get cronjob "$backup_cronjob" -o jsonpath='{.spec.suspend}')"
case "$backup_was_suspended" in true|false|'') ;; *) die 'Unexpected backup CronJob suspend state' ;; esac
kubectl -n "$namespace" create configmap "$lock_name" \
  --from-literal="operation=$operation" --from-literal="originalReplicas=$original_replicas" \
  --from-literal="archive=$archive" --from-literal="backupWasSuspended=${backup_was_suspended:-false}" >/dev/null
lock_owned=true
active_maintenance="$(kubectl -n "$namespace" get jobs -l "$selector,app.kubernetes.io/component in (inspection,verification,restore)" -o "go-template=$terminal_template")"
test -z "$active_maintenance" || die 'A maintenance Job already exists; inspect it before proceeding'
case "$backup_was_suspended" in
  true) ;;
  false|'')
    # A failed API response may still have applied the patch; keep recovery information.
    safe_to_resume=false
    kubectl -n "$namespace" patch cronjob "$backup_cronjob" --type=merge -p '{"spec":{"suspend":true}}'
    resume_backup=true
    safe_to_resume=true ;;
  *) die 'Unexpected backup CronJob suspend state' ;;
esac
deadline=$((SECONDS + 600))
while true; do
  active_backups="$(kubectl -n "$namespace" get jobs -l "$selector,app.kubernetes.io/component=backup" -o "go-template=$terminal_template")"
  test -z "$active_backups" && break
  test "$SECONDS" -lt "$deadline" || die 'Backup Jobs did not become terminal within 10 minutes'
  echo 'Waiting for existing backup Jobs to finish'
  sleep 5
done

finish_job() {
  succeeded=false
  if kubectl -n "$namespace" wait --for=condition=complete "job/$current_job" --timeout=31m; then
    succeeded=true
  else
    terminal="$(kubectl -n "$namespace" get "job/$current_job" -o go-template='{{range .status.conditions}}{{if and (or (eq .type "Complete") (eq .type "Failed")) (eq .status "True")}}terminal{{end}}{{end}}')"
    [ "$terminal" = terminal ] || die 'Job state is running or unknown; do not resume backups'
  fi
  # Foreground deletion waits for all Job Pods, including terminating Pods.
  kubectl -n "$namespace" logs "job/$current_job" || echo 'Job logs unavailable' >&2
  kubectl -n "$namespace" delete "job/$current_job" --cascade=foreground --wait=true --timeout=10m
  current_job=""
  safe_to_resume=true
  if [ "$succeeded" = false ] && [ "$current_kind" = restore ]; then safe_to_resume=false; fi
  [ "$succeeded" = true ] || die 'Maintenance Job failed; application is not automatically restarted'
}
create_job() {
  current_kind="$1"
  safe_to_resume=false
  current_job="$(kubectl -n "$namespace" create -f "$temporary_dir/$1.yaml" -o jsonpath='{.metadata.name}')"
  test -n "$current_job" || die 'Job creation result is unknown'
  finish_job
}
case "$operation" in
  backup)
    current_kind=backup
    safe_to_resume=false
    current_job="$(kubectl -n "$namespace" create job --from="cronjob/$backup_cronjob" \
      "${release:0:40}-manual-$(date -u +%Y%m%d%H%M%S)" -o jsonpath='{.metadata.name}')"
    test -n "$current_job" || die 'Backup creation result is unknown'
    finish_job ;;
  inspect) create_job inspect ;;
  verify) create_job verification ;;
  restore)
    # Full trial restore must succeed before stopping the application or replacing its database.
    create_job verification
    kubectl -n "$namespace" scale "$deployment" --replicas=0
    kubectl -n "$namespace" rollout status "$deployment" --timeout=10m
    pods="$(kubectl -n "$namespace" get pods -l "$application_selector" -o name)"
    if [ -n "$pods" ]; then
      kubectl -n "$namespace" wait --for=delete pods -l "$application_selector" --timeout=10m
    fi
    remaining="$(kubectl -n "$namespace" get pods -l "$application_selector" -o name)"
    test -z "$remaining" || die 'Application Pods still exist; restore was not started'
    active_backups="$(kubectl -n "$namespace" get jobs -l "$selector,app.kubernetes.io/component=backup" -o "go-template=$terminal_template")"
    test -z "$active_backups" || die 'A backup Job appeared during maintenance; restore was not started'
    create_job restore
    kubectl -n "$namespace" scale "$deployment" --replicas="$original_replicas"
    kubectl -n "$namespace" rollout status "$deployment" --timeout=10m ;;
esac
echo "Database $operation completed"
