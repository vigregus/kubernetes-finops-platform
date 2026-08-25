import http from "k6/http";
import { sleep } from "k6";

// Through ingress-nginx, not the Service directly, so the load also
// exercises the real entry point (and its OpenTelemetry trace origin) -
// see gitops/02-infra/ingress-nginx.
const HOST = __ENV.K6_HOST || "checkout.finops.local";
const TARGET = __ENV.K6_TARGET || "http://ingress-nginx-controller.ingress-nginx.svc.cluster.local";

export const options = {
  vus: 10,
  duration: "2m",
  thresholds: {
    // checkout is configured with ERROR_RATE=0.02 (see gitops/04-business-app/app1/values.yaml)
    // and /checkout/lookup deliberately 404s/400s a fraction of the time
    // (see apps/checkout/app.py checkout_lookup) - a <0.01 threshold
    // would always fail by design, not by regression.
    http_req_failed: ["rate<0.30"],
    http_req_duration: ["p(95)<500"],
  },
};

function randomUuid() {
  // Same shape as a real order id but never actually inserted - a
  // legitimate "valid input, no such row" 404, not an error.
  return "11111111-1111-4111-8111-11111111111" + Math.floor(Math.random() * 10);
}

export default function () {
  http.get(`${TARGET}/checkout`, { headers: { Host: HOST } });

  // Mixed read traffic against the same order table persist_order writes
  // to: mostly the real seed order (200 with an actual row), some
  // syntactically valid ids that were never written (404, not an error),
  // and some genuinely malformed input that Postgres itself rejects
  // (400, a real driver-level exception) - see apps/checkout/app.py
  // checkout_lookup/lookup_order for what each path actually does.
  const roll = Math.random();
  if (roll < 0.7) {
    const slow = Math.random() < 0.2 ? "&slow=1" : "";
    http.get(`${TARGET}/checkout/lookup?id=00000000-0000-0000-0000-000000000001${slow}`, {
      headers: { Host: HOST },
    });
  } else if (roll < 0.9) {
    http.get(`${TARGET}/checkout/lookup?id=${randomUuid()}`, { headers: { Host: HOST } });
  } else {
    http.get(`${TARGET}/checkout/lookup?id=not-a-valid-uuid`, { headers: { Host: HOST } });
  }

  sleep(1);
}
