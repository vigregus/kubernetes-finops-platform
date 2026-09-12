import http from "k6/http";
import { sleep } from "k6";
import { Trend } from "k6/metrics";

// Load profiles for the stand, selected with PROFILE.
//
// Every scenario uses an *arrival-rate* executor, not VUs. The difference is
// the single most important thing about this file. A VU-based test is a closed
// model: each VU waits for its response before sending the next request, so
// when the system slows down the offered load falls with it. The system can
// never be pushed past its capacity, queueing stays invisible, and every
// latency number is really a measure of how politely the load generator waited.
//
// Arrival rate is an open model: requests are issued on a schedule regardless
// of whether the previous ones came back - which is what real traffic does.
// It is also what makes `dropped_iterations` meaningful: if k6 runs out of VUs
// to issue the scheduled arrivals, the system is failing to keep up.
const PROFILE = __ENV.PROFILE || "baseline";
const RPS = Number(__ENV.RPS_TARGET || 50);
const DURATION = __ENV.DURATION || "5m";

const HOST = __ENV.K6_HOST || "shop.finops.local";
const TARGET = __ENV.K6_TARGET || "http://ingress-nginx-controller.ingress-nginx.svc.cluster.local";

// Sized from Little's law: concurrency = arrival rate x response time. The
// ceiling is deliberately generous - hitting maxVUs would cap the offered
// load and silently turn this back into a closed model.
const preAllocatedVUs = Math.max(20, Math.ceil(RPS / 2));
const maxVUs = Math.max(100, RPS * 3);

const base = { executor: "constant-arrival-rate", timeUnit: "1s", preAllocatedVUs, maxVUs };

const PROFILES = {
  // Steady, modest load. The reference point every other profile is read
  // against.
  baseline: { ...base, rate: RPS, duration: DURATION },

  // A day's shape compressed: night trough, morning ramp, midday plateau,
  // evening peak, decline. What a scale-to-zero or predictive strategy has
  // to be judged against, since a flat load makes them all look alike.
  diurnal: {
    executor: "ramping-arrival-rate",
    timeUnit: "1s",
    startRate: Math.ceil(RPS * 0.1),
    preAllocatedVUs,
    maxVUs,
    stages: [
      { target: Math.ceil(RPS * 0.1), duration: "2m" },
      { target: Math.ceil(RPS * 0.6), duration: "4m" },
      { target: Math.ceil(RPS * 0.8), duration: "5m" },
      { target: RPS, duration: "4m" },
      { target: Math.ceil(RPS * 0.2), duration: "3m" },
    ],
  },

  // A step change, not a ramp. Reaction time and overshoot only become
  // visible when the load does not politely warn the autoscaler first.
  flash: {
    executor: "ramping-arrival-rate",
    timeUnit: "1s",
    startRate: Math.ceil(RPS * 0.1),
    preAllocatedVUs,
    maxVUs,
    stages: [
      { target: Math.ceil(RPS * 0.1), duration: "3m" },
      { target: RPS, duration: "10s" },
      { target: RPS, duration: "5m" },
      { target: Math.ceil(RPS * 0.1), duration: "10s" },
      { target: Math.ceil(RPS * 0.1), duration: "3m" },
    ],
  },

  // Long and flat: finds leaks, connection churn and cost accumulation that
  // a five-minute run cannot.
  soak: { ...base, rate: RPS, duration: __ENV.DURATION || "45m" },

  // Ramps until something gives. This is what measures real capacity, and
  // therefore what the rightsizing decisions should be argued from instead
  // of from whatever the demo happened to be doing.
  stress: {
    executor: "ramping-arrival-rate",
    timeUnit: "1s",
    startRate: Math.ceil(RPS * 0.1),
    preAllocatedVUs,
    maxVUs: Math.max(200, RPS * 6),
    stages: [
      { target: Math.ceil(RPS * 0.25), duration: "2m" },
      { target: Math.ceil(RPS * 0.5), duration: "2m" },
      { target: RPS, duration: "2m" },
      { target: RPS * 2, duration: "2m" },
      { target: RPS * 4, duration: "2m" },
    ],
  },
};

const selected = PROFILES[PROFILE];
if (!selected) {
  throw new Error(`unknown PROFILE '${PROFILE}' - expected one of ${Object.keys(PROFILES).join(", ")}`);
}

// Shed responses are not failures: 429 is the service correctly protecting
// itself, and counting it as an error would make a well-behaved service look
// broken exactly when it is doing the right thing. Tracked separately so a
// run can be read as "how much load was accepted" versus "how much was
// refused".
const shedRate = new Trend("shop_shed_ratio");

export const options = {
  scenarios: { [PROFILE]: selected },
  thresholds: {
    // checkout runs with ERROR_RATE=0.02 and /checkout/lookup deliberately
    // returns 404/400 for a share of requests, so these bounds describe the
    // intended behaviour rather than a regression-free ideal.
    http_req_failed: ["rate<0.35"],
    "http_req_duration{expected_response:true}": ["p(95)<500"],
    // If k6 cannot issue the scheduled arrivals, the measurement itself is
    // compromised and the run should not be read as a capacity result.
    dropped_iterations: ["count<100"],
  },
};

function randomUuid() {
  // Same shape as a real order id but never actually inserted - a
  // legitimate "valid input, no such row" 404, not an error.
  return "11111111-1111-4111-8111-11111111111" + Math.floor(Math.random() * 10);
}

const params = { headers: { Host: HOST }, tags: { journey: "shop" } };

export default function () {
  // A session, not an isolated request: a checkout followed by the reads a
  // real client would make around it. Weights keep reads dominant, which is
  // what makes the cache and the read path matter.
  let shed = 0;

  const placed = http.get(`${TARGET}/checkout`, params);
  if (placed.status === 429) shed++;

  const roll = Math.random();
  let lookup;
  if (roll < 0.7) {
    const slow = Math.random() < 0.2 ? "&slow=1" : "";
    lookup = http.get(
      `${TARGET}/checkout/lookup?id=00000000-0000-0000-0000-000000000001${slow}`,
      params
    );
  } else if (roll < 0.9) {
    lookup = http.get(`${TARGET}/checkout/lookup?id=${randomUuid()}`, params);
  } else {
    lookup = http.get(`${TARGET}/checkout/lookup?id=not-a-valid-uuid`, params);
  }
  if (lookup.status === 429) shed++;

  // A minority of sessions look at analytics, so its tier carries real but
  // lighter traffic than checkout - the asymmetry the cost-per-product view
  // is supposed to show.
  let requests = 2;
  if (Math.random() < 0.25) {
    const insight = http.get(`${TARGET}/analytics`, {
      headers: { Host: __ENV.K6_ANALYTICS_HOST || "analytics.finops.local" },
      tags: { journey: "shop" },
    });
    if (insight.status === 429) shed++;
    requests++;
  }

  shedRate.add(shed / requests);

  // Think time, not a throttle: the arrival-rate executor controls the load,
  // so this only shapes how long a session occupies a VU.
  sleep(Math.random() * 0.5);
}
