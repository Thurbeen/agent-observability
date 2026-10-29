# /// script
# requires-python = ">=3.12"
# ///
"""Smoke-test the quick-start stack with synthetic Fleet and Thurbox telemetry.

    docker compose -f quickstart/compose.yaml up -d
    uv run tests/smoke.py

It sends two hours of tools/synthetic.py's telemetry to the collector, the
way Fleet, Thurbox and the agents send it: OTLP over HTTP, Thurbox's and the
agents' metrics as deltas.
It then checks that every series in catalog/ reached Prometheus, that
Prometheus loaded the rules, that Tempo and Loki stored spans and log
records, that Grafana provisioned the dashboards under Agents/Fleet and
Agents/Thurbox, and that every dashboard query returns data.
"""

import json
import os
import subprocess
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
HOURS = 2


def http(url: str) -> tuple[int, dict | list | str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            raw, status = r.read().decode(), r.status
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


def query(base: str, path: str, **params) -> tuple[int, dict | list | str]:
    return http(f"{base}{path}?" + urllib.parse.urlencode(params))


def has_result(r: tuple) -> tuple[bool, tuple]:
    status, body = r
    return status == 200 and isinstance(body, dict) and bool(body.get("data", {}).get("result")), r


def grafana_vars(expr: str, values: dict[str, str]) -> str:
    for k, v in sorted(values.items(), key=lambda kv: -len(kv[0])):
        expr = expr.replace(f"${k}", v)
    return expr


def panels(items: list[dict]):
    for p in items:
        yield p
        yield from panels(p.get("panels", []))


def main() -> None:
    deadline = time.monotonic() + DEADLINE
    for name, url in (("Prometheus", f"{PROMETHEUS}/-/ready"), ("Tempo", f"{TEMPO}/ready"), ("Loki", f"{LOKI}/ready")):
        until(f"{name} ready", lambda: (http(url)[0] == 200, http(url)), deadline)
    subprocess.run([sys.executable, str(ROOT / "tools" / "synthetic.py"), "--hours", str(HOURS),
                    "--collector", COLLECTOR], check=True)
    end = int(time.time()) + 60
    window = {"start": end - HOURS * 3600 - 60, "end": end}
    # Tempo searches spans older than 15 minutes only once it has flushed
    # them to its backend, a few minutes after they arrive.
    recent = {"start": end - 600, "end": end}

    for product in ("fleet", "thurbox", "claude-code", "codex"):
        for m in json.loads((ROOT / "catalog" / f"{product}.json").read_text())["metrics"]:
            for s in m["prometheus"]["series"]:
                until(f"Prometheus has {s}",
                      lambda: has_result(query(PROMETHEUS, "/api/v1/query", query=f"count_over_time({s}[3h])")),
                      deadline)

    groups = {g["name"] for g in http(f"{PROMETHEUS}/api/v1/rules")[1]["data"]["groups"]}
    expected = {"fleet.recording", "fleet.alerts", "thurbox.recording", "thurbox.alerts"}
    if groups != expected:
        sys.exit(f"FAIL Prometheus loaded rule groups {sorted(groups)}")
    print("ok   Prometheus loaded the rules")

    # The Agent usage dashboard's four token classes add up to Codex's total.
    usage = json.loads((ROOT / "dashboards" / "thurbox" / "thurbox-agent-usage.json").read_text())
    by_agent = next(p for p in usage["panels"] if p["title"] == "Tokens by agent")["targets"][0]["expr"]

    def codex_adds_up() -> tuple[bool, tuple]:
        classes = query(PROMETHEUS, "/api/v1/query", query=by_agent.replace("$__range", "3h"))[1]["data"]["result"]
        total = query(PROMETHEUS, "/api/v1/query", query='sum(last_over_time(codex_turn_token_usage_sum'
                      '{thurbox_agent="codex",token_type="total"}[3h]))')[1]["data"]["result"]
        codex = [float(r["value"][1]) for r in classes if r["metric"].get("thurbox_agent") == "codex"]
        return bool(codex and total and abs(codex[0] - float(total[0]["value"][1])) < 0.5), (codex, total)

    until("Codex's token classes add up to its total", codex_adds_up, deadline)

    until("Tempo search finds Fleet's effect spans",
          lambda: (lambda r: (r[0] == 200 and bool(r[1].get("traces")), r))(
              query(TEMPO, "/api/search", q='{ name =~ "effect .+" }', **recent)),
          deadline)
    until("Loki has Thurbox's session events",
          lambda: has_result(query(LOKI, "/loki/api/v1/query_range",
                                   query='{service_name="thurbox"} |= "thurbox.session.created"', **window)),
          deadline)

    uids = {json.loads(f.read_text())["uid"]: f.parent.name for f in (ROOT / "dashboards").glob("*/*.json")}
    until("Grafana provisioned the dashboards",
          lambda: (lambda r: (r[0] == 200 and {d["uid"] for d in r[1]} >= set(uids), r))(
              http(f"{GRAFANA}/api/search?tag=agent-observability")),
          deadline)
    folders = {d["uid"]: d.get("folderTitle") for d in http(f"{GRAFANA}/api/search?tag=agent-observability")[1]}
    roots = [f for f in http(f"{GRAFANA}/api/folders")[1] if f["title"] == "Agents"]
    children = {f["title"] for f in http(f"{GRAFANA}/api/folders?parentUid={roots[0]['uid']}")[1]} if roots else set()
    wrong = {uid for uid, product in uids.items() if folders.get(uid) != product.title()}
    if not roots or children != {"Fleet", "Thurbox"} or wrong:
        sys.exit(f"FAIL Grafana folders: Agents={bool(roots)}, children={sorted(children)}, misplaced={sorted(wrong)}")
    print("ok   Grafana files them under Agents/Fleet and Agents/Thurbox")
    for uid in ("prometheus", "tempo", "loki"):
        until(f"Grafana reaches data source {uid}",
              lambda: (lambda r: (r[0] == 200, r))(http(f"{GRAFANA}/api/datasources/uid/{uid}/health")), deadline)

    # Every dashboard query, as Grafana would send it with All selected, over
    # the window the synthetic data covers; Tempo searches over the last ten
    # minutes. TraceQL metrics on spans Tempo has not flushed yet come back
    # empty, so those only have to run.
    values = {"__rate_interval": "5m", "__interval": "10m", "__range": f"{HOURS}h", "job": ".*", "host": ".*",
              "agent": ".*", "session": ".*", "service": "fleet", "object": ".+"}
    failed = []
    for path in sorted((ROOT / "dashboards").glob("*/*.json")):
        for p in panels(json.loads(path.read_text())["panels"]):
            for t in p.get("targets", []):
                kind = t["datasource"]["type"]
                if kind == "prometheus":
                    ok, r = has_result(query(PROMETHEUS, "/api/v1/query_range", query=grafana_vars(t["expr"], values),
                                             step=60, **window))
                elif kind == "loki":
                    ok, r = has_result(query(LOKI, "/loki/api/v1/query_range", query=grafana_vars(t["expr"], values),
                                             step=600, **window))
                elif t.get("metricsQueryType"):
                    r = query(TEMPO, "/api/metrics/query_range", q=grafana_vars(t["query"], values), step=600, **window)
                    ok = r[0] == 200
                else:
                    # Errors and unanswered polls are rare: the last ten
                    # minutes need not have one.
                    r = query(TEMPO, "/api/search", q=grafana_vars(t["query"], values), **recent)
                    ok = r[0] == 200 and (bool(r[1].get("traces")) or p["title"] not in ("Object reconciles", "Effects"))
                print(f"{'ok  ' if ok else 'FAIL'} {path.stem} / {p['title']} {t['refId']}")
                if not ok:
                    failed.append(f"{path.stem} / {p['title']}: {str(r)[:300]}")
    if failed:
        sys.exit("FAIL dashboard queries without data:\n" + "\n".join(failed))
    print("smoke: the stack ingests Fleet's, Thurbox's and the agents' telemetry and every dashboard query returns data")


if __name__ == "__main__":
    main()
