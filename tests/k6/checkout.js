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
    // checkout is configured with ERROR_RATE=0.02 (see gitops/04-business-app/app1/values.yaml) -
    // a <0.01 threshold would always fail by design, not by regression.
    http_req_failed: ["rate<0.05"],
    http_req_duration: ["p(95)<500"],
  },
};

export default function () {
  http.get(`${TARGET}/checkout`, { headers: { Host: HOST } });
  sleep(1);
}
