"""Builds the metrics -> logs -> traces correlation UX (see docs/ or the
Application Overview dashboard for the original hand-built version) so it
stops being copy-pasted Grafana JSON across dashboards.

Grafana has no native way to open Explore on a +/-N window around a clicked
point on a Prometheus panel (Correlations inherits the dashboard's own time
range; the Tempo "span time shift" fields are Tempo-datasource-specific).
The workaround, used throughout: a `calculateField` transform adds
window_start/window_end fields (Time +/- offset_ms) to the panel's own data,
hidden from the chart, and a data link reads them back via
`${__data.fields.window_start}`.
"""
from __future__ import annotations

import json
import re
import urllib.parse

WINDOW_MS = 300_000  # +/-5m, matches every dashboard built with this so far


def window_transform(offset_ms: int = WINDOW_MS) -> list[dict]:
    """calculateField transforms that add window_start/window_end fields."""
    return [
        {
            "id": "calculateField",
            "options": {
                "mode": "binary",
                "binary": {"left": "Time", "operator": "-", "right": str(offset_ms)},
                "alias": "window_start",
                "replaceFields": False,
            },
        },
        {
            "id": "calculateField",
            "options": {
                "mode": "binary",
                "binary": {"left": "Time", "operator": "+", "right": str(offset_ms)},
                "alias": "window_end",
                "replaceFields": False,
            },
        },
    ]


def hide_helper_field_overrides(existing: list[dict] | None = None) -> list[dict]:
    """Hides window_start/window_end from the chart/legend/tooltip and strips
    the panel's own unit (percent, bytes, ...) off them - without this they
    either render as extra plotted series with huge epoch values, or the
    data link interpolates a formatted string like "1789140020000%" instead
    of a plain epoch-ms number and Explore silently falls back to its
    default range."""

    def one(name: str) -> dict:
        return {
            "matcher": {"id": "byName", "options": name},
            "properties": [
                {"id": "custom.hideFrom", "value": {"viz": True, "legend": True, "tooltip": True}},
                {"id": "unit", "value": "none"},
                {"id": "decimals", "value": 0},
            ],
        }

    return [one("window_start"), one("window_end")] + (existing or [])


_VAR_RE = re.compile(r"\$\{[^}]+\}")


def _explore_url(datasource: str, query: dict) -> str:
    payload = {
        "e1w": {
            "datasource": datasource,
            "queries": [query],
            "range": {
                "from": "${__data.fields.window_start}",
                "to": "${__data.fields.window_end}",
            },
        }
    }
    raw = json.dumps(payload, separators=(",", ":"))
    # Grafana finds `${__field.labels.x}`-style variables by scanning the
    # raw link URL for literal "${...}" text and substituting the result in
    # place - percent-encoding them along with everything else here would
    # hide them from that scan and Grafana would treat the escaped sequence
    # as opaque text, silently falling back to its default time range.
    tokens: list[str] = []

    def stash(m: re.Match) -> str:
        tokens.append(m.group(0))
        return f"ZZTOKENZZ{len(tokens) - 1}ZZ"

    protected = _VAR_RE.sub(stash, raw)
    encoded = urllib.parse.quote(protected, safe="")
    for i, token in enumerate(tokens):
        encoded = encoded.replace(f"ZZTOKENZZ{i}ZZ", token)
    return f"/explore?schemaVersion=1&panes={encoded}&orgId=1"


def logs_link(namespace_label: str, identity_label: str, identity_field: str = "app", title: str = "View logs (+/-5m)") -> dict:
    """A data link to VictoriaLogs Explore, windowed +/-5m around the clicked
    point. `identity_field` is the VictoriaLogs stream field to match the
    panel's identity label against - "app" for job/service-level panels,
    "pod" for panels that already group by pod (CNPG, USE panels)."""
    ns = "${__field.labels." + namespace_label + "}"
    ident = "${__field.labels." + identity_label + "}"
    if identity_field == "pod":
        expr = f'{{namespace="{ns}",pod=~"{ident}"}}'
    else:
        expr = f'{{namespace="{ns}"}} {identity_field}:~"{ident}"'
    url = _explore_url("victorialogs", {"refId": "A", "expr": expr})
    return {"title": title, "url": url, "targetBlank": True}


def traces_link(service_label: str, title: str = "View traces (+/-5m)") -> dict:
    """A data link to Tempo Explore (TraceQL by resource.service.name),
    windowed the same way as logs_link. Only add this to panels whose
    workload is actually instrumented with OpenTelemetry - CloudNativePG and
    nginx-ingress itself aren't, so they only ever get logs_link."""
    svc = "${__field.labels." + service_label + "}"
    query = f'{{resource.service.name="{svc}"}}'
    url = _explore_url("tempo", {"refId": "A", "queryType": "traceql", "query": query})
    return {"title": title, "url": url, "targetBlank": True}


def dashboard_link(uid: str, var_namespace: str = "prod", title: str = "Open dashboard (+/-5m)") -> dict:
    """A data link to another dashboard, windowed the same way, for panels
    whose result has no clean per-workload identity to link straight to logs
    with (see Cost Story's 'Cost per 1M requests' panel - the cost formula's
    joins collapse everything down to a 'product' label)."""
    url = (
        f"/d/{uid}/{uid}?orgId=1&var-namespace={var_namespace}"
        "&from=${__data.fields.window_start}&to=${__data.fields.window_end}"
    )
    return {"title": title, "url": url, "targetBlank": True}


def add_correlation(panel: dict, links: list[dict]) -> None:
    """Wires window_transform + hide_helper_field_overrides + the given
    links into a panel in place. Idempotent - safe to re-run against an
    already-wired panel."""
    field_config = panel.setdefault("fieldConfig", {})
    defaults = field_config.setdefault("defaults", {})
    defaults["links"] = links
    existing_overrides = [
        o
        for o in field_config.get("overrides", [])
        if o.get("matcher", {}).get("options") not in ("window_start", "window_end")
    ]
    field_config["overrides"] = hide_helper_field_overrides(existing_overrides)
    panel["transformations"] = window_transform()
