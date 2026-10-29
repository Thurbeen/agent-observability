# /// script
# requires-python = ">=3.12"
# ///
"""Smoke-test the quick-start stack with a Fleet-shaped OTLP payload.

    docker compose -f quickstart/compose.yaml up -d
    uv run tests/smoke.py

The payload carries every metric, label and span name in catalog/fleet.json,
sent the way Fleet sends it: OTLP over HTTP to the collector. The test then
checks that each backend stored it, that Prometheus loaded the rules, that
every dashboard query runs, and that Grafana provisioned the dashboards.
"""

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = os.environ.get("COLLECTOR", "http://localhost:4318")
PROMETHEUS = os.environ.get("PROMETHEUS", "http://localhost:9090")
TEMPO = os.environ.get("TEMPO", "http://localhost:3200")
LOKI = os.environ.get("LOKI", "http://localhost:3100")
GRAFANA = os.environ.get("GRAFANA", "http://localhost:3000")
DEADLINE = float(os.environ.get("SMOKE_TIMEOUT", "180"))
# The OpenTelemetry SDK's default histogram bounds, which Fleet v2.4 uses.
BOUNDS = [0, 5, 10, 25, 50, 75, 100, 250, 500, 750, 1000, 2500, 5000, 7500, 10000]


def http(url: str, body: dict | None = None) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode(), e.code
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        return 0, str(e)
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def until(what: str, check, deadline: float) -> None:
    last = None
    while time.monotonic() < deadline:
        try:
            ok, last = check()
        except Exception as e:  # noqa: BLE001 - a backend that is still starting
            ok, last = False, repr(e)
        if ok:
            print(f"ok   {what}")
            return
        time.sleep(2)
    sys.exit(f"FAIL {what}: {str(last)[:500]}")


def attrs(d: dict) -> list[dict]:
    return [{"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": v}}
            for k, v in d.items()]


def payloads(catalog: dict, trace_id: str) -> dict[str, dict]:
    tag = catalog["fleet"]["tag"]
    resource = {"attributes": attrs({"service.name": "fleet", "service.version": tag.lstrip("v")})}
    scope = {"name": "fleet"}
    now = time.time_ns()
    starts, times = now - 600 * 10**9, [now - 60 * 10**9, now]
    metrics = []
    for m in catalog["metrics"]:
        labels = {a: (v[0] if v else "github.com") for a, v in m["attributes"].items()}
        points = []
        for i, t in enumerate(times):
            p = {"attributes": attrs(labels), "startTimeUnixNano": str(starts), "timeUnixNano": str(t)}
            if m["instrument"] == "histogram":
                n = 60 * (i + 1)
                p.update(count=str(n), sum=0.25 * n, explicitBounds=BOUNDS,
                         bucketCounts=[str(x) for x in [0, n] + [0] * (len(BOUNDS) - 1)])
            else:
                p["asInt"] = str(10 * (i + 1)) if m["instrument"] == "counter" else "1"
            points.append(p)
        body = {"dataPoints": points}
        if m["instrument"] == "histogram":
            metric = {"histogram": {**body, "aggregationTemporality": 2}}
        elif m["instrument"] == "counter":
            metric = {"sum": {**body, "aggregationTemporality": 2, "isMonotonic": True}}
        else:
            metric = {"gauge": body}
        metrics.append({"name": m["name"], "unit": m["unit"], **metric})

    def span(name: str, span_id: str, parent: str, a: dict, offset_ms: int) -> dict:
        start = now - 30 * 10**9 + offset_ms * 10**6
        return {"traceId": trace_id, "spanId": span_id, "parentSpanId": parent, "name": name, "kind": 1,
                "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + 40 * 10**6),
                "attributes": attrs(a)}

    identity = {"fleet.kind": "Task", "fleet.name": "smoke", "fleet.uid": "u1-smoke", "fleet.generation": 1}
    root, task, deliver = (secrets.token_hex(8) for _ in range(3))
    spans = [
        span("reconcile pass", root, "", {"fleet.pass": 1}, 0),
        span("reconcile Task", task, root,
             {**identity, "fleet.controller": "TaskController", "fleet.outcome": "changed"}, 1),
        span("effect deliver", deliver, task,
             {**identity, "fleet.effect": "deliver", "fleet.outcome": "delivered"}, 2),
    ]
    log = {"timeUnixNano": str(now), "severityText": "INFO", "body": {"stringValue": "fleet-monitoring smoke"}}
    return {
        "metrics": {"resourceMetrics": [{"resource": resource, "scopeMetrics": [{"scope": scope, "metrics": metrics}]}]},
        "traces": {"resourceSpans": [{"resource": resource, "scopeSpans": [{"scope": scope, "spans": spans}]}]},
        "logs": {"resourceLogs": [{"resource": {"attributes": attrs({"service.name": "fleet-monitoring-smoke"})},
                                   "scopeLogs": [{"scope": scope, "logRecords": [log]}]}]},
    }


def prom(query: str) -> tuple[int, dict | str]:
    return http(f"{PROMETHEUS}/api/v1/query?" + urllib.parse.urlencode({"query": query}))


