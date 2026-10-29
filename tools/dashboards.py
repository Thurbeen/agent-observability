# /// script
# requires-python = ">=3.12"
# ///
"""Generate the Grafana dashboards in dashboards/ from this file.

    uv run tools/dashboards.py           # fail if dashboards/ is stale
    uv run tools/dashboards.py --write   # regenerate dashboards/

Edit this file, not the JSON. Every dashboard picks its data sources through
template variables, so it imports into any Grafana unchanged.
"""

import argparse
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "dashboards"

# Fleet exports metrics every 60 s by default (OTEL_METRIC_EXPORT_INTERVAL).
# A rate needs two points, so no Prometheus panel looks at less than 2m.
MIN_INTERVAL = "2m"
JOB = 'job=~"$job"'
PROM = {"type": "prometheus", "uid": "${prometheus}"}
TEMPO = {"type": "tempo", "uid": "${tempo}"}

# The status colours. Every other series takes Grafana's palette.
BAD, WARN, GOOD = "red", "orange", "green"
OUTCOME_COLORS = {
    "error": BAD, "failed": BAD, "unknown": WARN, "leaseLost": WARN, "conflict": WARN,
    "throttled": WARN, "notLoggedIn": BAD, "refused": BAD, "timeout": WARN,
    "unavailable": WARN, "unreadable": WARN, "incomplete": WARN,
}


def sel(*matchers: str) -> str:
    return "{" + ",".join((JOB,) + matchers) + "}"


def rate(metric: str, *matchers: str, by: str = "") -> str:
    group = f" by ({by})" if by else ""
    return f"sum{group} (rate({metric}{sel(*matchers)}[$__rate_interval]))"


def over_range(metric: str, *matchers: str, by: str = "") -> str:
    """Increase over the dashboard range, counting a series born inside the
    range from zero: `increase()` never counts a series' first sample, and
    Fleet's rare outcomes start a new series at 1."""
    s = f"{metric}{sel(*matchers)}"
    group = f" by ({by})" if by else ""
    return f"sum{group} (((({s}) * 1) unless ({s} offset $__range)) or increase({s}[$__range]))"


def mean_duration(*matchers: str, by: str) -> str:
    m = "fleet_reconcile_duration_seconds"
    return f"{rate(m + '_sum', *matchers, by=by)} / {rate(m + '_count', *matchers, by=by)}"


class Board:
    def __init__(self) -> None:
        self.panels: list[dict] = []
        self.y = 0
        self.x = 0
        self.row_h = 0

    def _place(self, w: int, h: int) -> dict:
        if self.x + w > 24:
            self.x, self.y = 0, self.y + self.row_h
            self.row_h = 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.row_h = max(self.row_h, h)
        return pos

    def row(self, title: str) -> None:
        if self.x:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        self.panels.append(
            {"type": "row", "title": title, "collapsed": False, "panels": [], "gridPos": self._place(24, 1)}
        )
        self.x, self.y, self.row_h = 0, self.y + 1, 0

    def add(self, panel: dict, w: int, h: int) -> None:
        panel["gridPos"] = self._place(w, h)
        self.panels.append(panel)


def target(expr: str, legend: str = "", instant: bool = False, ref: str = "A") -> dict:
    t = {"refId": ref, "datasource": PROM, "expr": expr, "legendFormat": legend or "__auto"}
    if instant:
        t.update(instant=True, range=False, format="table")
    return t


def overrides_by_outcome() -> list[dict]:
    return [
        {
            "matcher": {"id": "byRegexp", "options": f"(^|.* ){outcome}( .*|$)"},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}],
        }
        for outcome, color in OUTCOME_COLORS.items()
    ]


def stat(title: str, expr: str, desc: str, unit: str = "short", bad_above: float | None = None) -> dict:
    steps = [{"color": GOOD if bad_above is not None else "text", "value": None}]
    if bad_above is not None:
        steps.append({"color": BAD, "value": bad_above})
    return {
        "type": "stat",
        "title": title,
        "description": desc,
        "datasource": PROM,
        "interval": MIN_INTERVAL,
        "targets": [target(expr, instant=True)],
        "fieldConfig": {
            "defaults": {"unit": unit, "noValue": "0", "thresholds": {"mode": "absolute", "steps": steps}},
            "overrides": [],
        },
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "none"},
    }


