# ADR 0002: Platform overhead is in scope as an object of measurement

## Status

Accepted

## Context

ADR 0001 admits only components that directly strengthen the cost-attribution
path, and the README lists "a generic service mesh, CI platform, or internal
developer portal" among the non-goals. Read literally, that excludes a service
mesh and runtime security tooling.

That reading is right about the failure mode it guards against — a repository
that accumulates platform components because they are interesting — but it
also excludes a class of cost that FinOps work exists to answer questions
about. A service mesh, an image scanner and a runtime-security agent are not
free: they consume CPU and memory continuously, their consumption scales on a
different axis from the application's, and in real clusters they routinely
account for a double-digit percentage of compute spend.

"What does our platform tooling cost, and is it worth it?" is a FinOps
question. A repository that cannot answer it is narrower than it needs to be,
while a repository that installs these components without measuring them has
simply grown into the generic platform ADR 0001 was written to prevent.

The distinction that resolves this is *why* a component is present, not *what*
it is.

## Decision

Platform overhead is in scope when it is installed as an object of
measurement — that is, when all of the following hold:

1. Its resource consumption is attributed through the same
   `finops.internal/*` label model as every other workload, so it appears in
   the existing cost-by-product and cost-by-component views rather than
   disappearing into untracked overhead.
2. It can be switched off, so the cost of running it can be compared against
   the cost of not running it. A component that cannot be turned off is
   infrastructure, not a measured variable.
3. Its cost profile is documented — specifically, what its consumption scales
   with: per pod, per node, per request, or in bursts. This is what makes it
   interact meaningfully with the node and autoscaling strategies.

Under this rule the following are admitted:

- **Istio**, in ambient mode by default. Sidecar mode stays available as an
  experiment variant precisely because the sidecar-versus-ambient difference
  is one of the larger cost deltas available to measure.
- **Falco**, whose cost scales per node and therefore interacts directly with
  bin-packing decisions.
- **Trivy Operator**, whose cost is bursty and competes with application
  workloads for the same cores.

## Consequences

- The README non-goal is narrowed from "a generic service mesh" to a mesh
  adopted as default infrastructure. A mesh present as a measurable variant is
  in scope.
- Every component admitted under this ADR must ship with a way to disable it
  and with its cost profile recorded, or it falls back under ADR 0001's
  exclusion.
- The load stand is the mechanism that makes this ADR honest: without the
  ability to run the same load against a variant with and without a component,
  "measured" degrades into "installed and labelled".
- This ADR does not widen scope to CI platforms or developer portals, which
  remain non-goals — they are not workloads whose cost this cluster carries.
