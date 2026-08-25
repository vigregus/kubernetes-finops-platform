import http from "k6/http";
import { sleep } from "k6";

const HOST = __ENV.K6_HOST || "analytics-stage.finops.local";
const TARGET = __ENV.K6_TARGET || "http://ingress-nginx-controller.ingress-nginx.svc.cluster.local";

export const options = {
  vus: 5,
  duration: "2m",
  thresholds: {
    // analytics is configured with ERROR_RATE=0.01 (see gitops/04-business-app/app2/values.yaml).
    http_req_failed: ["rate<0.05"],
    http_req_duration: ["p(95)<750"],
  },
};

export default function () {
  http.get(`${TARGET}/analytics`, { headers: { Host: HOST } });
  sleep(1);
}
