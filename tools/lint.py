# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml==6.0.3"]
# ///
"""Check every dashboard and rule against the Fleet telemetry catalog.

    uv run tools/lint.py                       # check, exit 1 on a problem
    uv run tools/lint.py --promql out.yml      # also write every dashboard
                                               # query as a rule file, for
                                               # `promtool check rules`

A query may name only a metric, label, label value, span name or span
attribute that catalog/fleet.json lists. Panels reach data sources through
the dashboard's template variables only. Each alert has a severity, a summary
and a runbook section.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATASOURCES = {"prometheus": "${prometheus}", "tempo": "${tempo}"}
SEVERITIES = {"critical", "warning", "info"}
# What Grafana substitutes before a query reaches Prometheus.
GRAFANA_VARS = {"$__rate_interval": "5m", "$__interval": "2m", "$__range": "1h"}

problems: list[str] = []


def problem(where: str, what: str) -> None:
    problems.append(f"{where}: {what}")


class Catalog:
    def __init__(self, data: dict) -> None:
        self.series = {s for m in data["metrics"] for s in m["prometheus"]["series"]}
        self.labels: dict[str, set[str]] = {}
        for m in data["metrics"]:
            for attr, values in m["attributes"].items():
                self.labels.setdefault(attr.replace(".", "_"), set()).update(values)
        self.span_attrs = set(data["spans"]["attributes"])
        self.span_examples = [re.sub(r"\{\w+\}", "X", n) for n in data["spans"]["names"]]


def check_promql(where: str, expr: str, cat: Catalog, recorded: set[str]) -> None:
    for token in re.findall(r"(?<![:\w])fleet_\w+(?![:\w])", expr):
        if token not in cat.series and token not in cat.labels:
            problem(where, f"{token} is not a metric or label Fleet exports")
    for token in re.findall(r"(?<![\w:])(\w*:[\w:]+)(?![\w:])", expr):
        if token not in recorded:
            problem(where, f"{token} is not a recorded series")
    for label, op, value in re.findall(r"\b(fleet_\w+)\s*(=~|!~|!=|=)\s*\"([^\"]*)\"", expr):
        allowed = cat.labels.get(label)
        if not allowed or "$" in value:
            continue
        alternatives = value.split("|") if "~" in op else [value]
        for alt in alternatives:
            if re.fullmatch(r"\w+", alt) and alt not in allowed:
                problem(where, f'{label} has no value "{alt}" (Fleet exports {sorted(allowed)})')


def check_traceql(where: str, query: str, cat: Catalog) -> None:
    for attr in re.findall(r"\bspan\.([\w.]+)", query):
        if attr not in cat.span_attrs:
            problem(where, f"span attribute {attr} is not one Fleet exports")
    for op, value in re.findall(r"(?<![\w.])name\s*(=~|!~|!=|=)\s*\"([^\"]+)\"", query):
        pattern = value if "~" in op else re.escape(value)
        if not any(re.fullmatch(pattern, ex) for ex in cat.span_examples):
            problem(where, f'span name "{value}" matches no span Fleet exports')


def panels(items: list[dict]):
    for p in items:
        yield p
        yield from panels(p.get("panels", []))


def check_dashboards(cat: Catalog, recorded: set[str]) -> list[str]:
    exprs, uids = [], set()
    files = sorted((ROOT / "dashboards").glob("*.json"))
    if not files:
        problem("dashboards", "no dashboards found")
    for path in files:
        where = path.relative_to(ROOT)
        try:
            d = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            problem(str(where), f"invalid JSON: {e}")
            continue
        uid = d.get("uid")
        if not uid or uid in uids:
            problem(str(where), f"missing or duplicate uid {uid!r}")
        uids.add(uid)
        if d.get("id") is not None:
            problem(str(where), "a provisioned dashboard must not carry a numeric id")
        variables = {v["name"]: v for v in d.get("templating", {}).get("list", [])}
        for v in variables.values():
            if v["type"] == "query":
                q = v["query"]["query"] if isinstance(v["query"], dict) else v["query"]
                check_promql(f"{where} variable {v['name']}", q, cat, recorded)
        for p in panels(d.get("panels", [])):
            name = f"{where} panel {p.get('title')!r}"
            sources = [p.get("datasource")] + [t.get("datasource") for t in p.get("targets", [])]
            for ds in filter(None, sources):
                kind = ds.get("type")
                if DATASOURCES.get(kind) != ds.get("uid"):
                    problem(name, f"data source {ds} is not the ${kind} template variable")
                elif kind not in variables:
                    problem(name, f"uses ${kind}, which the dashboard does not define")
            for t in p.get("targets", []):
                if "expr" in t:
                    check_promql(name, t["expr"], cat, recorded)
                    exprs.append(t["expr"])
                if t.get("queryType") == "traceql":
                    check_traceql(name, t["query"], cat)
    return exprs


def check_rules(cat: Catalog) -> set[str]:
    runbook = (ROOT / "docs" / "runbook.md").read_text()
    anchors = {h.strip().lower() for h in re.findall(r"^## (.+)$", runbook, re.M)}
    groups = []
    for path in sorted((ROOT / "rules").glob("*.rules.yml")):
        groups += [(path.relative_to(ROOT), g) for g in yaml.safe_load(path.read_text())["groups"]]
    recorded = {r["record"] for _, g in groups for r in g["rules"] if "record" in r}
    for where, g in groups:
        for r in g["rules"]:
            name = f"{where} {r.get('record') or r.get('alert')}"
            check_promql(name, r["expr"], cat, recorded)
            if "alert" not in r:
                continue
            if r.get("labels", {}).get("severity") not in SEVERITIES:
                problem(name, f"severity must be one of {sorted(SEVERITIES)}")
            notes = r.get("annotations", {})
            if not notes.get("summary"):
                problem(name, "no summary annotation")
            anchor = notes.get("runbook_url", "").rpartition("#")[2]
            if anchor != r["alert"].lower() or anchor not in anchors:
                problem(name, f"runbook_url must point at docs/runbook.md#{r['alert'].lower()}")
    return recorded


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--promql", type=Path, help="write the dashboard queries as a rule file")
    a = p.parse_args()
    cat = Catalog(json.loads((ROOT / "catalog" / "fleet.json").read_text()))
    recorded = check_rules(cat)
    exprs = check_dashboards(cat, recorded)
    if a.promql:
        rules = []
        for i, expr in enumerate(exprs):
            for var, value in GRAFANA_VARS.items():
                expr = expr.replace(var, value)
            rules.append({"record": f"lint:dashboard_query_{i}", "expr": re.sub(r"\$\w+", ".*", expr)})
        a.promql.write_text(yaml.safe_dump({"groups": [{"name": "dashboards", "rules": rules}]}, sort_keys=False))
    if problems:
        print("\n".join(problems), file=sys.stderr)
        sys.exit(f"lint: {len(problems)} problem(s)")
    print(f"lint: {len(exprs)} dashboard queries and the rules match Fleet {json.loads((ROOT / 'catalog' / 'fleet.json').read_text())['fleet']['tag']}")


if __name__ == "__main__":
    main()
