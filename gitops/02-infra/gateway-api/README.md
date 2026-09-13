# Gateway API CRDs

`manifests/standard-install.yaml` is the unmodified standard-channel bundle from
`kubernetes-sigs/gateway-api` release **v1.3.0**, vendored rather than fetched.

Vendored on purpose. These are CRDs: the cluster cannot be rebuilt without them,
and a build that pulls a bundle from a URL at sync time is reproducible only for
as long as that URL serves the same bytes. The version is visible in git, and
upgrading is a reviewable diff rather than a silent change in what a release tag
points at.

Nothing here is Cilium-specific. The same CRDs are what an Istio waypoint needs -
waypoints are `Gateway` resources, so until this existed there was no L7 in the
ambient mesh at all: no path/method authorization, no mesh-level canary, no
Istio-generated traces.

To upgrade: replace the file from the release of the same name and check that
the GatewayClass still reports Accepted, since the controller validates the CRD
version it finds.
