# Verifying the prod authorization policies

A policy that is *applied* and a policy that *refuses* are different states, and
only one of them is worth anything. This is how the ten policies in
`manifests/` were checked, and the result of that check.

## How to probe, and how not to

`connect()` succeeding proves nothing in ambient. The socket opens against the
local ztunnel every time; the refusal arrives when the tunnel to the far side is
established. A first pass at this reported "open" for three flows that are in
fact refused, purely because it stopped at `connect()`.

Every probe must exchange real bytes:

- **Postgres / PgBouncer** — send an SSLRequest (`00 00 00 08 04 d2 16 2f`).
  A live server answers with a single byte, `S` or `N`. A refusal resets.
- **Redis** — send `PING`. A live server answers `+PONG`.
- **HTTP** — send a request and read the status line.

Refusal appears as `ConnectionResetError`, not as a 403: authorization is
enforced at L4 by ztunnel, so the connection dies before any application
protocol happens. A 403 would mean something else entirely.

## Result — 19 of 19 as intended

| From (identity) | To | Expected | Observed |
|---|---|---|---|
| `prod/checkout` | own pooler :5432 | allow | access |
| `prod/checkout` | **checkout-db-rw :5432 (bypassing the pooler)** | **deny** | **reset** |
| `prod/checkout` | checkout-db-ro :5432 | deny | refused |
| `prod/checkout` | own redis :6379 | allow | access |
| `prod/checkout` | analytics-redis :6379 | deny | reset |
| `prod/checkout` | analytics' pooler :5432 | deny | reset |
| `prod/checkout` | analytics :80 | deny | reset |
| `prod/checkout` | frontend :80 | deny | reset |
| `prod/analytics` | own pooler :5432 | allow | access |
| `prod/analytics` | **analytics-events-db-rw :5432 (bypassing the pooler)** | **deny** | **reset** |
| `prod/analytics` | checkout's pooler :5432 | deny | reset |
| `prod/analytics` | checkout-db-rw :5432 | deny | reset |
| `prod/analytics` | checkout :80 | deny | reset |
| `prod/analytics` (worker pod) | own pooler :5432 | allow | access |
| `prod/analytics` (worker pod) | checkout's pooler :5432 | deny | reset |
| no identity (loadgen) | checkout :80 | deny | reset |
| no identity (loadgen) | pooler :5432 | deny | reset |
| no identity (loadgen) | checkout-db-rw :5432 | deny | reset |
| no identity (loadgen) | frontend :80 | deny | reset |

The two rows in bold are the ones the design turns on: **Postgres cannot be
reached directly on 5432 by the application that owns it**, only by that
database's own PgBouncer. Without them the connection ceiling PgBouncer exists
to enforce could be walked around by any application, and nothing in any pool
metric would show it.

## What must still work, and does

Restrictions that break the legitimate paths are not restrictions, they are
outages. Checked in the same pass:

- Ingress path end to end: `200 200 200` through ingress-nginx.
- All 11 prod scrape targets up, including both poolers and both Postgres
  instances — every policy admits vmagent explicitly on the metrics port, or
  the pool-saturation panels would go dark exactly when the pool is the thing
  being investigated.
- A 15-minute `baseline` run at 80 rps finished its full duration: 71,833
  iterations at 79.77/s against a target of 80, with the policies in force.

## Reproducing

`probe.py` in this directory takes `host|port|kind|label|expect` arguments and
prints a pass/fail line per flow. Run it from inside a workload so it carries
that workload's identity:

```
pod=$(kubectl get pod -n prod -l app.kubernetes.io/name=checkout -o jsonpath='{.items[0].metadata.name}')
kubectl exec -i -n prod "$pod" -- python - \
  "checkout-db-rw.prod.svc.cluster.local|5432|pg|direct to Postgres|deny" < probe.py
```
