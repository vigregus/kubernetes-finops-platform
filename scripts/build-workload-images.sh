#!/usr/bin/env bash
# Builds checkout/analytics locally and loads them into minikube's image
# cache. There is no registry for these images - Local v1 runs entirely
# off locally-built images, so this must be re-run after any change under
# apps/checkout or apps/analytics before Argo CD will pick up a new image.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="${1:-local}"

for app in checkout analytics; do
  echo "==> building ${app}:${TAG}"
  docker build -t "${app}:${TAG}" "${REPO_ROOT}/apps/${app}"
  echo "==> loading ${app}:${TAG} into minikube"
  minikube image load "${app}:${TAG}"
done

echo "==> done. Restart the deployments to pick up the new image if the tag didn't change:"
echo "    kubectl rollout restart deployment/checkout -n prod deployment/checkout -n stage"
echo "    kubectl rollout restart deployment/analytics -n stage deployment/analytics -n prod"
