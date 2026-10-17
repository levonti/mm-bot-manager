#!/usr/bin/env bash
set -euo pipefail

chart="deploy/helm/mm-bot-manager"
schema_dir="${KUBECONFORM_SCHEMA_LOCATION:-}"

if [[ -z "$schema_dir" ]]; then
  echo "KUBECONFORM_SCHEMA_LOCATION must name a local directory with Kubernetes 1.21.1 schemas." >&2
  exit 2
fi
if [[ ! -d "$schema_dir" ]]; then
  echo "KUBECONFORM_SCHEMA_LOCATION is not a local directory: $schema_dir" >&2
  exit 2
fi
schema_dir="$(cd "$schema_dir" && pwd -P)"

profiles=(
  "values-external.yaml"
  "values-internal-longhorn.yaml"
  "values-internal-local-path.yaml"
  "values-harbor.yaml"
)
temporary_dir="$(mktemp -d)"
trap 'rm -rf "$temporary_dir"' EXIT

helm lint "$chart"

validate_manifest() {
  kubeconform \
    -strict \
    -summary \
    -kubernetes-version 1.21.1 \
    -schema-location "$schema_dir/{{ .ResourceKind }}{{ .KindSuffix }}.json" \
    "$1"
}

for profile in "${profiles[@]}"; do
  output="$temporary_dir/${profile%.yaml}.yaml"
  helm template mmbot "$chart" \
    --namespace mmbot \
    --kube-version 1.21.1 \
    --values "$chart/examples/$profile" >"$output"
  validate_manifest "$output"
done

for operation in inspect verification restore; do
  output="$temporary_dir/$operation.yaml"
  extra=(--set "database.internal.$operation.enabled=true")
  if [[ "$operation" != inspect ]]; then
    extra+=(--set-string "database.internal.$operation.file=archive.dump")
  fi
  helm template mmbot "$chart" --namespace mmbot --kube-version 1.21.1 \
    --values "$chart/examples/values-harbor.yaml" "${extra[@]}" >"$output"
  validate_manifest "$output"
done

output="$temporary_dir/private-ca.yaml"
helm template mmbot "$chart" --namespace mmbot --kube-version 1.21.1 \
  --values "$chart/examples/values-internal-longhorn.yaml" \
  --set mattermost.caBundle.existingConfigMap=mattermost-ca >"$output"
validate_manifest "$output"
