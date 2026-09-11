#!/usr/bin/env python3
"""Applies the metrics -> logs -> traces correlation UX (see
scripts/grafana_correlation/) to the dashboards listed in DASHBOARDS below.

This is the source of truth for which panels get which links now - adding
the pattern to a new dashboard means adding an entry here, not hand-copying
a links/transformations/overrides JSON blob into the manifest. Re-running
this script is idempotent: it fully replaces each target panel's
links/transformations/overrides rather than appending to them, so running
it twice (or after editing a panel's query) produces the same result.

Usage: python3 scripts/apply_dashboard_correlations.py [--check]
  --check  exit 1 if applying would change any file, without writing
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from grafana_correlation import add_correlation, dashboard_link, logs_link, traces_link
from grafana_correlation.dashboard_yaml import load, save

MANIFESTS = Path(__file__).parent.parent / "gitops/02-infra/observability-objects/dashboards/manifests"

# Each entry: (panel id, list of (old, new) expr substring replacements to
# fix aggregation grouping so the identity label survives, links to attach).
DASHBOARDS = {
    "application-overview.yaml": [
        (4, [("by (job, status)", "by (job, status, namespace)")], lambda: [logs_link("namespace", "job", "app")]),
        (5, [("by (job)", "by (job, namespace)")], lambda: [logs_link("namespace", "job", "app"), traces_link("job")]),
        (7, [("by (job, le)", "by (job, namespace, le)")], lambda: [logs_link("namespace", "job", "app"), traces_link("job")]),
        (9, [], lambda: [logs_link("namespace", "pod", "pod")]),
        (10, [], lambda: [logs_link("namespace", "pod", "pod")]),
    ],
    "ingress-nginx.yaml": [
        (86, [("by (ingress)", "by (ingress, exported_namespace, cluster)")], lambda: [logs_link("exported_namespace", "exported_service", "app")]),
        (87, [("by (ingress)", "by (ingress, exported_namespace, cluster)")], lambda: [logs_link("exported_namespace", "exported_service", "app"), traces_link("exported_service")]),
    ],
    "postgres.yaml": [
        (273, [("by (pod)", "by (pod, namespace)")], lambda: [logs_link("namespace", "pod", "pod")]),
        (275, [("by (pod)", "by (pod, namespace)")], lambda: [logs_link("namespace", "pod", "pod")]),
        (50, [("by (pod)", "by (pod, namespace)")], lambda: [logs_link("namespace", "pod", "pod")]),
        (4, [("by (pod)", "by (pod, namespace)")], lambda: [logs_link("namespace", "pod", "pod")]),
        (55, [], lambda: [logs_link("namespace", "pod", "pod")]),
        (54, [], lambda: [logs_link("namespace", "pod", "pod")]),
    ],
    "cost-story.yaml": [
        (12, [], lambda: [dashboard_link("application-overview", title="Open Application Overview (+/-5m)")]),
    ],
}


def main() -> int:
    check = "--check" in sys.argv
    any_changed = False
    for filename, panel_specs in DASHBOARDS.items():
        path = MANIFESTS / filename
        data, lines, start, end, indent = load(str(path))
        panels_by_id = {p["id"]: p for p in data["panels"] if "id" in p}
        for panel_id, replacements, make_links in panel_specs:
            panel = panels_by_id[panel_id]
            for target in panel.get("targets", []):
                if "expr" not in target:
                    continue
                for old, new in replacements:
                    target["expr"] = target["expr"].replace(old, new)
            add_correlation(panel, make_links())

        original = path.read_text()
        save(str(path), data, lines, start, end, indent)
        new_content = path.read_text()
        if new_content != original:
            any_changed = True
            print(f"{'would change' if check else 'updated'}: {filename}")
            if check:
                path.write_text(original)
        else:
            print(f"unchanged: {filename}")

    if check and any_changed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
