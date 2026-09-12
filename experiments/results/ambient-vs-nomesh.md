# Istio ambient vs no mesh — stress, 2026-09-12

What the ambient mesh costs on this stand, measured rather than quoted. Two
`stress` runs (ramping arrival rate to 600 rps over 10 minutes, `RPS_TARGET=150`),
identical in every respect except the `istio.io/dataplane-mode` label on the
`prod` namespace.

| | no mesh | ambient | delta |
|---|---|---|---|
| Sustained rps at p95 < 500ms | **332** | **293** | **−11.7%** |
| Peak accepted rps | 353 | 319 | −9.6% |
| p95 at peak | 632 ms | 588 ms | — |
| App CPU at peak (`prod`) | 2.796 cores | 2.306 cores | — |
| Mesh CPU at peak (`istio-system`) | 0.004 cores | **0.387 cores** | +0.383 |
| Total CPU per request | 8.16 ms·core | 8.95 ms·core | **+9.7%** |
| Mesh share of total CPU | 0.1% | **14.4%** | |
| Max 429/s | 691 | 802 | |
| Max 5xx/s | 0.20 | 0.57 | |

Raw per-15s samples: `ambient-vs-nomesh-nomesh.csv`, `ambient-vs-nomesh-ambient.csv`.

## Reading it

The mesh costs roughly **12% of SLO capacity and 10% more CPU per request**, and
that second number is the one that belongs in a cost model: at fixed throughput
the bill rises by about a tenth. What it buys is mTLS between every workload
with SPIFFE identities and L4 authorization, with no application change — the
access logs show `checkout` reaching `checkout-db-pooler` over HBONE on port
15008 with both identities named, which is transport the application never had
to know about.

The comparison against **sidecars** is the one that matters for the cost story,
and it is structural rather than marginal: ztunnel is one per *node*, so its
0.387 cores is shared by every pod on that node. A sidecar is one per *pod* at
roughly 0.20 cores and 60–100MB each; across the twenty-odd pods here that is
about 4 cores spent on proxies before a request is served. The mesh here cost
0.39.

## Validity

Honest limits on these numbers:

- **`dropped_iterations` exceeded its threshold in both runs** (787 no-mesh, 412
  ambient, against a bound of 100). k6 could not issue every scheduled arrival at
  the top of the ramp, so both peak figures are lower bounds on offered load
  rather than exact capacities. Both arms are affected in the same direction and
  the gap between them is well outside that error, but a precise capacity number
  needs a generator with headroom — it is pinned to the same node as the workload
  on this single-node cluster.
- **Single node.** ztunnel's per-node cost is amortised across every pod here.
  On a multi-node cluster that cost multiplies by node count while the sidecar
  alternative multiplies by pod count — the gap widens in ambient's favour as
  pods-per-node rises, and narrows as nodes are added.
- **Cache warmth differed slightly** between runs (23.0% vs 21.6% hit ratio at
  peak), which flatters the no-mesh arm by a small amount.
- Runs were back to back on the same cluster with the same images, immediately
  after `PaymentValidationError` began returning 402 rather than 500, so neither
  arm carries injected declines as 5xx.

## Reproducing

```
scripts/run-loadgen.sh stress-nomesh stress 150 5m
python3 scripts/sample_load.py --duration 640 --interval 15 --csv nomesh.csv
```

with `istio.io/dataplane-mode` removed from and then restored to the `prod`
namespace in `gitops/02-infra/infra-bootstrap/values.yaml`. Pods must be
restarted after removing the label: redirection is established when the pod is
created, so existing pods stay captured until they are replaced.
