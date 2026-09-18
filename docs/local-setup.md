# Local setup

This project assumes a local `minikube` cluster (Docker driver) for Local v1.

## Bootstrap order

1. Start minikube with enough headroom for the full observability stack, and
   without a CNI or kube-proxy, because Cilium replaces both:

   ```
   minikube start --cpus=14 --memory=22000 --disk-size=60g \
     --kubernetes-version=v1.33.1 --cni=false \
     --extra-config=kubeadm.skip-phases=addon/kube-proxy
   ```

   Then install Cilium once by hand - it cannot come from Argo CD, because
   Argo CD's own pods need a CNI before they can start:

   ```
   helm repo add cilium https://helm.cilium.io/ && helm repo update cilium
   helm install cilium cilium/cilium -n kube-system --version 1.20.1 \
     -f gitops/02-infra/cilium/values.yaml --wait
   ```

   Argo CD adopts it afterwards (`gitops/02-infra/cilium/`), so this is the
   only manual step in its lifetime rather than its permanent state.

1b. **Point CoreDNS at a public resolver.** On the Docker driver, the node's
   `/etc/resolv.conf` points at Docker Desktop's internal resolver
   (`192.168.65.254`), which is reachable from the node's own network
   namespace but not from inside pods. CoreDNS forwards to it by default, so
   every external name fails with `i/o timeout` while general egress works
   fine - pods can ping 8.8.8.8 and open TCP 443, they just cannot resolve.
   The visible symptom is Argo CD's repo-server restarting in a loop
   (`failed to list refs ... lookup github.com ... server misbehaving`), which
   looks like a git or RBAC problem and is neither.

   ```
   kubectl -n kube-system get cm coredns -o jsonpath='{.data.Corefile}' \
     | sed 's|forward . /etc/resolv.conf|forward . 8.8.8.8 1.1.1.1|' > /tmp/Corefile
   kubectl -n kube-system create cm coredns --from-file=Corefile=/tmp/Corefile \
     --dry-run=client -o yaml | kubectl apply -f -
   kubectl -n kube-system rollout restart deploy/coredns
   ```
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

Local v1 must not store secret values in Git. Detailed secrets policy and ESO mappings: [docs/secrets-policy.md](secrets-policy.md).

Allowed approaches:

- for host-based local service development, copy `apps/messenger/.env.example` to `apps/messenger/.env` and fill in credentials extracted via `kubectl get secret ...`,
- create secrets directly in the cluster with `kubectl`,
- let in-cluster operators and `messenger-secrets-bootstrap` generate local credentials automatically (see [docs/secrets-policy.md](secrets-policy.md)).

Do not commit:

- `.env` files with real values,
- kubeconfig files,
- cloud credentials,
- Grafana admin passwords,
- Terraform variable files with real secrets.

