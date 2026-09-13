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
const TARGET = __ENV.K6_TARGET || "https://shop.finops.local";

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

// Name-to-address overrides, the equivalent of `curl --resolve`.
//
// Needed to drive the Cilium gateway at all. It terminates TLS and selects its
// filter chain by SNI, and SNI comes from the URL's hostname - a Host header
// does not set it. Pointing k6 at the node's address with a Host header gets a
// connection reset, because no chain matches an empty server name. This maps
// the real hostname onto the node so the URL stays https://shop.finops.local
// and the SNI is right.
//
// Format: "shop.finops.local:192.168.49.2,analytics.finops.local:192.168.49.2"
const RESOLVE = __ENV.K6_RESOLVE || "";
const hostMap = {};
RESOLVE.split(",").filter(Boolean).forEach((pair) => {
  const idx = pair.lastIndexOf(":");
  if (idx > 0) hostMap[pair.slice(0, idx)] = pair.slice(idx + 1);
});

export const options = {
  scenarios: { [PROFILE]: selected },
  hosts: hostMap,
  // The gateway's certificate is issued by the cluster's own CA, which no
  // client here trusts. Verifying it would measure our willingness to
  // distribute a CA bundle to the load generator, not the gateway.
  insecureSkipTLSVerify: true,
  // `url` is a default system tag and carries the raw request URL, so tagging
  // each request with a bounded `name` is not on its own enough - the URL would
  // keep minting a series per order id regardless. This is the explicit list
  // minus `url`; everything kept here has bounded cardinality, and dropping the
  // tag costs nothing because `name` already says which endpoint was called.
  systemTags: [
    "proto",
    "method",
    "status",
    "name",
    "scenario",
    "group",
    "check",
    "error_code",
    "expected_response",
  ],
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

// The order id is a pure function of the idempotency key (see order_id_for in
// apps/checkout/app.py), so the generator can read back anything it wrote
// without a discovery step.
function orderId(i) {
  return "00000000-0000-4000-8000-" + String(i).padStart(12, "0");
}

// How many distinct orders the run circulates through. This is the number
// that decides whether caching means anything: with a key space of one - the
// previous profile read a single seed id for 70% of its requests - hit ratio
// is ~100% by construction, eviction never happens, and every cache setting
// measures the same.
const KEY_SPACE = Number(__ENV.KEY_SPACE || 50000);
// Real read traffic is skewed, not uniform: a small set of items takes most
// of the requests and a long tail takes the rest. A uniform draw over a large
// key space would instead produce a ~0% hit ratio, which is just as
// unrepresentative as a 100% one. Higher skew concentrates harder.
const SKEW = Number(__ENV.ACCESS_SKEW || 3);

function hotIndex() {
  return Math.floor(KEY_SPACE * Math.pow(Math.random(), SKEW));
}

const params = { headers: { Host: HOST }, tags: { journey: "shop" } };

// Every request below carries an explicit `name` tag, and none of them may be
// left to default.
//
// k6 tags each request with its URL as `name` unless told otherwise. The URLs
// here embed an order id and a key index drawn from a 50k key space, so a run
// mints a fresh time series per distinct URL: a stress run generated 400,103
// unique series against a suggested ceiling of 100,000, spent its memory limit
// holding them, and was OOMKilled at minute seven of ten. That looked exactly
// like the system collapsing - throughput halved and p95 doubled in the final
// sample - when what actually died was the instrument.
//
// The names below are the endpoint shapes, which is the cardinality that
// carries information: `lookup-hit` and `lookup-miss` hit the same handler but
// exercise opposite cache paths, so they stay separate on purpose.
const asName = (name) => Object.assign({}, params, { tags: { journey: "shop", name } });

export default function () {
  // A session, not an isolated request: a checkout followed by the reads a
  // real client would make around it. Weights keep reads dominant, which is
  // what makes the cache and the read path matter.
  let shed = 0;

  // Write into the same skewed key space the reads use, so the hot set is
  // genuinely present in the database and the cold tail genuinely is not.
  const writeIdx = hotIndex();
  const placed = http.get(`${TARGET}/checkout?key=${writeIdx}`, asName("checkout"));
  if (placed.status === 429) shed++;

  const roll = Math.random();
  let lookup;
  if (roll < 0.7) {
    // Existing orders, skewed towards the hot set - the traffic a cache is
    // supposed to absorb.
    const slow = Math.random() < 0.05 ? "&slow=1" : "";
    lookup = http.get(`${TARGET}/checkout/lookup?id=${orderId(hotIndex())}${slow}`, asName("lookup-hit"));
  } else if (roll < 0.9) {
    // Well-formed ids beyond the populated space: real 404s, and the traffic
    // that negative caching exists to absorb.
    lookup = http.get(
      `${TARGET}/checkout/lookup?id=${orderId(KEY_SPACE + Math.floor(Math.random() * KEY_SPACE))}`,
      asName("lookup-miss")
    );
  } else {
    lookup = http.get(`${TARGET}/checkout/lookup?id=not-a-valid-uuid`, asName("lookup-invalid"));
  }
  if (lookup.status === 429) shed++;

  // A minority of sessions look at analytics, so its tier carries real but
  // lighter traffic than checkout - the asymmetry the cost-per-product view
  // is supposed to show.
  let requests = 2;
  if (Math.random() < 0.25) {
    const insight = http.get(`${TARGET}/analytics`, {
      headers: { Host: __ENV.K6_ANALYTICS_HOST || "analytics.finops.local" },
      tags: { journey: "shop", name: "analytics" },
    });
    if (insight.status === 429) shed++;
    requests++;
  }

  shedRate.add(shed / requests);

  // Think time, not a throttle: the arrival-rate executor controls the load,
  // so this only shapes how long a session occupies a VU.
  sleep(Math.random() * 0.5);
}
