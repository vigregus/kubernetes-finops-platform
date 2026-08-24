# kubernetes-finops-platform

`kubernetes-finops-platform` is a reference repository for application-level cost attribution and resource efficiency analysis in Kubernetes. The project focuses on a narrow, testable path: connect GitOps-managed platform components, workload metadata, operational telemetry, rightsizing signals, and cloud billing inputs into a single FinOps model.

The goal is not to build a generic internal developer platform or a kitchen-sink observability stack. Each component in this repository exists only if it supports one of these questions:

- Which product or workload consumes infrastructure resources?
- Is current resource sizing justified by real traffic and latency?
- What is the modeled or actual cost of serving application demand?
- How does infrastructure efficiency change after rightsizing?

## Problem

Kubernetes clusters usually expose CPU, memory, logs, and dashboards, but they rarely expose cost in a way that maps cleanly to products, teams, and environments. Billing systems provide account-level or service-level spend, while Kubernetes telemetry describes pod-level usage. Without a consistent ownership model and a reconciliation path between telemetry and billing, teams can observe utilization but still fail to answer:

- what a product actually costs,
- whether overprovisioning is material,
- how to compare cost before and after a change,
- how to express unit economics such as cost per 1M requests.

This repository is intended to make that path explicit and reproducible.

## Goals

- Define a GitOps-managed platform boundary for FinOps-related Kubernetes components.
- Enforce a consistent application metadata model with labels for `product`, `team`, `environment`, and `component`.
- Collect metrics, logs, and traces needed to reason about workload behavior and resource efficiency.
- Estimate modeled cost locally from Kubernetes allocation and usage signals.
- Measure unit economics with a baseline metric: `Cost per 1M requests`.
- Demonstrate a repeatable local workflow before expanding into cloud billing reconciliation.

## Expected Outcomes

If the repository is implemented as intended, it should produce a concrete engineering result rather than just a deployed toolset.

Expected outcomes for Local v1:

- a reproducible local environment where the same workloads can be deployed, observed, and analyzed through GitOps,
- a consistent ownership model that maps workloads to product, team, environment, and component,
- dashboards that connect traffic, latency, resource requests, actual usage, and modeled cost in one place,
- an explicit view of overprovisioning or underprovisioning candidates,
- a modeled product-cost breakdown for the sample workloads,
- a measurable `Cost per 1M requests` signal for `checkout` and `analytics`,
- a before/after comparison path for rightsizing decisions.

Expected outcomes for AWS v2:

- the same ownership model carried into cloud infrastructure and billing datasets,
- reconciliation between Kubernetes allocation and cloud billing records,
- product-level reporting based on actual or effective cloud cost rather than local modeled estimates alone.

## Non-goals

- Multi-cluster platform management in v1.
- Production-grade HA, disaster recovery, or enterprise access model in v1.
- A generic service mesh, CI platform, or internal developer portal.
- Full multi-cloud billing support in the first implementation.
- A broad collection of tools that are not required for GitOps, observability, governance, or FinOps attribution.

## Scope Overview

### Local v1

Local v1 is intentionally constrained to one local `k3s` cluster with three namespaces representing `dev`, `stage`, and `prod`.

Included components:

- Argo CD for GitOps delivery.
- VictoriaMetrics for metrics.
- VictoriaLogs for logs.
- Tempo for traces.
- Grafana plus Grafana Operator.
- Dashboards-as-code in `platform/observability/grafana/dashboards/`.
- OpenCost for Kubernetes cost allocation.
- Goldilocks and VPA for rightsizing recommendations.
- Kyverno for governance and metadata policy.
- Two sample workloads: `checkout` and `analytics`.
- `k6` load tests to generate controlled traffic and compare before/after resource efficiency.

Key local outputs:

- namespace and application ownership taxonomy,
- modeled product cost,
- capacity and rightsizing signals,
- request-volume-aware unit economics,
- `Cost per 1M requests` for sample workloads.

### AWS v2

AWS v2 extends the model from local attribution to actual cloud cost reconciliation.

Planned scope:

- Terraform foundation.
- One EKS platform split into `prod` and `nonprod` environments.
- Required AWS resources such as VPC, load balancers, and RDS where they are needed by the workloads.
- Karpenter for node provisioning.
- AWS Split Cost Allocation Data.
- CUR and FOCUS datasets queried through Athena.
- Reconciliation between OpenCost allocation and billing-derived actual or effective product costs.

