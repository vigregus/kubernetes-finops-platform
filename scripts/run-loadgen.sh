#!/usr/bin/env bash
# Launch one loadgen run from the CronJob template with explicit overrides.
#
# `kubectl create job --from=cronjob/loadgen` cannot be used on its own here:
# a Job's pod template is immutable once created, so the profile and rate
# cannot be patched afterwards - they have to be substituted before the object
# is submitted. Hence the render-then-apply below.
#
# Every knob that shapes the result is an argument rather than a default, so
# two runs being compared cannot silently differ: an A/B where the B arm ran at
# a different rate is not a measurement of the change under test.
#
# Usage:
#   scripts/run-loadgen.sh <run-name> <profile> <rps-target> <duration>
# Example:
#   scripts/run-loadgen.sh stress-before stress 150 5m
set -euo pipefail

RUN_NAME="${1:?run name, e.g. stress-before}"
PROFILE="${2:?profile: baseline|diurnal|flash|soak|stress}"
RPS_TARGET="${3:?peak arrival rate for the profile}"
DURATION="${4:?duration, e.g. 5m (ignored by ramping profiles with fixed stages)}"
NAMESPACE="${NAMESPACE:-loadgen}"

# The transform lives in a temp file rather than a `python3 - <<'PY'` heredoc:
# with the CronJob piped in on stdin, the heredoc and the pipe fight over that
# same stdin, and python reads the script while the JSON is discarded.
TRANSFORM="$(mktemp -t loadgen-render).py"
trap 'rm -f "$TRANSFORM"' EXIT
cat >"$TRANSFORM" <<'PY'
import json, sys

run_name, profile, rps, duration = sys.argv[1:5]
cron = json.load(sys.stdin)
spec = cron["spec"]["jobTemplate"]["spec"]

overrides = {"PROFILE": profile, "RPS_TARGET": rps, "DURATION": duration}
for container in spec["template"]["spec"]["containers"]:
    for env in container.get("env", []):
        if env["name"] in overrides:
            # valueFrom and value are mutually exclusive; these are all plain
            # values, but drop it defensively rather than submit both.
            env.pop("valueFrom", None)
            env["value"] = overrides[env["name"]]

labels = spec["template"]["metadata"].setdefault("labels", {})
# The run name travels on the pod as well as the Job so a series can be
# pulled back out of logs and metrics after the fact, when the Job object
# itself has long since been cleaned up by ttlSecondsAfterFinished.
labels["finops.internal/run"] = run_name

job = {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {
        "name": run_name,
        "namespace": cron["metadata"]["namespace"],
        "labels": dict(cron["metadata"].get("labels", {}), **{"finops.internal/run": run_name}),
    },
    "spec": spec,
}
print(json.dumps(job))
PY

kubectl delete job "$RUN_NAME" -n "$NAMESPACE" --ignore-not-found >/dev/null
kubectl get cronjob loadgen -n "$NAMESPACE" -o json \
  | python3 "$TRANSFORM" "$RUN_NAME" "$PROFILE" "$RPS_TARGET" "$DURATION" \
  | kubectl apply -f - >/dev/null
echo "started ${RUN_NAME}: profile=${PROFILE} rps=${RPS_TARGET} duration=${DURATION}"
