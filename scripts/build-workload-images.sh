#!/usr/bin/env bash
# Builds checkout/analytics directly against minikube's own docker daemon.
# There is no registry for these images - Local v1 runs entirely off
# locally-built images, so this must be re-run after any change under
# apps/checkout or apps/analytics before Argo CD will pick up a new image.
#
# Builds straight into minikube's daemon (via `minikube docker-env`)
# rather than building on the host and `minikube image load`-ing the
# result: `image load` treats same-tag images as already present and
# silently skips reloading even with --overwrite=true, which leaves
# stale code running with no error anywhere in the chain.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="${1:-local}"

eval "$(minikube docker-env)"

for app in checkout analytics; do
  echo "==> building ${app}:${TAG} into minikube's docker daemon"
  docker build -t "${app}:${TAG}" "${REPO_ROOT}/apps/${app}"
done

echo "==> done. Restart the deployments to pick up the new image:"
echo "    kubectl rollout restart deployment/checkout -n prod deployment/checkout -n stage"
echo "    kubectl rollout restart deployment/analytics -n stage deployment/analytics -n prod"
