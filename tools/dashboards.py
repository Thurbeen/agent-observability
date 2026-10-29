# /// script
# requires-python = ">=3.12"
# ///
"""Generate the Grafana dashboards in dashboards/fleet and dashboards/thurbox.

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

# Fleet exports metrics every 60 s by default (OTEL_METRIC_EXPORT_INTERVAL),
# and Thurbox once a minute from `automation tick`. A rate needs two points,
# so no Prometheus panel looks at less than 2m.
MIN_INTERVAL = "2m"
JOB = 'job=~"$job"'
PROM = {"type": "prometheus", "uid": "${prometheus}"}
TEMPO = {"type": "tempo", "uid": "${tempo}"}
LOKI = {"type": "loki", "uid": "${loki}"}

# The status colours. Every other series takes Grafana's palette.
BAD, WARN, GOOD = "red", "orange", "green"
OUTCOME_COLORS = {
    "error": BAD, "failed": BAD, "unknown": WARN, "leaseLost": WARN, "conflict": WARN,
    "throttled": WARN, "notLoggedIn": BAD, "refused": BAD, "timeout": WARN,
    "unavailable": WARN, "unreadable": WARN, "incomplete": WARN,
}


def sel(*matchers: str) -> str:
    # Thurbox's resource names service.name only, so its job is always thurbox.
    first = () if any(m.startswith("job=") for m in matchers) else (JOB,)
    return "{" + ",".join(first + matchers) + "}"


def rate(metric: str, *matchers: str, by: str = "") -> str:
    group = f" by ({by})" if by else ""
    return f"sum{group} (rate({metric}{sel(*matchers)}[$__rate_interval]))"


def over_range(metric: str, *matchers: str, by: str = "") -> str:
    """Increase over the dashboard range, counting a series born inside the
    range from zero: `increase()` never counts a series' first sample, and
    rare outcomes start a new series at 1."""
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
    product = uid.split("-")[0]
    return {
        "uid": uid,
        "title": title,
        "description": desc,
        "tags": ["agent-observability", product],
        "editable": True,
        "graphTooltip": 1,
        "time": {"from": "now-6h", "to": "now"},
        "refresh": "1m",
        "schemaVersion": 41,
        "templating": {"list": variables},
        "annotations": {"list": []},
        "links": [{"type": "dashboards", "tags": ["agent-observability"], "title": "Agents", "asDropdown": True,
                   "includeVars": False, "keepTime": True}],
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


THURBOX = 'job="thurbox"'
HOST = 'host_name=~"$host"'
AGENT = 'agent=~"$agent"'
# Thurbox's state words, coloured the way its session list reads.
STATE_COLORS = {
    "working": "blue", "blocked": "orange", "background": "purple", "done": "green", "idle": "text",
    "failed": "red", "lost": "dark-red", "unreachable": "semi-dark-orange", "stopped": "gray",
    "running": "light-blue", "uncovered": "gray", "unreported": "gray",
}
THURBOX_VARS = [
    datasource_var("prometheus", "prometheus", "Prometheus"),
    query_var("host", "Host", 'label_values(thurbox_sessions{job="thurbox"}, host_name)',
              "The host.name of a Thurbox instance. Empty unless Prometheus promotes host.name."),
    query_var("agent", "Agent", 'label_values(thurbox_sessions{job="thurbox", host_name=~"$host"}, agent)',
              "The coding agent a session runs."),
]


def tb(*matchers: str) -> tuple[str, ...]:
    return (THURBOX, HOST, AGENT) + matchers


def counted(metric: str, window: str = "$__range", by: str = "") -> str:
    """What a Thurbox counter or histogram series counted over a window.

    Thurbox exports deltas, and the collector's deltatocumulative processor
    sends a cumulative point only when a delta arrives, so a series has a
    sample only in the minutes something happened. `increase()` needs two
    samples in the window and goes stale between them; the last value in the
    window minus the last value before it does not. A series born in the
    window counts from zero. The look-back before the window is one day.
    """
    s = f"{metric}{sel(*tb())}"
    last = f"last_over_time({s}[{window}])"
    group = f" by ({by})" if by else ""
    return f"sum{group} (clamp_min({last} - last_over_time({s}[1d] offset {window}), 0) or {last})"


def quantile(q: float, metric: str, window: str) -> str:
    return f"histogram_quantile({q}, {counted(metric + '_bucket', window, by='le')})"


def by_state(panel: dict) -> dict:
    panel["fieldConfig"]["overrides"] = [
        {"matcher": {"id": "byName", "options": state},
         "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]}
        for state, color in STATE_COLORS.items()
    ]
    return panel


def thurbox_sessions() -> dict:
    g = "thurbox_sessions"
    now = lambda states: f'sum({g}{sel(*tb(f"state=~\"{states}\""))})'
    b = Board()
    b.row("Now")
    b.add(stat("Sessions", f"sum({g}{sel(*tb())})", "Live sessions, in every state."), 4, 4)
    b.add(stat("Working", now("working"), "Sessions whose agent is in a turn."), 4, 4)
    waiting = stat("Waiting on you", now("blocked"), "Sessions blocked on a human: a permission prompt or a "
                   "question.")
    waiting["fieldConfig"]["defaults"]["thresholds"]["steps"] = [{"color": GOOD, "value": None},
                                                                 {"color": WARN, "value": 1}]
    b.add(waiting, 4, 4)
    b.add(stat("Done", now("done"), "Sessions whose agent finished a turn you have not looked at."), 4, 4)
    b.add(stat("Idle", now("idle|background"), "Sessions at rest, or running background work."), 4, 4)
    b.add(stat("Failed, lost or unreachable", now("failed|lost|unreachable"),
               "A launch that failed, a pane that went with nobody asking, or a remote host that cannot be "
               "reached.", bad_above=1), 4, 4)

    b.row("Sessions")
    b.add(by_state(series("Sessions by state", [target(f"sum by (state) ({g}{sel(*tb())}) > 0", "{{state}}")],
                          "Live sessions by the state word thurbox-cli session list shows.", stack=True)), 12, 8)
    b.add(series("Sessions by agent", [target(f"sum by (agent) ({g}{sel(*tb())})", "{{agent}}")],
                 "Live sessions per coding agent.", stack=True), 12, 8)

    turns, waits = "thurbox_turn_duration_seconds", "thurbox_blocked_duration_seconds"
    b.row("Turns")
    b.add(stat("Turns", counted(f"{turns}_count"),
               "Turns that ended over the range. A turn runs from entering working to leaving it."), 6, 4)
    b.add(stat("Median turn", quantile(0.5, turns, "$__range"), "Median turn over the range.", unit="s"), 6, 4)
    b.add(stat("p95 turn", quantile(0.95, turns, "$__range"), "95th percentile turn over the range.", unit="s"), 6, 4)
    b.add(stat("Median wait on you", quantile(0.5, waits, "$__range"),
               "Median time a session stayed blocked on a human, over the range.", unit="s"), 6, 4)
    b.add(series("Turn duration, trailing hour",
                 [target(quantile(0.5, turns, "1h"), "p50"), target(quantile(0.95, turns, "1h"), "p95", ref="B")],
                 "Turn quantiles over the hour before each point, from the histogram's buckets (1 s to 8 h).",
                 unit="s"), 12, 8)
    b.add(series("Wait on you, trailing hour",
                 [target(quantile(0.5, waits, "1h"), "p50"), target(quantile(0.95, waits, "1h"), "p95", ref="B")],
                 "How long sessions stayed blocked on a human, over the hour before each point.", unit="s"), 12, 8)
    b.add(table("Turns by outcome", counted(f"{turns}_count", by="outcome"),
                "Turns over the range by the state they ended in: done, blocked, idle or background.",
                ["outcome"]), 8, 8)
    mean = f"{counted(turns + '_sum', by='agent')} / {counted(turns + '_count', by='agent')}"
    gauge = {"type": "bargauge", "title": "Mean turn by agent", "datasource": PROM, "interval": MIN_INTERVAL,
             "description": "Mean turn per agent over the range.",
             "targets": [{**target(mean, "{{agent}}"), "instant": True, "range": False}],
             "fieldConfig": {"defaults": {"unit": "s", "min": 0, "color": {"mode": "palette-classic"}}, "overrides": []},
             "options": {"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                         "reduceOptions": {"calcs": ["lastNotNull"]}}}
    b.add(gauge, 8, 8)
    b.add(table("State changes", counted("thurbox_session_transitions_total", by="from, to"),
                "State changes over the range. An empty from is a session's first state.", ["from", "to"]), 8, 8)

    b.row("Lifecycle")
    b.add(table("Lifecycle events", counted("thurbox_session_lifecycle_total", by="reason"),
                "Sessions spawned, stopped, lost, relaunched, deleted and more, over the range.", ["reason"]), 8, 8)
    b.add(table("Agent exits", counted("thurbox_session_exits_total", by="agent, exit_code"),
                "Agent processes that returned, by exit code, over the range.", ["agent", "exit_code"]), 8, 8)
    life = "thurbox_session_lifetime_seconds"
    b.add(stat("Median session lifetime", quantile(0.5, life, "$__range"),
               "Median time from creation to going, for sessions that went over the range.", unit="s"), 8, 8)
    return dashboard("thurbox-sessions", "Thurbox sessions",
                     "Thurbox's own session metrics: sessions by state, turns, waits on a human and lifecycle.",
                     b, THURBOX_VARS)


def thurbox_events() -> dict:
    stream = '{service_name="thurbox"}'
    session = '| thurbox_session_name =~ "$session" | agent =~ "$agent"'
    events = f'{stream} |= "thurbox.session." {session}'

    def logs(title: str, expr: str, desc: str) -> dict:
        return {"type": "logs", "title": title, "description": desc, "datasource": LOKI,
                "targets": [{"refId": "A", "datasource": LOKI, "expr": expr, "queryType": "range"}],
                "options": {"showTime": True, "wrapLogMessage": False, "sortOrder": "Descending",
                            "enableLogDetails": True, "dedupStrategy": "none"}}

    b = Board()
    b.row("Session events")
    count = {"type": "timeseries", "title": "Session events", "datasource": LOKI, "interval": "10m",
             "description": "Session events per 10 minutes, from Thurbox's event log records.",
             "targets": [{"refId": "A", "datasource": LOKI, "legendFormat": "{{event}}",
                          "expr": f'sum by (event) (count_over_time({events} | label_format event="{{{{__line__}}}}" '
                                  "[$__interval]))"}],
             "fieldConfig": {"defaults": {"unit": "short", "min": 0, "custom": {
                 "drawStyle": "bars", "fillOpacity": 60, "lineWidth": 1, "stacking": {"mode": "normal", "group": "A"}}},
                 "overrides": []},
             "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["sum"]},
                         "tooltip": {"mode": "multi", "sort": "desc"}}}
    b.add(count, 24, 7)
    timeline = (f'{events} | line_format "{{{{.thurbox_session_name}}}}  {{{{.agent}}}}  '
                '{{trimPrefix \\"thurbox.session.\\" __line__}} {{.from}}{{if .to}} -> {{.to}}{{end}} {{.reason}}"')
    b.add(logs("Session timeline", timeline,
               "One line per session event: created, state_changed (from -> to), changed and gone (reason). "
               "Each record carries thurbox.session.id, which the agents' own telemetry carries too."), 24, 12)
    b.row("Diagnostics")
    b.add(logs("Interface warnings", f'{stream} != "thurbox.session." | detected_level=~"warn|error"',
               "The interface's own WARN and ERROR log lines, sent when export_diagnostic_logs is on."), 24, 7)
    return dashboard("thurbox-events", "Thurbox session events",
                     "Thurbox's session event log records and diagnostic logs in Loki.", b,
                     [datasource_var("loki", "loki", "Loki"),
                      textbox_var("session", "Session", ".+", "A regular expression on the session name."),
                      textbox_var("agent", "Agent", ".+", "A regular expression on the agent.")])


# Each agent's own token types mapped onto four classes, so every agent
# Thurbox launches shares one scale. Claude Code's types are disjoint. Codex
# counts cached input inside input (total = input + output) and reasoning
# inside output. A new agent needs one entry here.
TOKEN_CLASSES = {
    "claude": ("claude_code_token_usage_tokens_total", "type", {
        "input": ["input"], "cache_read": ["cacheRead"], "cache_write": ["cacheCreation"], "output": ["output"]}),
    "codex": ("codex_turn_token_usage_sum", "token_type", {
        "input": ["input", "-cached_input"], "cache_read": ["cached_input"], "cache_write": ["cache_write_input"],
        "output": ["output"]}),
}
SESSION = "thurbox_agent, thurbox_session_id, thurbox_session_name"


def grew(selector: str, window: str) -> str:
    """Per series, what an agent's counter counted over a window. The agents
    export deltas, which reach Prometheus as sparse cumulative samples: the
    same last-value difference as counted()."""
    last = f"last_over_time({selector}[{window}])"
    return f"(clamp_min({last} - last_over_time({selector}[1d] offset {window}), 0) or {last})"


def tokens(window: str, by: str) -> str:
    parts = []
    for metric, label, classes in TOKEN_CLASSES.values():
        for cls, types in classes.items():
            terms = [(t.startswith("-"), t.lstrip("-")) for t in types]
            expr = " ".join(
                f"{'-' if neg else ('+' if i else '')} sum by ({SESSION}) "
                f"({grew(f'{metric}{{thurbox_agent!=\"\",{label}=\"{t}\"}}', window)})"
                for i, (neg, t) in enumerate(terms))
            parts.append(f'label_replace({expr}, "token_class", "{cls}", "", "")')
    return f"sum by ({by}) ({' or '.join(parts)})"


def cost(window: str, by: str) -> str:
    # Only Claude Code exports a cost. Nothing here prices another agent.
    return f'sum by ({by}) ({grew("claude_code_cost_usage_USD_total{thurbox_agent!=\"\"}", window)})'


def as_agent(expr: str) -> str:
    return f'sum by (agent) (label_replace({expr}, "agent", "$1", "thurbox_agent", "(.+)"))'


def agent_usage() -> dict:
    def bar_gauge(title: str, expr: str, desc: str, unit: str) -> dict:
        return {"type": "bargauge", "title": title, "description": desc, "datasource": PROM, "interval": MIN_INTERVAL,
                "targets": [{**target(expr, "{{thurbox_agent}}"), "instant": True, "range": False}],
                "fieldConfig": {"defaults": {"unit": unit, "min": 0, "noValue": "none exported",
                                             "color": {"mode": "palette-classic"}}, "overrides": []},
                "options": {"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                            "reduceOptions": {"calcs": ["lastNotNull"]}}}

    def merged(title: str, desc: str, targets: list[dict], names: dict[str, str], units: dict[str, str],
               sort: str, required: str = "") -> dict:
        return {"type": "table", "title": title, "description": desc, "datasource": PROM, "interval": MIN_INTERVAL,
                "targets": targets,
                "transformations": [
                    {"id": "merge", "options": {}},
                    {"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": names}},
                    {"id": "sortBy", "options": {"sort": [{"field": sort, "desc": True}]}},
                ] + ([{"id": "filterByValue", "options": {"type": "exclude", "match": "any", "filters": [
                    {"fieldName": required, "config": {"id": "isNull", "options": {}}}]}}] if required else []),
                "fieldConfig": {"defaults": {"noValue": "—", "decimals": 0}, "overrides": [
                    {"matcher": {"id": "byName", "options": name},
                     "properties": [{"id": "unit", "value": unit}] + (
                         [{"id": "decimals", "value": 2}] if unit == "currencyUSD" else [])}
                    for name, unit in units.items()]},
                "options": {"showHeader": True}}

    b = Board()
    b.row("Over the range")
    b.add(bar_gauge("Cost by agent", cost("$__range", "thurbox_agent"),
                    "USD over the range, as each agent reports it. Only Claude Code exports a cost: the "
                    "other agents show under Agent coverage, never as an estimate.", "currencyUSD"), 6, 8)
    b.add(bar_gauge("Tokens by agent", tokens("$__range", "thurbox_agent"),
                    "Every agent on one scale: uncached input, cache reads, cache writes and output, "
                    "normalised from each agent's own token types (TOKEN_CLASSES in tools/dashboards.py).", "short"), 6, 8)
    peak = 'max by (agent) (max_over_time((sum by (agent) (thurbox_sessions{job="thurbox"}))[$__range:5m]))'
    b.add(merged("Agent coverage",
                 "Every agent Thurbox ran over the range, with what it exports. An agent with sessions and no "
                 "tokens exports no usage telemetry, so it is missing from the other panels.",
                 [{**target(peak, instant=True), "refId": "A"},
                  {**target(as_agent(tokens("$__range", "thurbox_agent")), instant=True), "refId": "B"},
                  {**target(as_agent(cost("$__range", "thurbox_agent")), instant=True), "refId": "C"}],
                 {"Value #A": "peak sessions", "Value #B": "tokens", "Value #C": "USD"},
                 {"tokens": "short", "USD": "currencyUSD"}, sort="peak sessions"), 12, 8)

    b.row("Over time")
    hourly = series("Tokens per hour by agent and class",
                    [target(tokens("1h", "thurbox_agent, token_class"), "{{thurbox_agent}} {{token_class}}")],
                    "Normalised tokens per hour, stacked: every agent that exports usage, in one graph.",
                    bars=True, stack=True)
    hourly["interval"] = "1h"
    hourly["options"]["legend"]["calcs"] = ["sum"]
    b.add(hourly, 14, 10)
    b.add(merged("Top sessions",
                 "Thurbox sessions by normalised tokens over the range, every agent together, with the cost "
                 "where the agent reports one.",
                 [{**target(f"topk(15, {tokens('$__range', 'thurbox_session_name, thurbox_agent')})",
                            instant=True), "refId": "A"},
                  {**target(cost("$__range", "thurbox_session_name, thurbox_agent"), instant=True),
                   "refId": "B"}],
                 {"thurbox_session_name": "session", "thurbox_agent": "agent", "Value #A": "tokens", "Value #B": "USD"},
                 {"tokens": "short", "USD": "currencyUSD"}, sort="tokens", required="tokens"), 10, 10)
    return dashboard("thurbox-agent-usage", "Agent usage",
                     "Tokens and cost of the agents Thurbox launches, normalised across agents.", b,
                     [datasource_var("prometheus", "prometheus", "Prometheus")])


DASHBOARDS = {
    "fleet/fleet-overview.json": overview, "fleet/fleet-forge.json": forge, "fleet/fleet-gateway.json": gateway,
    "fleet/fleet-traces.json": traces,
    "thurbox/thurbox-sessions.json": thurbox_sessions, "thurbox/thurbox-events.json": thurbox_events,
    "thurbox/thurbox-agent-usage.json": agent_usage,
}


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