Future billing adapters for Azure, GCP, and Yandex Cloud may be added later, but they are explicitly out of scope for Local v1.

## Architecture

### Platform boundary

The repository is organized around a clear boundary:

- `platform/` contains shared cluster-level components managed through GitOps.
- `workloads/` contains sample applications and their deployment manifests.
- `tests/` contains synthetic load generation and verification assets.
- `infrastructure/` contains cloud foundation code for phases beyond local v1.

The platform layer should remain narrowly scoped. Components belong here only when they provide one of the following:

- deployment control,
- governance,
- telemetry collection,
- cost allocation,
- rightsizing signals,
- cost visualization.

### End-to-end flow

The intended analytical flow is:

`GitOps manifests -> labeled workloads -> workload telemetry -> OpenCost allocation -> Grafana dashboards -> modeled product cost -> Cost per 1M requests`

In AWS v2 the flow extends further:

`modeled Kubernetes cost -> CUR / FOCUS / Split Cost Allocation Data -> Athena reconciliation -> actual or effective product cost`

This flow is the reason the repository exists. If a component does not improve one step in this chain, it should not be added.

### GitOps boundary

Argo CD is the control plane for deploying repository-managed Kubernetes resources. The intended boundary is:

- declarative manifests live in Git,
- cluster state converges through Argo CD,
- dashboards are versioned as code,
- governance policies are versioned as code,
- secrets are not stored in Git.

Local bootstrap may require an initial manual cluster install step, but post-bootstrap platform state should be managed through GitOps definitions.

### Observability model

Observability exists here to support FinOps decisions, not as an end in itself.

- VictoriaMetrics stores metrics required for workload usage, saturation, latency, and request volume analysis.
- VictoriaLogs stores logs for workload and platform troubleshooting when analyzing anomalies.
- Tempo stores traces for request-path visibility and performance correlation.
- Grafana is the primary analysis surface.
- Grafana Operator manages dashboard and datasource resources declaratively.

Initial dashboard set:

- `application-overview`: traffic, latency, errors, resource usage, and ownership context for a workload.
- `kubernetes-capacity`: cluster and namespace capacity, requests, limits, saturation, and rightsizing candidates.
- `finops`: modeled cost, allocation views, unit economics, and before/after comparisons.

### FinOps model

The v1 FinOps model is intentionally simple and auditable.

Inputs:

- workload ownership labels,
- namespace and environment metadata,
- Kubernetes requests and limits,
- actual usage metrics,
- OpenCost allocation data,
- request counts from application telemetry.

Outputs:

- modeled cost per workload,
- modeled cost per product,
- environment split,
- utilization versus requested capacity,
- rightsizing opportunities,
- `Cost per 1M requests`.

The local model is not presented as cloud-billing truth. It is a reproducible approximation used to validate ownership, telemetry quality, and the analytical workflow. Billing truth and reconciliation arrive in AWS v2.

### Modeled cost versus actual cost

The repository uses two different cost concepts and keeps them separate on purpose.

- `modeled cost` is derived from Kubernetes allocation, requests, usage, and OpenCost-related cost logic; it is the primary Local v1 signal,
- `actual cost` is derived from cloud billing records and provider cost datasets,
- `effective cost` may include allocation or shared-cost rules used to assign billed spend to products more fairly.

Local v1 is successful if modeled cost is stable, explainable, and useful for engineering decisions. AWS v2 is the phase where modeled allocation is reconciled against billing-derived cost.

## Labels and Tags

Every managed workload should carry a minimal ownership schema:

- `app.kubernetes.io/name`
- `app.kubernetes.io/component`
- `finops.openai.io/product`
- `finops.openai.io/team`
- `finops.openai.io/environment`
- `finops.openai.io/component`

Purpose:

- map Kubernetes objects to products and teams,
- keep dashboard filtering consistent,
- support policy validation through Kyverno,
- enable aggregation for cost and unit economics.

Cloud phases should map the same ownership concepts to provider-native tags where possible.

## Environments

Local v1 uses one `k3s` cluster with three namespaces:

- `dev`
- `stage`
- `prod`

This is not a claim of production isolation. It is a controlled local model used to test:

- environment-aware metadata,
- GitOps layout,
- dashboard filtering,
- allocation logic,
- before/after comparisons across consistent workload definitions.