def series(title: str, targets: list[dict], desc: str, unit: str = "short", bars: bool = False,
           stack: bool = False) -> dict:
    custom = {"drawStyle": "bars" if bars else "line", "fillOpacity": 60 if bars else 10, "lineWidth": 1,
              "showPoints": "never", "spanNulls": True}
    if stack:
        custom["stacking"] = {"mode": "normal", "group": "A"}
    return {
        "type": "timeseries",
        "title": title,
        "description": desc,
        "datasource": PROM,
        "interval": MIN_INTERVAL,
        "targets": targets,
        "fieldConfig": {"defaults": {"unit": unit, "min": 0, "custom": custom}, "overrides": overrides_by_outcome()},
        "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["mean", "max"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def table(title: str, expr: str, desc: str, labels: list[str]) -> dict:
    return {
        "type": "table",
        "title": title,
        "description": desc,
        "datasource": PROM,
        "interval": MIN_INTERVAL,
        "targets": [target(expr, instant=True)],
        "transformations": [
            {"id": "organize", "options": {
                "excludeByName": {"Time": True},
                "indexByName": {**{lab: i for i, lab in enumerate(labels)}, "Value": len(labels)},
                "renameByName": {"Value": "count"},
            }},
        ],
        "fieldConfig": {"defaults": {"decimals": 0}, "overrides": []},
        "options": {"sortBy": [{"displayName": "count", "desc": True}]},
    }


def text(title: str, content: str) -> dict:
    return {"type": "text", "title": title, "options": {"mode": "markdown", "content": content}}


def datasource_var(name: str, kind: str, label: str) -> dict:
    return {"name": name, "label": label, "type": "datasource", "query": kind, "hide": 0,
            "current": {}, "options": [], "refresh": 1, "regex": "", "skipUrlSync": False}


def query_var(name: str, label: str, query: str, desc: str) -> dict:
    return {"name": name, "label": label, "description": desc, "type": "query", "datasource": PROM,
            "query": {"query": query, "refId": "PrometheusVariableQueryEditor-VariableQuery"},
            "definition": query, "refresh": 2, "includeAll": True, "multi": True, "allValue": ".*",
            "current": {"text": "All", "value": "$__all"}, "options": [], "regex": "", "sort": 1, "hide": 0}


def textbox_var(name: str, label: str, default: str, desc: str) -> dict:
    return {"name": name, "label": label, "description": desc, "type": "textbox", "query": default,
            "current": {"text": default, "value": default}, "options": [], "hide": 0}


JOB_VAR = query_var(
    "job", "Fleet", "label_values(fleet_reconcile_duration_seconds_count, job)",
    "The Prometheus job of a Fleet manager: service.namespace/service.name, or the scrape job.",
)


def dashboard(uid: str, title: str, desc: str, board: Board, variables: list[dict]) -> dict:
    return {
        "uid": uid,
        "title": title,
        "description": desc,
        "tags": ["fleet"],
        "editable": True,
        "graphTooltip": 1,
        "time": {"from": "now-6h", "to": "now"},
        "refresh": "1m",
        "schemaVersion": 41,
        "templating": {"list": variables},
        "annotations": {"list": []},
        "links": [{"type": "dashboards", "tags": ["fleet"], "title": "Fleet", "asDropdown": True,
                   "includeVars": True, "keepTime": True}],
        "panels": board.panels,
    }


def overview() -> dict:
    c = "fleet_reconcile_duration_seconds_count"
    b = Board()
    b.row("Now")
    b.add(stat("Reconciles per second", rate(c),
               "Object reconciles per second over all controllers. Zero means the manager stopped sweeping.",
               unit="ops"), 4, 4)
    b.add(stat("Reconcile errors", over_range(c, 'fleet_outcome=~"error|leaseLost"'),
               "Reconciles that ended in error or lost the manager lease, over the range.", bad_above=1), 4, 4)
    b.add(stat("Failed effects", over_range("fleet_effects_total", 'fleet_outcome="failed"'),
               "Effects whose outcome was failed, over the range.", bad_above=1), 4, 4)
    b.add(stat("Unknown effect outcomes", over_range("fleet_effects_total", 'fleet_outcome="unknown"'),
               "Effects Fleet could not observe the outcome of. Fleet recovers them by observing their key.",
               bad_above=1), 4, 4)
    b.add(stat("Inbox entries waiting", f"max(fleet_inbox_depth{sel()})",
               "Undelivered inbox entries over every Task, at the start of the latest pass."), 4, 4)
    b.add(stat("Forge throttles", over_range("fleet_forge_calls_total", 'fleet_outcome="throttled"'),
               "Change polls a forge throttled, over the range.", bad_above=1), 4, 4)

    # Fleet exports no Task phases; the Task effects are the flow they leave.
    b.row("Task flow")
    for title, effect, outcomes, desc in (
        ("Tasks spawned", "spawn", "spawned|adopted", "Task sessions started or adopted, over the range."),
        ("Tasks briefed", "brief", "delivered|already", "Task briefs sent, over the range."),
        ("Tasks released", "release", "released", "Tasks whose release was established, over the range."),
    ):
        b.add(stat(title, over_range("fleet_effects_total", f'fleet_effect="{effect}"', f'fleet_outcome=~"{outcomes}"'),
                   desc), 8, 4)

    b.row("Reconcile")
    b.add(series("Reconciles by controller", [target(rate(c, by="fleet_controller"), "{{fleet_controller}}")],
                 "Object reconciles per second, per controller.", unit="ops"), 12, 8)
    b.add(series("Reconciles by outcome", [target(rate(c, by="fleet_outcome"), "{{fleet_outcome}}")],
                 "changed: the reconcile wrote or ran an effect. conflict, error and leaseLost need attention "
                 "when they persist.", unit="ops", stack=True), 12, 8)
    b.add(series("Mean reconcile duration by controller",
                 [target(mean_duration(by="fleet_controller"), "{{fleet_controller}}")],
                 "Mean of one object's reconcile. Fleet v2.4 records this histogram on the SDK's default "
                 "bucket bounds, which are millisecond-scale, so quantiles from its buckets are wrong. "
                 "Use the Fleet traces dashboard for p50/p95.", unit="s"), 12, 8)
    b.add(series("Reconcile problems",
                 [target(rate(c, 'fleet_outcome=~"error|conflict|leaseLost"', by="fleet_controller, fleet_outcome"),
                         "{{fleet_controller}} {{fleet_outcome}}")],
                 "Reconciles per second that ended in error, conflict or leaseLost.", unit="ops"), 12, 8)

    b.row("Effects")
    b.add(series("Effects by kind and outcome",
                 [target(rate("fleet_effects_total", by="fleet_effect, fleet_outcome"),
                         "{{fleet_effect}} {{fleet_outcome}}")],
                 "External effects per second: spawn, brief, deliver, release and markReady.",
                 unit="ops", bars=True, stack=True), 16, 8)
    b.add(table("Effects over the range",
                over_range("fleet_effects_total", by="fleet_effect, fleet_outcome"),
                "Effects by kind and outcome over the dashboard range.", ["fleet_effect", "fleet_outcome"]), 8, 8)
    b.row("Not exported yet")
    b.add(text("Task phases", "Fleet v2.4 exports no gauge of Tasks by phase, so this dashboard cannot "
               "show Releasing or NeedsAttention Tasks. Use `fleet list task` meanwhile."), 24, 3)
    return dashboard("fleet-overview", "Fleet overview",
                     "Fleet v2 manager health: reconcile passes, effects and errors.", b,
                     [datasource_var("prometheus", "prometheus", "Prometheus"), JOB_VAR])


def forge() -> dict:
    m = "fleet_forge_calls_total"
    host = 'fleet_forge_host=~"$host"'
    no_answer = 'fleet_outcome!~"known|absent"'
    b = Board()
    b.row("Over the range")
    b.add(stat("Change polls", over_range(m, host), "Change polls Fleet sent to the selected forges."), 6, 4)
    b.add(stat("Throttled", over_range(m, host, 'fleet_outcome="throttled"'),
               "Polls the forge throttled. Fleet defers the next polls to that host.", bad_above=1), 6, 4)
    b.add(stat("Not logged in or refused", over_range(m, host, 'fleet_outcome=~"notLoggedIn|refused"'),
               "Polls that failed on the forge login or permissions. These do not recover on their own.",
               bad_above=1), 6, 4)
    b.add(stat("Timed out or unavailable", over_range(m, host, 'fleet_outcome=~"timeout|unavailable|unreadable|incomplete"'),
               "Polls without a usable answer for a transient reason.", bad_above=1), 6, 4)

    b.row("Polls per host")
    b.add(series("Polls by host and outcome",
                 [target(rate(m, host, by="fleet_forge_host, fleet_outcome"), "{{fleet_forge_host}} {{fleet_outcome}}")],
                 "Change polls per second. known and absent are answers; every other outcome is an Unknown "
                 "observation, which Fleet never treats as absence.", unit="ops", stack=True), 12, 8)
    b.add(series("Unanswered ratio by host",
                 [target(f"{rate(m, host, no_answer, by='fleet_forge_host')} / {rate(m, host, by='fleet_forge_host')}",
                         "{{fleet_forge_host}}")],
                 "Share of polls per host that got no answer (not known or absent).", unit="percentunit"), 12, 8)
    b.add(series("Throttles by host",
                 [target(rate(m, host, 'fleet_outcome="throttled"', by="fleet_forge_host"), "{{fleet_forge_host}}")],
                 "Throttled polls per second, per forge host.", unit="ops", bars=True), 12, 8)
    b.add(table("Polls over the range", over_range(m, host, by="fleet_forge_host, fleet_outcome"),
                "Change polls by host and outcome over the dashboard range.", ["fleet_forge_host", "fleet_outcome"]),
          12, 8)

    b.row("ChangeRequests")
    cr = 'fleet_controller="ChangeRequestController"'
    b.add(series("ChangeRequest reconciles by outcome",
                 [target(rate("fleet_reconcile_duration_seconds_count", cr, by="fleet_outcome"), "{{fleet_outcome}}")],
                 "ChangeRequestController reconciles per second. Each due ChangeRequest polls its forge once.",
                 unit="ops", stack=True), 8, 8)
    b.add(series("Mean ChangeRequest reconcile duration", [target(mean_duration(cr, by="fleet_controller"), "mean")],
                 "Mean ChangeRequest reconcile, including the forge poll.", unit="s"), 8, 8)
    b.add(series("Mark-ready effects by outcome",
                 [target(rate("fleet_effects_total", 'fleet_effect="markReady"', by="fleet_outcome"), "{{fleet_outcome}}")],
                 "markReady effects per second: Fleet marks a draft change ready on the forge.",
                 unit="ops", bars=True), 8, 8)
    return dashboard("fleet-forge", "Fleet forge polling",
                     "ChangeRequest polling per forge host: answers, throttles and failures.", b,
                     [datasource_var("prometheus", "prometheus", "Prometheus"), JOB_VAR,
                      query_var("host", "Forge host", 'label_values(fleet_forge_calls_total{job=~"$job"}, fleet_forge_host)',
                                "The forge host of a change poll.")])


def gateway() -> dict:
    gw = 'fleet_controller=~"ConversationController|ListenerController"'
    deliver = 'fleet_effect="deliver"'
    b = Board()
    b.row("Inbox")
    b.add(stat("Inbox entries waiting", f"max(fleet_inbox_depth{sel()})",
               "Undelivered inbox entries over every Task, at the start of the latest pass. Fleet delivers an "
               "entry when the Task's session is idle."), 6, 4)
    b.add(stat("Delivered", over_range("fleet_effects_total", deliver, 'fleet_outcome=~"delivered|already"'),
               "Inbox entries delivered over the range."), 6, 4)
    b.add(stat("Delivery failed or unknown", over_range("fleet_effects_total", deliver, 'fleet_outcome=~"failed|unknown"'),
               "Deliveries that failed, or whose outcome Fleet could not observe.", bad_above=1), 6, 4)
    b.add(text("Per-Task depth", "Fleet v2.4 exports the inbox depth as one total without a Task attribute. "
               "Find the Task in the **Fleet traces** dashboard (`effect deliver` spans)."), 6, 4)
    b.add(series("Inbox depth", [target(f"max(fleet_inbox_depth{sel()})", "waiting")],
                 "Undelivered inbox entries over time. A depth that never returns to zero means a Task's "
                 "session never goes idle.", unit="short"), 12, 8)
    b.add(series("Deliveries by outcome",
                 [target(rate("fleet_effects_total", deliver, by="fleet_outcome"), "{{fleet_outcome}}")],
                 "deliver effects per second, by outcome.", unit="ops", bars=True, stack=True), 12, 8)

    b.row("Gateway")
    b.add(series("Gateway reconciles by controller and outcome",
                 [target(rate("fleet_reconcile_duration_seconds_count", gw, by="fleet_controller, fleet_outcome"),
                         "{{fleet_controller}} {{fleet_outcome}}")],
                 "ConversationController relays conversations into Task inboxes; ListenerController keeps "
                 "Gateway listeners.", unit="ops", stack=True), 12, 8)
    b.add(series("Mean Gateway reconcile duration",
                 [target(mean_duration(gw, by="fleet_controller"), "{{fleet_controller}}")],
                 "Mean reconcile of a Conversation or a Listener.", unit="s"), 12, 8)
    return dashboard("fleet-gateway", "Fleet Gateway and inbox",
                     "Inbox depth, deliveries and the Gateway controllers.", b,
                     [datasource_var("prometheus", "prometheus", "Prometheus"), JOB_VAR])


def traces() -> dict:
    svc = 'resource.service.name = "$service"'
    obj = 'span.fleet.name =~ "$object"'

    def search(title: str, q: str, desc: str) -> dict:
        return {"type": "table", "title": title, "description": desc, "datasource": TEMPO,
                "targets": [{"refId": "A", "datasource": TEMPO, "queryType": "traceql", "query": q,
                             "limit": 20, "spss": 3, "tableType": "spans"}],
                "fieldConfig": {"defaults": {}, "overrides": []}, "options": {}}

    def metrics(title: str, q: str, desc: str, unit: str) -> dict:
        return {"type": "timeseries", "title": title, "description": desc, "datasource": TEMPO,
                "targets": [{"refId": "A", "datasource": TEMPO, "queryType": "traceql", "query": q,
                             "metricsQueryType": "range", "step": "1m"}],
                "fieldConfig": {"defaults": {"unit": unit, "custom": {"lineWidth": 1, "showPoints": "never",
                                                                       "spanNulls": True}},
                                "overrides": []},
                "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["mean", "max"]},
                            "tooltip": {"mode": "multi", "sort": "desc"}}}

    reconciles = f'{svc} && name =~ "reconcile .+" && name != "reconcile pass"'
    b = Board()
    b.row("Latency from spans")
    b.add(metrics("Object reconcile p50 / p95 by controller",
                  f"{{ {reconciles} }} | quantile_over_time(duration, .5, .95) by (span.fleet.controller)",
                  "Quantiles of one object's reconcile, from spans. Needs TraceQL metrics in Tempo.", "s"), 12, 8)
    b.add(metrics("Reconcile pass p50 / p95",
                  f'{{ {svc} && name = "reconcile pass" }} | quantile_over_time(duration, .5, .95)',
                  "Quantiles of one whole pass over every kind.", "s"), 12, 8)
    b.row("Find a trace")
    b.add(search("Object reconciles", f"{{ {reconciles} && {obj} }}",
                 "One reconcile of a Fleet object. Set Object to a Task or ChangeRequest name."), 12, 9)
    b.add(search("Effects", f'{{ {svc} && name =~ "effect .+" && {obj} }}',
                 "spawn, brief, deliver, release and markReady effects, with their outcome."), 12, 9)
    b.add(search("Errors", f"{{ {svc} && status = error }}",
                 "Commands with a non-zero exit and reconciles that failed."), 12, 9)
    b.add(search("Forge polls without an answer",
                 f'{{ {svc} && name = "forge poll" && span.fleet.outcome !~ "known|absent" && {obj} }}',
                 "Change polls that were throttled, timed out or failed, with their forge host."), 12, 9)
    b.add(search("Commands", f'{{ {svc} && name =~ "fleet .+" }}',
                 "Fleet CLI commands, such as a worker's fleet result, when its session exports telemetry."), 24, 9)
    return dashboard("fleet-traces", "Fleet traces",
                     "Reconcile, effect and forge spans in Tempo.", b,
                     [datasource_var("tempo", "tempo", "Tempo"),
                      textbox_var("service", "Service", "fleet", "The service.name Fleet exports under (OTEL_SERVICE_NAME)."),
                      textbox_var("object", "Object", ".+", "A regular expression on the Fleet object name (fleet.name).")])


DASHBOARDS = {"fleet-overview.json": overview, "fleet-forge.json": forge, "fleet-gateway.json": gateway,
              "fleet-traces.json": traces}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--write", action="store_true", help="regenerate dashboards/")
    a = p.parse_args()
    stale = []
    for name, build in DASHBOARDS.items():
        path, text_ = OUT / name, json.dumps(build(), indent=2) + "\n"
        if a.write:
            path.write_text(text_)
        elif not path.exists() or path.read_text() != text_:
            stale.append(name)
    if stale:
        sys.exit(f"dashboards: {', '.join(stale)} differ from tools/dashboards.py; run with --write")
    print("dashboards: written" if a.write else "dashboards: up to date")


if __name__ == "__main__":
    main()
