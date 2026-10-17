#!/usr/bin/env bash
# Read-only validation of the exact profile + mandatory site overlay used for installation.
set -euo pipefail
if [ "$#" -lt 2 ] || [ "$#" -gt 4 ]; then
  echo "Usage: $0 PROFILE.yaml SITE.yaml [RELEASE] [NAMESPACE]" >&2
  exit 2
fi
profile="$1"
site="$2"
release="${3:-mm-bot-manager}"
namespace="${4:-mm-bot-manager}"
chart="$(cd "$(dirname "$0")/../deploy/helm/mm-bot-manager" && pwd -P)"
manifest="$(mktemp)"
trap 'rm -f "$manifest"' EXIT
helm lint "$chart" --values "$profile" --values "$site"
helm template "$release" "$chart" --namespace "$namespace" --kube-version 1.21.1 \
  --values "$profile" --values "$site" >"$manifest"
if grep -Eq 'CHANGE_ME|mm\.internal\.example|mm\.example\.com|harbor\.internal\.example|image: "?mm-bot-manager:' "$manifest"; then
  echo 'Replace all example image repositories, tags and Mattermost URLs in the site overlay' >&2
  exit 1
fi
echo 'Site overlay renders without the built-in deployment placeholders'