AWS v2 moves from namespace-only separation to an EKS-backed environment model with `prod` and `nonprod`.

## Sample Workloads

Two sample workloads are included to keep the platform concrete:

- `checkout`
- `analytics`

They should be intentionally simple but instrumented enough to expose:

- request volume,
- latency,
- error rate,
- resource usage,
- trace spans,
- ownership labels.

`k6` scenarios generate repeatable traffic so that dashboards and modeled cost outputs can be compared across tuning changes.

## User Scenarios

The repository should support a small set of practical questions.

### Platform engineer

A platform engineer should be able to deploy the stack into a local cluster, inspect whether workloads follow the required metadata model, and verify that dashboards and cost views are generated from Git-managed configuration.

### SRE or infrastructure engineer

An SRE should be able to identify whether `checkout` or `analytics` is over-requesting CPU or memory, compare utilization against requests and limits, review Goldilocks or VPA recommendations, and observe how a sizing change affects cost signals.

### Product or engineering owner

A product or engineering owner should be able to see which sample workload is more expensive to operate, how that cost changes with traffic, and what the estimated `Cost per 1M requests` looks like over a comparable time window.

## Unit Economics

The primary unit-economics metric for v1 is:

`Cost per 1M requests = total modeled application cost / request_count * 1,000,000`

This metric is only meaningful when:

- request telemetry is stable,
- workload ownership labels are complete,
- OpenCost allocation is mapped correctly,
- time windows are comparable.

The purpose is not accounting precision. The purpose is to create an engineering signal that links resource configuration, traffic, and cost.

## What Success Looks Like

The repository is useful only if it can answer a bounded set of questions with evidence.

For Local v1, success means the project can show:

- which product and team own a workload,
- how much traffic that workload serves,
- what resources it requests and actually uses,
- whether rightsizing is justified,
- what the modeled cost is for that workload or product,
- how that modeled cost translates into `Cost per 1M requests`,
- how the numbers change after a resource-tuning decision.

For AWS v2, success means those same questions can be answered with a reconciliation path to billing-derived cost.

## Example Analytical Result

An expected Local v1 outcome is not merely “Grafana is running” or “OpenCost is installed”. A meaningful result looks more like this:

- `checkout` in `prod` serves a known request volume,
- current CPU and memory requests are materially above observed usage,
- Goldilocks or VPA recommends lower requests,
- after applying a sizing change, latency remains acceptable,
- modeled workload cost decreases,
- `Cost per 1M requests` improves over a comparable interval.

That is the level of result the repository is intended to make repeatable.

## Implementation Roadmap

### Phase 1: Repository foundation

- Create repository structure for platform, workloads, tests, and cloud infrastructure.
- Define metadata conventions, placeholder manifests, and secrets policy.
- Document the GitOps and FinOps boundaries clearly.

### Phase 2: Local cluster baseline

- Bootstrap one `k3s` cluster.
- Create `dev`, `stage`, and `prod` namespaces.
- Install Argo CD and define the initial application layout.
- Install Kyverno and validate required labels.

### Phase 3: Telemetry and dashboards

- Install VictoriaMetrics, VictoriaLogs, Tempo, Grafana, and Grafana Operator.
- Add datasources and dashboards-as-code.
- Validate that sample workloads appear with environment and ownership metadata.

### Phase 4: Cost attribution and rightsizing

- Install OpenCost.
- Install Goldilocks and VPA.
- Surface rightsizing and cost views in Grafana.
- Establish a reproducible modeled-cost view for `checkout` and `analytics`.

### Phase 5: Load-driven unit economics

- Add `k6` load scenarios.
- Capture baseline traffic and resource behavior.
- Calculate and visualize `Cost per 1M requests`.
- Compare results before and after sizing changes.

### Phase 6: AWS reconciliation

- Add Terraform foundation for AWS.
- Stand up EKS and required surrounding infrastructure.
- Integrate CUR, FOCUS, Athena, and AWS Split Cost Allocation Data.
- Reconcile OpenCost allocation with billing-derived actual or effective product cost.

## Repository Structure