def grafana_vars(expr: str, values: dict[str, str]) -> str:
    for k, v in sorted(values.items(), key=lambda kv: -len(kv[0])):
        expr = expr.replace(f"${k}", v)
    return expr


def main() -> None:
    deadline = time.monotonic() + DEADLINE
    catalog = json.loads((ROOT / "catalog" / "fleet.json").read_text())
    trace_id = secrets.token_hex(16)
    body = payloads(catalog, trace_id)

    until("Prometheus ready", lambda: (http(f"{PROMETHEUS}/-/ready")[0] == 200, "not ready"), deadline)
    until("Tempo ready", lambda: (http(f"{TEMPO}/ready")[0] == 200, http(f"{TEMPO}/ready")), deadline)
    until("Loki ready", lambda: (http(f"{LOKI}/ready")[0] == 200, http(f"{LOKI}/ready")), deadline)
    for signal, payload in body.items():
        until(f"collector accepts {signal}",
              lambda: (lambda r: (r[0] == 200, r))(http(f"{COLLECTOR}/v1/{signal}", payload)), deadline)

    for m in catalog["metrics"]:
        for s in m["prometheus"]["series"]:
            until(f"Prometheus has {s}",
                  lambda: (lambda r: (r[0] == 200 and bool(r[1]["data"]["result"]), r))(prom(f'{s}{{job="fleet"}}')),
                  deadline)

    groups = http(f"{PROMETHEUS}/api/v1/rules")[1]["data"]["groups"]
    names = {g["name"] for g in groups}
    if names != {"fleet.recording", "fleet.alerts"}:
        sys.exit(f"FAIL Prometheus loaded rule groups {sorted(names)}")
    print("ok   Prometheus loaded the rules")

    until(f"Tempo has trace {trace_id}", lambda: (http(f"{TEMPO}/api/traces/{trace_id}")[0] == 200, "absent"),
          deadline)
    # Tempo serves a trace by id before its search finds it.
    until("Tempo search finds the trace",
          lambda: (lambda r: (r[0] == 200 and any(t.get("traceID", "").lstrip("0") == trace_id.lstrip("0")
                                                  for t in r[1].get("traces", [])), r))(
              http(f"{TEMPO}/api/search?" + urllib.parse.urlencode(
                  {"q": '{ name = "effect deliver" }', "start": int(time.time()) - 3600,
                   "end": int(time.time()) + 60}))),
          deadline)
    until("Loki has the log line",
          lambda: (lambda r: (r[0] == 200 and bool(r[1]["data"]["result"]), r))(
              http(f"{LOKI}/loki/api/v1/query_range?"
                   + urllib.parse.urlencode({"query": '{service_name="fleet-monitoring-smoke"}', "since": "1h"}))),
          deadline)

    until("Grafana provisioned the dashboards",
          lambda: (lambda r: (r[0] == 200 and {d["uid"] for d in r[1]}
                              >= {"fleet-overview", "fleet-forge", "fleet-gateway", "fleet-traces"}, r))(
              http(f"{GRAFANA}/api/search?tag=fleet")),
          deadline)
    for uid in ("prometheus", "tempo", "loki"):
        until(f"Grafana reaches data source {uid}",
              lambda: (lambda r: (r[0] == 200, r))(http(f"{GRAFANA}/api/datasources/uid/{uid}/health")), deadline)

    # Every dashboard query, as Grafana would send it with All selected.
    values = {"__rate_interval": "5m", "__interval": "2m", "__range": "1h", "job": ".*", "host": ".*",
              "service": "fleet", "object": ".+"}
    end = int(time.time()) + 60
    failed = []
    for path in sorted((ROOT / "dashboards").glob("*.json")):
        d = json.loads(path.read_text())
        for p in d["panels"]:
            for t in p.get("targets", []):
                if "expr" in t:
                    status, r = prom(grafana_vars(t["expr"], values))
                    ok = status == 200 and r["status"] == "success"
                    if ok and p["title"] in ("Reconciles per second", "Change polls", "Inbox depth"):
                        ok = bool(r["data"]["result"])
                elif t.get("queryType") == "traceql":
                    q = grafana_vars(t["query"], values)
                    api = "metrics/query_range" if t.get("metricsQueryType") else "search"
                    status, r = http(f"{TEMPO}/api/{api}?"
                                     + urllib.parse.urlencode({"q": q, "start": end - 3600, "end": end}))
                    ok = status == 200
                    if ok and p["title"] in ("Object reconciles", "Effects"):
                        ok = bool(r.get("traces"))
                else:
                    continue
                print(f"{'ok  ' if ok else 'FAIL'} {path.stem} / {p['title']}")
                if not ok:
                    failed.append(f"{path.stem} / {p['title']}: {str(r)[:300]}")
    if failed:
        sys.exit("FAIL dashboard queries:\n" + "\n".join(failed))
    print("smoke: the stack ingests Fleet's telemetry and every dashboard query runs")


if __name__ == "__main__":
    main()
