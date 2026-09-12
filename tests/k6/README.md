# k6 scenarios

The load scripts now live in `charts/loadgen/files/` rather than here, because
Helm can only embed files from inside its own chart directory and the chart is
how load actually runs in the cluster. This directory keeps the notes.

## Why arrival rate rather than VUs

The previous scripts used `vus: 10` with a `sleep(1)`, which is a *closed*
model: each virtual user waits for its response before sending the next
request. When the system slows down, the offered load slows down with it. The
system can never be pushed past capacity, queueing never becomes visible, and
the latency figures describe how patiently the generator waited.

`charts/loadgen/files/shop.js` uses arrival-rate executors instead - an *open*
model, where requests are issued on schedule whether or not the previous ones
came back. That is what production traffic does, and it is what makes
`dropped_iterations` meaningful: if k6 cannot issue the scheduled arrivals,
the run is not a valid capacity measurement.

## Profiles

Selected with `PROFILE`, scaled with `RPS_TARGET`:

| profile    | shape                                        | answers |
|------------|----------------------------------------------|---------|
| `baseline` | steady rate                                  | the reference every other run is read against |
| `diurnal`  | night trough, ramp, plateau, peak, decline   | whether a scaling strategy tracks demand or just averages it |
| `flash`    | 10x step change, held, then dropped          | reaction time and overshoot |
| `soak`     | long and flat                                | leaks, connection churn, accumulated cost |
| `stress`   | ramps until the SLO breaks                   | real capacity, which is what rightsizing should argue from |

## Running one

Runs are created from a suspended CronJob rather than scheduled, so Argo CD
never launches load on its own - a load test that re-runs on every sync is
not an experiment, it is noise in every cost measurement taken afterwards.

```
kubectl create job -n loadgen --from=cronjob/loadgen loadgen-$(date +%s)
```

Override the profile for a single run by editing that Job, or change the
defaults in `gitops/04-business-app/loadgen/values.yaml`.

Client-side metrics are remote-written to VictoriaMetrics, so k6's own p95
sits next to the server's. The gap between them is the queueing the server
cannot see, and it is the honest SLO number.

## Locally, without the cluster

```
docker run --rm -v "$PWD/charts/loadgen/files:/scripts:ro" \
  -e PROFILE=flash -e RPS_TARGET=200 \
  grafana/k6:0.54.0 inspect --include-system-env-vars /scripts/shop.js
```

`inspect` does not pass system environment variables unless told to, so
without that flag every profile silently resolves to `baseline` at its
default rate.