```text
kubernetes-finops-platform/
├── README.md
├── .gitignore
├── clusters/
│   └── local/
├── docs/
│   ├── ADR/
│   └── secrets-policy.md
├── examples/
│   ├── app-metadata.example.yaml
│   └── local-secret.example.yaml
├── infrastructure/
│   └── aws/
│       └── terraform/
├── platform/
│   ├── gitops/
│   │   └── bootstrap/
│   ├── governance/
│   │   └── kyverno/
│   ├── finops/
│   │   ├── goldilocks/
│   │   └── opencost/
│   ├── local/
│   └── observability/
│       ├── grafana/
│       ├── tempo/
│       ├── victorialogs/
│       └── victoriametrics/
├── tests/
│   └── k6/
└── workloads/
    ├── analytics/
    │   ├── base/
    │   └── local/
    ├── checkout/
    │   ├── base/
    │   └── local/
    └── local/
```

## Implementation Assets Already Included

The repository now contains a working scaffold for the first implementation steps:

- local namespace definitions for `argocd`, `observability`, `finops-system`, `governance-system`, `dev`, `stage`, and `prod`,
- an Argo CD root application and local application split between governance, observability, platform, and workloads,
- a GitOps-managed Kyverno installation plus a Kyverno policy for required FinOps labels,
- local entrypoints for observability and FinOps components,
- a dedicated GitOps-managed `observability-local` application that owns metrics, logs, traces, dashboards, and the VictoriaMetrics stack,
- placeholder dashboards-as-code ConfigMaps,
- sample workload manifests for `checkout` and `analytics`,
- initial `k6` scenarios.

This is still a scaffold, not a completed runtime stack. The remaining implementation work is primarily wiring the chosen Helm releases or operators into the paths that already exist in the repository.

## Security and Secrets Policy

No secrets, credentials, tokens, kubeconfigs, or cloud access keys should be committed to Git.

Rules for this repository:

- commit only templates such as `*.example` files,
- keep placeholder directories with `.gitkeep` where needed,
- ignore local secret files in `.gitignore`,
- avoid embedding credentials in manifests, dashboards, or Terraform variables,
- use Kubernetes Secrets in Local v1 only through a safe local mechanism outside Git,
- use a cloud secret manager and External Secrets in cloud phases.

Recommended approach:

- Local v1: create secrets locally from ignored files or local secret tooling, then reference them from manifests without storing values in the repository.
- Cloud phase: use a managed secret store and synchronize into Kubernetes through External Secrets or an equivalent controller.

## Local Prerequisites

Local v1 assumes:

- a workstation capable of running one local `k3s` cluster,
- `kubectl`,
- `helm`,
- `argocd` CLI or UI access for validation,
- Docker or another supported container runtime for local images when required,
- `k6` for load generation,
- enough CPU and memory to run observability components locally.

Tooling details can evolve, but the repository should not depend on hidden manual steps or secret values committed in Git.

## Local Bootstrap

The intended local path is deliberately standard:

1. Create a local `k3s` cluster.
2. Install Argo CD with `kubectl` and `helm` according to the chosen local workflow.
3. Update the repository URL in `platform/gitops/bootstrap/root-application.yaml`.
4. Apply the root Argo CD application with `kubectl`.
5. Let Argo CD reconcile `clusters/local`.
6. Let Argo CD install GitOps-managed platform dependencies such as Kyverno, then wire the remaining Helm-based platform components through the repository paths already defined under `platform/`.
7. Create any required local secrets outside Git.
8. Deploy workloads and run `k6` scenarios.

## Definition of Done for Local v1

Local v1 is complete when all of the following are true:

- one local `k3s` cluster is running,
- `dev`, `stage`, and `prod` namespaces exist,
- Argo CD manages the repository-defined platform resources,
- VictoriaMetrics, VictoriaLogs, Tempo, Grafana, and Grafana Operator are deployed,
- dashboards exist as code under `platform/observability/grafana/dashboards`,
- OpenCost is deployed and exposes allocation data,
- Goldilocks and VPA provide rightsizing recommendations,
- Kyverno validates required application labels,
- `checkout` and `analytics` workloads are deployed with ownership metadata,
- `k6` scenarios generate repeatable traffic,
- Grafana shows workload overview, cluster capacity, and FinOps views,
- modeled cost is available per workload and per product,
- `Cost per 1M requests` is computed for the sample workloads,
- no real secrets are present in Git.

## Notes on Future Scope

Azure, GCP, and Yandex Cloud billing adapters are reasonable future extensions once the local model and the AWS reconciliation path are stable. They should reuse the same ownership model and reporting vocabulary rather than introducing provider-specific semantics into Local v1.
