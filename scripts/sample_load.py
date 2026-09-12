#!/usr/bin/env python3
"""Sample the server side of a load run from VictoriaMetrics.

k6 reports what the client saw. This reports what the cluster did - throughput
split by status, latency, CPU against the limit, cache behaviour and the
database query rate the cache is supposed to be suppressing. Reading the two
together is the point: the gap between client-side and server-side latency is
the queueing the server never sees.

Resolution ceiling: application metrics are scraped every 15s (see the
VMServiceScrape in charts/app*/templates/deployment.yaml) and vmagent's global
interval is 20s. Sampling faster than that returns the same value repeated,
which reads as detail but is not - so this refuses to pretend, and says so.
Genuinely finer resolution means lowering the scrape interval, which costs
proportionally more samples stored and more vmagent CPU.

Needs a port-forward to VictoriaMetrics:

    kubectl port-forward -n observability svc/vmsingle-vm-local-victoria-metrics-k8s-stack 8428:8428

Usage:
    python3 scripts/sample_load.py --duration 660 --interval 15
    python3 scripts/sample_load.py --namespace prod --job checkout --csv run.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
import urllib.parse
import urllib.request

SCRAPE_INTERVAL_S = 15


def query(vm: str, expr: str, timeout: float = 5.0) -> float | None:
    """Run one instant query.

    Returns the value, 0.0 for "queried fine, no series matched", or None for
    "could not ask". Those last two must not collapse into each other: an
    earlier run reported a full screen of 0.00 that read as a system serving
    nothing, when in fact the port-forward had died mid-run and the system was
    serving ~288 rps. A dead sampler that reports zeros is worse than one that
    crashes, because its output is publishable.
    """
    url = f"{vm}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            import json

            payload = json.load(resp)
    except Exception:
        return None
    if payload.get("status") != "success":
        return None
    result = payload.get("data", {}).get("result", [])
    if not result:
        return 0.0
    try:
        return float(result[0]["value"][1])
    except (KeyError, IndexError, ValueError):
        return None


def build_queries(namespace: str, job: str, window: str) -> dict[str, str]:
    ns = f'namespace="{namespace}"'
    jb = f'job="{job}"'
    return {
        "ok/s": f'sum(rate(http_requests_total{{{ns},{jb},status="200"}}[{window}]))',
        "429/s": f'sum(rate(http_requests_total{{{ns},{jb},status="429"}}[{window}]))',
        "503/s": f'sum(rate(http_requests_total{{{ns},{jb},status="503"}}[{window}]))',
        "500/s": f'sum(rate(http_requests_total{{{ns},{jb},status="500"}}[{window}]))',
        "p95ms": (
            f"1000*histogram_quantile(0.95, sum by (le) "
            f"(rate(http_request_duration_seconds_bucket{{{ns},{jb}}}[{window}])))"
        ),
        # The pod regex excludes the redis sidecar deployment, whose pods also
        # start with the service name.
        "cpu": (
            f'sum(rate(container_cpu_usage_seconds_total{{{ns},pod=~"{job}-[^r].*",'
            f'metrics_path="/metrics/resource"}}[{window}]))'
        ),
        "pods": f'count(up{{{ns},{jb}}} == 1)',
        # Cached absences count as hits: answering "no such row" from Redis is
        # exactly what negative caching is for.
        "hit%": (
            f'100*sum(rate(cache_requests_total{{{ns},result=~"hit|negative"}}[{window}]))'
            f'/clamp_min(sum(rate(cache_requests_total{{{ns}}}[{window}])),0.001)'
        ),
        "dbq/s": f'sum(rate(db_query_duration_seconds_count{{{ns},{jb}}}[{window}]))',
        "shed": f'sum(http_requests_shed_total{{{ns},{jb}}})',
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vm", default="http://localhost:8428", help="VictoriaMetrics base URL")
    ap.add_argument("--namespace", default="prod")
    ap.add_argument("--job", default="checkout")
    ap.add_argument("--interval", type=int, default=SCRAPE_INTERVAL_S, help="seconds between samples")
    ap.add_argument("--duration", type=int, default=660, help="total seconds to sample")
    ap.add_argument("--window", default="30s", help="rate() window")
    ap.add_argument("--csv", help="also write samples to this CSV file")
    args = ap.parse_args()

    if args.interval < SCRAPE_INTERVAL_S:
        print(
            f"note: sampling every {args.interval}s but metrics are scraped every "
            f"{SCRAPE_INTERVAL_S}s - the extra samples repeat the same value rather "
            f"than adding detail",
            file=sys.stderr,
        )

    queries = build_queries(args.namespace, args.job, args.window)
    columns = list(queries)

    header = f"{'elapsed':>8} | " + " | ".join(f"{c:>7}" for c in columns)
    print(header)
    print("-" * len(header))

    rows = []
    started = time.monotonic()
    deadline = started + args.duration
    # A sample where nothing could be asked is not a sample. Tolerate a couple
    # in a row - VM restarts, a scrape gap - then stop, because past that the
    # run is no longer being observed and continuing only produces a longer
    # file of things that were never measured.
    consecutive_failures = 0
    while time.monotonic() < deadline:
        elapsed = int(time.monotonic() - started)
        values = {c: query(args.vm, q) for c, q in queries.items()}
        rows.append({"elapsed": elapsed, **values})
        cells = " | ".join(
            f"{'   err':>7}" if values[c] is None else f"{values[c]:>7.2f}" for c in columns
        )
        print(f"{elapsed:>7}s | " + cells, flush=True)

        if all(values[c] is None for c in columns):
            consecutive_failures += 1
            if consecutive_failures >= 3:
                print(
                    f"\naborting: {consecutive_failures} consecutive samples could not reach "
                    f"{args.vm}. Check the port-forward - the run itself may well be fine, "
                    f"but nothing from here on would be measured.",
                    file=sys.stderr,
                )
                break
        else:
            consecutive_failures = 0
        time.sleep(args.interval)

    if args.csv and rows:
        with open(args.csv, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["elapsed"] + columns)
            writer.writeheader()
            # None lands as an empty cell, which every reader treats as
            # missing - distinct from a 0 that was actually measured.
            writer.writerows(rows)
        print(f"\nwrote {len(rows)} samples to {args.csv}", file=sys.stderr)

    # Non-zero when the run ended blind, so a wrapper script cannot mistake an
    # unobserved run for a completed one.
    return 1 if consecutive_failures >= 3 else 0


if __name__ == "__main__":
    raise SystemExit(main())
