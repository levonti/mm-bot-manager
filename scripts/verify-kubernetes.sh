#!/usr/bin/env bash
set -euo pipefail

chart="deploy/helm/mm-bot-manager"
profiles=(
  "values-external.yaml"
  "values-internal-longhorn.yaml"
  "values-internal-local-path.yaml"
  "values-harbor.yaml"
)
temporary_dir="$(mktemp -d)"
trap 'rm -rf "$temporary_dir"' EXIT

helm lint "$chart"

for profile in "${profiles[@]}"; do
  output="$temporary_dir/${profile%.yaml}.yaml"
  helm template mmbot "$chart" \
    --namespace mmbot \
    --kube-version 1.21.1 \
    --values "$chart/examples/$profile" >"$output"
  kubeconform \
    -strict \
    -summary \
    -kubernetes-version 1.21.1 \
    "$output"
done
