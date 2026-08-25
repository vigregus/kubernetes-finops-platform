# Local setup

This project assumes a local `minikube` cluster (Docker driver) for Local v1.

## Bootstrap order

1. Start minikube with enough headroom for the full observability stack:
   `minikube start --memory=16000 --cpus=6`.
2. Install Argo CD into the `argocd` namespace (standard `kubectl`/`helm`
   install - not GitOps-managed itself, since it has to exist before
   anything else can be applied).
3. If deploying from a fork, update `repoURL` in every `application.yaml`
   under `gitops/` (they all point at
   `https://github.com/vigregus/kubernetes-finops-platform.git`).
4. Apply the root application: `kubectl apply -f gitops/01-root/project.yaml -f gitops/01-root/root-application.yaml`.
5. Let Argo CD reconcile - it discovers one `application.yaml` per
   component under `gitops/02-infra`, `gitops/03-finops`, and
   `gitops/04-business-app` (see `gitops/01-root/root-application.yaml`'s
   `directory.include` glob) and installs them in `sync-wave` order:
   infra (CNPG operator, Kyverno, ingress-nginx, Kafka operator,
   observability stack) before the FinOps layer (OpenCost, Goldilocks)
   before the business apps.
6. `checkout`/`analytics` have no registry - build their images directly
   into minikube's own Docker daemon: `scripts/build-workload-images.sh`.
   Re-run this after any change under `apps/checkout` or
   `apps/analytics`, then `kubectl rollout restart deployment/checkout
   deployment/analytics -n prod -n stage` (image tags don't change, so
   Argo CD alone won't pick up new code).
7. Add the app hostnames to `/etc/hosts`, pointed at `127.0.0.1`, and
   keep a `kubectl port-forward -n ingress-nginx
   svc/ingress-nginx-controller <local-port>:80` running - on macOS with
   the Docker driver, minikube's own IP is not directly reachable from
   the host, so hitting the ingress via `minikube ip`'s NodePort does
   not work from a browser:
   ```
   127.0.0.1  checkout.finops.local checkout-stage.finops.local \
              analytics.finops.local analytics-stage.finops.local \
              opencost.finops.local shop.finops.local
   ```
8. Create any required local secrets outside Git (see "Local secret
   handling" below).
9. Run `k6` scenarios from `tests/k6` (see that directory's own README)
   against the ingress, not the Kubernetes Service directly, so load
   also exercises the real trace origin.

## Local secret handling

Local v1 must not store secret values in Git.

Allowed approaches:

- create secrets from ignored local files,
- create secrets directly with `kubectl`,
- use local secret tooling that reads from ignored sources.

Do not commit:

- `.env` files with real values,
- kubeconfig files,
- cloud credentials,
- Grafana admin passwords,
- Terraform variable files with real secrets.
