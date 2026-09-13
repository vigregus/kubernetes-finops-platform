# Gateway API CRDs

`manifests/experimental-install.yaml` is the unmodified **experimental-channel**
bundle from `kubernetes-sigs/gateway-api` release **v1.5.0**, vendored rather
than fetched.

## Why experimental, and why this exact version

Not a preference - a requirement Cilium states at runtime. The standard channel
was tried first and the operator refused it, naming precisely what was missing:

```
Required GatewayAPI resources are not found
  customresourcedefinitions "tlsroutes.gateway.networking.k8s.io" not found
  CRD "referencegrants.gateway.networking.k8s.io" does not have version "v1"
  customresourcedefinitions "backendtlspolicies.gateway.networking.k8s.io" not found
```

`tlsroutes` and `backendtlspolicies` exist only in the experimental channel. The
third is the one that pins the version: ReferenceGrant is `v1beta1` in every
release through v1.4.1 and reaches `v1` in **v1.5.0**. So v1.5.0-experimental is
the floor, not a choice.

The symptom of getting this wrong is quiet: the GatewayClass sits at
`Accepted=Unknown`, reason `Pending`, message "Waiting for controller" - which
reads like a controller that has not started rather than a controller that
looked, found the wrong CRDs, and logged why.

## Why vendored

These are CRDs: the cluster cannot be rebuilt without them, and a bundle pulled
from a URL at sync time is reproducible only for as long as that URL serves the
same bytes. The version is visible in git and an upgrade is a reviewable diff.

Pruning is disabled on the application for the same reason it is disabled for
the CNI - removing these deletes every Gateway and HTTPRoute in the cluster.

## Not Cilium-specific

Nothing here belongs to Cilium. The same CRDs are what an Istio waypoint needs -
waypoints *are* Gateway resources - so until this existed the ambient mesh had
no L7 at all: no path or method authorization, no mesh-level canary, no
Istio-generated traces. Both controllers now register against these CRDs, which
is exactly why they are not shipped by either one.
