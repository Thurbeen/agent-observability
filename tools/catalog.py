# /// script
# requires-python = ">=3.12"
# ///
"""Derive the telemetry Fleet, Thurbox and the agents export from a pinned source.

The catalogs in catalog/ are the source of truth for every check in this
repository: a dashboard or a rule may only name a metric, a label, a label
value, a span name, a span attribute or a log event that a catalog lists.
Fleet, Thurbox and Codex are read from their source at a tag. Claude Code is
closed source: its catalog is read from its monitoring documentation, which
has no tag, so a change there shows up as a stale catalog.

    uv run tools/catalog.py                    # fail if a catalog is stale
    uv run tools/catalog.py --write            # rewrite all from their pinned tags
    uv run tools/catalog.py --write fleet --tag v2.5.0     # move one to another tag
    uv run tools/catalog.py thurbox --source ~/src/thurbox # read a local checkout

The parsers read a few source files and fail loudly when their shape
changes, so a refactor shows up here instead of as an empty dashboard.
"""

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = {
    "fleet": {
        "manager": "crates/fleet-manager/src/telemetry.rs",
        "runtime": "crates/fleet-runtime/src/telemetry.rs",
        "controllers": "crates/fleet-manager/src/lib.rs",
        "cron": "crates/fleet-manager/src/cron_tasks.rs",
        "verbs": "crates/fleet-manager/src/verbs.rs",
        "plan": "crates/fleet-api/src/plan.rs",
        "phases": "crates/fleet-api/src/task.rs",
        "sessions": "crates/fleet-api/src/session.rs",
        "harness": "crates/fleet-api/src/harness.rs",
        "cli": "crates/fleet-cli/src/main.rs",
        "runtime_mux": "crates/runtime-mux/src/main.rs",
        "workspace_git": "crates/workspace-git/src/main.rs",
        "docs": "docs/observability.md",
    },
    "thurbox": {
        "telemetry": "crates/thurbox/src/telemetry/mod.rs",
        "link": "crates/thurbox/src/fleet_link.rs",
        "otel": "crates/otel-env/src/lib.rs",
    },
    "codex": {
        "names": "codex-rs/otel/src/metrics/names.rs",
        "tokens": "codex-rs/core/src/state/turn_token_usage.rs",
    },
    "claude-code": {"docs": "monitoring-usage.md"},
}
SOURCES = {
    "fleet": "https://raw.githubusercontent.com/Thurbeen/thurbox/{tag}/{path}",
    "thurbox": "https://raw.githubusercontent.com/Thurbeen/thurbox/{tag}/{path}",
    "codex": "https://raw.githubusercontent.com/openai/codex/{tag}/{path}",
    "claude-code": "https://code.claude.com/docs/en/{path}",
}
REPOSITORY = "https://github.com/Thurbeen/thurbox"


def fetch(product: str, tag: str, source: Path | None) -> dict[str, str]:
    files = FILES[product]
    if source:
        return {k: (source / p).read_text() for k, p in files.items()}
    out = {}
    for key, path in files.items():
        url = SOURCES[product].format(tag=tag, path=path)
        req = urllib.request.Request(url, headers={"User-Agent": "agent-observability-catalog"})
        with urllib.request.urlopen(req, timeout=30) as r:
            out[key] = r.read().decode()
    return out


def body(src: str, fn: str) -> str:
    """The brace-balanced body of `fn NAME(` or `impl NAME`."""
    m = re.search(rf"\b{fn if fn.startswith('impl ') else 'fn ' + fn}\b[^{{]*\{{", src)
    if not m:
        sys.exit(f"catalog: {fn} not found; the telemetry source changed shape")
    depth, i = 1, m.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        i += 1
    return src[m.end() : i - 1]


def literals(text: str) -> list[str]:
    """The string literals that match arms return: `=> "value"`."""
    return re.findall(r'=>\s*"([^"]+)"', text)


def formatted(src: str) -> list[str]:
    """Span names built as `format!("verb {x}")` or `format!("verb {}", a.x)`;
    an error status's text is not a span name."""
    return [
        f"{verb} {{{inline or arg.split('.')[-1]}}}"
        for verb, inline, arg in re.findall(r'(?<!error\()format!\(\s*"([\w-]+) \{(\w*)\}"(?:\s*,\s*([\w.]+))?', src)
    ]


def keys(src: str) -> list[str]:
    return re.findall(r'KeyValue::new\(\s*"([^"]+)"', src)


def variants(src: str, enum: str) -> list[str]:
    """The variants listed in `pub const ALL: &[Enum] = &[...]`."""
    m = re.search(rf"const ALL: &\[{enum}\] = &\[(.*?)\];", src, re.S)
    return re.findall(rf"{enum}::(\w+)", m.group(1)) if m else []


def need(values: list, what: str) -> list:
    if not values:
        sys.exit(f"catalog: found no {what}; the telemetry source changed shape")
    return sorted(set(values))


