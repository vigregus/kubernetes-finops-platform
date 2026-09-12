# Run baselines

VictoriaMetrics native export, taken before the cluster was recreated with
Cilium. Re-importable into a fresh instance:

    curl -X POST http://localhost:8428/api/v1/import \
      --data-binary @runs-2026-09-12.jsonl

`runs-2026-09-12.jsonl` covers the four stress runs of 2026-09-12, which are
the reference every later comparison is read against:

| run | peak served | 429 total | of those PoolBusy | 503 | note |
|-----|-------------|-----------|-------------------|-----|------|
| 1 | ~231 rps | 3305 | - | 14858 | readiness probe competed for the DB pool; throughput collapsed to 9 rps |
| 2 | invalid | - | - | 1031 | duplicate-key violations opened the breaker; deterministic ids with a plain INSERT |
| 3 | 168 rps | 9553 | 5319 | 0 | idempotency fixed; pool was still the binding constraint |
| 4 | 288 rps | 1125 | 0 | 0 | PgBouncer + larger client pool; refusal moved entirely to admission |

Run 4 is the baseline to beat. Its p95 over served requests was 494.6ms,
under the 500ms objective - but note the latency histogram only gained its
`status` label at run 4, so p95 figures are not comparable with earlier runs.
