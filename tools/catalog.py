# /// script
# requires-python = ">=3.12"
# ///
"""Derive the telemetry Fleet exports from Fleet's source at a pinned tag.

The catalog in catalog/fleet.json is the source of truth for every check in
this repository: a dashboard or a rule may only name a metric, a label, a
label value, a span name or a span attribute that the catalog lists.

    uv run tools/catalog.py            # fail if catalog/fleet.json is stale
    uv run tools/catalog.py --write    # rewrite it from the pinned tag
    uv run tools/catalog.py --write --tag v2.5.0   # move to another tag
    uv run tools/catalog.py --source ~/src/fleet   # read a local checkout

The parser reads three files and fails loudly when their shape changes, so a
Fleet refactor shows up here instead of as an empty dashboard.
"""

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog" / "fleet.json"
REPO = "Thurbeen/fleet"
FILES = {
    "telemetry": "crates/fleet-manager/src/telemetry.rs",
    "manager": "crates/fleet-manager/src/lib.rs",
    "plan": "crates/fleet-api/src/plan.rs",
    "export": "crates/fleet-telemetry/src/lib.rs",
}


def fetch(tag: str, source: Path | None) -> dict[str, str]:
    if source:
        return {k: (source / p).read_text() for k, p in FILES.items()}
    out = {}
    for key, path in FILES.items():
        url = f"https://raw.githubusercontent.com/{REPO}/{tag}/{path}"
        with urllib.request.urlopen(url, timeout=30) as r:
            out[key] = r.read().decode()
    return out


def body(src: str, fn: str) -> str:
    """The brace-balanced body of `fn NAME(`."""
    m = re.search(rf"\bfn {fn}\b[^{{]*\{{", src)
    if not m:
        sys.exit(f"catalog: fn {fn} not found; Fleet's telemetry changed shape")
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
        for verb, inline, arg in re.findall(r'(?<!error\()format!\(\s*"(\w+) \{(\w*)\}"(?:\s*,\s*([\w.]+))?', src)
    ]


def need(values: list, what: str) -> list:
    if not values:
        sys.exit(f"catalog: found no {what}; Fleet's telemetry changed shape")
    return sorted(set(values))


def prometheus_names(name: str, instrument: str, unit: str) -> list[str]:
    """The series names the OTLP-to-Prometheus translation produces."""
    base = name.replace(".", "_")
    suffix = {"s": "_seconds", "ms": "_milliseconds", "By": "_bytes"}.get(unit, "")
    if unit and not unit.startswith("{") and not suffix:
        sys.exit(f"catalog: unit {unit!r} of {name} has no known Prometheus suffix")
    base += suffix
    if instrument == "histogram":
        return [base + s for s in ("_bucket", "_count", "_sum")]
    if instrument == "counter":
        return [base + "_total"]
    return [base]


def derive(tag: str, src: dict[str, str]) -> dict:
    tel = src["telemetry"]
    instruments = re.findall(
        r'(\w+):\s*meter\s*\.\s*(?:f64|u64|i64)_(histogram|counter|gauge)\(\s*"([^"]+)"\s*\)'
        r'\s*\.with_unit\(\s*"([^"]*)"\s*\)',
        tel,
    )
    need(instruments, "instruments")
    attrs = {
        field: re.findall(r'KeyValue::new\(\s*"([^"]+)"', kvs)
        for field, kvs in re.findall(
            r"instruments\(\)\s*\.(\w+)\s*\.\s*(?:record|add)\(\s*[^,]+,\s*&\[(.*?)\]\s*,?\s*\)",
            tel,
            re.S,
        )
    }

    controllers = need(
        re.findall(r'telemetry::reconcile\(\s*"(\w+)"', src["manager"]), "controllers"
    )
    effects = need(
        literals(body(src["plan"], "name")) + literals(body(tel, "effect_kind")),
        "effect kinds",
    )
    values = {
        "fleet.reconcile.duration": {
            "fleet.controller": controllers,
            "fleet.outcome": need(literals(body(tel, "reconcile")), "reconcile outcomes"),
        },
        "fleet.effects": {
            "fleet.effect": effects,
            "fleet.outcome": need(literals(body(tel, "effect_outcome")), "effect outcomes"),
        },
        "fleet.forge.calls": {
            "fleet.forge.host": [],  # any forge host name
            "fleet.outcome": need(
                literals(body(tel, "forge_poll")) + literals(body(tel, "uncertainty")),
                "forge outcomes",
            ),
        },
    }

    metrics = []
    for field, instrument, name, unit in instruments:
        if field not in attrs:
            sys.exit(f"catalog: no recording of instrument {field}")
        labels = sorted(attrs[field])
        known = values.get(name, {})
        if sorted(known) != labels and known:
            sys.exit(f"catalog: {name} records {labels}, expected {sorted(known)}")
        metrics.append(
            {
                "name": name,
                "instrument": instrument,
                "unit": unit,
                "attributes": {a: known.get(a, []) for a in labels},
                "prometheus": {
                    "series": prometheus_names(name, instrument, unit),
                    "labels": [a.replace(".", "_") for a in labels],
                },
            }
        )

    span_names = need(
        re.findall(r'Current::start\(\s*"([^"]+)"', tel)
        + formatted(tel)
        + formatted(src["export"]),
        "span names",
    )
    span_attrs = need(
        re.findall(r'KeyValue::new\(\s*"([^"]+)"', tel)
        # Only span attributes: the export crate also builds the resource.
        + re.findall(r'set_attribute\(KeyValue::new\(\s*"([^"]+)"', src["export"]),
        "span attributes",
    )
    return {
        "fleet": {"repository": f"https://github.com/{REPO}", "tag": tag},
        "metrics": metrics,
        "spans": {"names": span_names, "attributes": span_attrs},
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tag", help="Fleet tag to read (default: the catalog's tag)")
    p.add_argument("--source", type=Path, help="read a local Fleet checkout instead of GitHub")
    p.add_argument("--write", action="store_true", help="rewrite catalog/fleet.json")
    a = p.parse_args()
    current = json.loads(CATALOG.read_text()) if CATALOG.exists() else None
    tag = a.tag or (current and current["fleet"]["tag"])
    if not tag:
        sys.exit("catalog: no catalog yet; pass --tag")
    text = json.dumps(derive(tag, fetch(tag, a.source)), indent=2) + "\n"
    if a.write:
        CATALOG.write_text(text)
        print(f"catalog: wrote {CATALOG.relative_to(ROOT)} from Fleet {tag}")
    elif current is None or CATALOG.read_text() != text:
        sys.exit(f"catalog: {CATALOG.relative_to(ROOT)} differs from Fleet {tag}; run with --write")
    else:
        print(f"catalog: {CATALOG.relative_to(ROOT)} matches Fleet {tag}")


if __name__ == "__main__":
    main()