def prometheus_names(name: str, instrument: str, unit: str) -> list[str]:
    """The series names the OTLP-to-Prometheus translation produces."""
    base = name.replace(".", "_")
    suffix = {"s": "_seconds", "ms": "_milliseconds", "By": "_bytes", "tokens": "_tokens", "USD": "_USD"}.get(unit, "")
    if unit and not unit.startswith("{") and not suffix:
        sys.exit(f"catalog: unit {unit!r} of {name} has no known Prometheus suffix")
    base += suffix
    if instrument == "histogram":
        return [base + s for s in ("_bucket", "_count", "_sum")]
    if instrument == "counter":
        return [base + "_total"]
    return [base]


def derive_fleet(tag: str, src: dict[str, str]) -> dict:
    """Fleet's metrics and the spans of the programs it runs, from the
    thurbox monorepo. Names, kinds and units come from the code; the
    attributes of each metric from the table in docs/observability.md,
    checked against the keys the code sets."""
    code = src["manager"] + src["runtime"]
    instruments = {
        name: (k, unit)
        for k, name, unit in re.findall(
            r'\.(?:f64|u64|i64)_(histogram|counter|gauge)\(\s*"([^"]+)"\s*\)\s*\.with_unit\(\s*"([^"]*)"\s*\)', code
        )
    }
    need(list(instruments), "instruments")
    rows = re.findall(r"^\| `(fleet\.[\w.]+)` \| (\w+) \| `([^`]*)` \| (.+?) \|$", src["docs"], re.M)
    documented = {n: (k, u, re.findall(r"`([\w.]+)`", a)) for n, k, u, a in need(rows, "metric table")}
    if sorted(documented) != sorted(instruments):
        sys.exit(f"catalog: docs/observability.md lists {sorted(documented)}, the code builds {sorted(instruments)}")

    controllers = need(
        re.findall(r'telemetry::reconcile\(\s*"(\w+)"', src["controllers"] + src["cron"]), "controllers"
    )
    create = re.search(r'pub const CREATE_ACTION: &str = "(\w+)"', src["plan"])
    effects = need(
        literals(body(src["plan"], "name")) + literals(body(src["manager"], "effect_kind"))
        + ([create.group(1)] if create else []),
        "effect kinds",
    )
    runtime_outcomes = re.findall(r'if known \{ "(\w+)" \} else \{ "(\w+)" \}', src["runtime"])
    values = {
        "fleet.reconcile.duration": {
            "fleet.controller": controllers,
            "fleet.outcome": need(literals(body(src["manager"], "reconcile")), "reconcile outcomes"),
        },
        "fleet.effects": {
            "fleet.effect": effects,
            "fleet.outcome": need(literals(body(src["manager"], "effect_outcome")), "effect outcomes"),
        },
        "fleet.forge.calls": {
            "fleet.outcome": need(
                literals(body(src["manager"], "forge_poll")) + literals(body(src["manager"], "uncertainty")),
                "forge outcomes",
            ),
        },
        "fleet.runtime.call.duration": {
            "fleet.outcome": need([o for pair in runtime_outcomes for o in pair], "runtime call outcomes"),
        },
        "fleet.task.amendments": {
            "fleet.outcome": need(literals(body(src["verbs"], "amend")), "amendment outcomes"),
        },
        "fleet.tasks": {"fleet.phase": need(variants(src["phases"], "Phase"), "Task phases")},
        "fleet.sessions": {"fleet.session.state": need(variants(src["sessions"], "SessionState"), "session states")},
    }

    metrics = []
    for name, (k, unit, attrs) in sorted(documented.items()):
        if (k, unit) != instruments[name]:
            sys.exit(f"catalog: {name} is documented as {k} {unit}, built as {instruments[name]}")
        if not all(f'"{a}"' in code for a in attrs):
            sys.exit(f"catalog: {name} documents {sorted(attrs)}, which the code never sets")
        known = values.get(name, {})
        metrics.append(metric(name, k, unit, {a: known.get(a, []) for a in attrs}))

    programs = src["cli"] + src["runtime_mux"] + src["workspace_git"]
    span_names = need(
        re.findall(r'Current::start\(\s*"([^"]+)"', src["manager"])
        + formatted(src["manager"]) + formatted(src["runtime"])
        + [n for n in formatted(programs) if re.fullmatch(r"fleet \{name\}|(runtime-mux|workspace-git) .+", n)],
        "span names",
    )
    span_attrs = need(keys(src["manager"]) + keys(src["runtime"]) + keys(programs), "span attributes")
    agent = body(src["harness"], "telemetry")
    return {
        "fleet": {"repository": REPOSITORY, "tag": tag},
        "metrics": metrics,
        "spans": {"names": span_names, "attributes": span_attrs},
        "agent_resource": need(re.findall(r'\(\s*"([\w.]+)",\s*[\w.]+', agent), "agent resource attributes"),
    }


def derive_thurbox(tag: str, src: dict[str, str]) -> dict:
    """The thurbox interface's own telemetry (ADR-38): no metrics, a client
    span around each Fleet command it runs, and its WARN-and-above log
    lines, under service.name thurbox."""
    tel, link = src["telemetry"], src["link"]
    if re.search(r"_(counter|gauge|histogram)\(", tel + link):
        sys.exit("catalog: the thurbox interface builds an instrument; derive its metrics")
    service = re.search(r'otel_env::init\(\s*"(\w+)"', tel)
    if not service or not re.search(r'KeyValue::new\("service\.version"', src["otel"]):
        sys.exit("catalog: the thurbox interface's resource changed shape")
    return {
        "thurbox": {"repository": REPOSITORY, "tag": tag},
        "service": service.group(1),
        "metrics": [],
        "spans": {"names": need(formatted(" ".join(re.findall(r"span_builder\((format!\([^)]*\))", link))), "span names"),
                  "attributes": need(keys(link), "span attributes")},
        "resource": need(["service.name", "service.version"] + keys(tel), "resource attributes"),
    }


def metric(name: str, instrument: str, unit: str, attributes: dict[str, list[str]]) -> dict:
    return {
        "name": name,
        "instrument": instrument,
        "unit": unit,
        "attributes": {a: sorted(set(v)) for a, v in sorted(attributes.items())},
        "prometheus": {"series": prometheus_names(name, instrument, unit),
                       "labels": sorted(underscore(a) for a in attributes)},
    }


def underscore(name: str) -> str:
    return name.replace(".", "_")


def derive_codex(tag: str, src: dict[str, str]) -> dict:
    """Codex's per-turn token histogram (codex-rs), with no unit."""
    name = re.search(r'TURN_TOKEN_USAGE_METRIC: &str = "([^"]+)"', src["names"])
    if not name or "TURN_TOKEN_USAGE_METRIC" not in src["tokens"]:
        sys.exit("catalog: Codex's turn token metric changed shape")
    types = need(re.findall(r'\(\s*"(\w+)",\s*usage\.', src["tokens"]), "Codex token types")
    return {
        "codex": {"repository": "https://github.com/openai/codex", "tag": tag},
        "metrics": [metric(name.group(1), "histogram", "", {"token_type": types})],
    }


def derive_claude_code(tag: str, src: dict[str, str]) -> dict:
    """Claude Code's token and cost counters, from its documentation."""
    docs = src["docs"]
    rows = dict(re.findall(r"^\| `(claude_code\.(?:token|cost)\.usage)` \| [^|]+\| (\w+) +\|$", docs, re.M))
    if sorted(rows) != ["claude_code.cost.usage", "claude_code.token.usage"]:
        sys.exit(f"catalog: Claude Code's metric table changed shape: {rows}")
    types = re.search(r"^\* `type`: \(((?:`\"\w+\"`,? ?)+)\)\. The `\"input\"` type excludes", docs, re.M)
    if not types:
        sys.exit("catalog: Claude Code's token types changed shape")
    return {
        "claude-code": {"documentation": SOURCES["claude-code"].format(path=FILES["claude-code"]["docs"]),
                        "tag": tag},
        "metrics": [
            metric("claude_code.cost.usage", "counter", rows["claude_code.cost.usage"], {}),
            metric("claude_code.token.usage", "counter", rows["claude_code.token.usage"],
                   {"type": re.findall(r'"(\w+)"', types.group(1))}),
        ],
    }


DERIVE = {"fleet": derive_fleet, "thurbox": derive_thurbox, "codex": derive_codex, "claude-code": derive_claude_code}


def check(product: str, tag: str | None, source: Path | None, write: bool) -> bool:
    path = ROOT / "catalog" / f"{product}.json"
    current = json.loads(path.read_text()) if path.exists() else None
    tag = tag or (current and current[product]["tag"])
    if not tag:
        sys.exit(f"catalog: no {path.name} yet; pass --tag")
    text = json.dumps(DERIVE[product](tag, fetch(product, tag, source)), indent=2) + "\n"
    where = path.relative_to(ROOT)
    if write:
        path.write_text(text)
        print(f"catalog: wrote {where} from {product} {tag}")
    elif current is None or path.read_text() != text:
        print(f"catalog: {where} differs from {product} {tag}; run with --write", file=sys.stderr)
        return False
    else:
        print(f"catalog: {where} matches {product} {tag}")
    return True


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("product", nargs="?", choices=sorted(DERIVE), help="one product (default: both)")
    p.add_argument("--tag", help="tag to read (default: the catalog's tag)")
    p.add_argument("--source", type=Path, help="read a local checkout instead of GitHub")
    p.add_argument("--write", action="store_true", help="rewrite the catalog")
    a = p.parse_args()
    if (a.tag or a.source) and not a.product:
        p.error("--tag and --source need a product")
    products = [a.product] if a.product else sorted(DERIVE)
    if not all([check(product, a.tag, a.source, a.write) for product in products]):
        sys.exit(1)


if __name__ == "__main__":
    main()
