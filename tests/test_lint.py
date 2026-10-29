# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml==6.0.3"]
# ///
"""The lint rejects what Fleet does not export: uv run tests/test_lint.py"""

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("lint", ROOT / "tools" / "lint.py")
lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lint)
CATALOG = lint.Catalog(json.loads((ROOT / "catalog" / "fleet.json").read_text()))


def promql(expr: str, recorded: frozenset = frozenset()) -> list[str]:
    lint.problems.clear()
    lint.check_promql("test", expr, CATALOG, set(recorded))
    return list(lint.problems)


def traceql(query: str) -> list[str]:
    lint.problems.clear()
    lint.check_traceql("test", query, CATALOG)
    return list(lint.problems)


class PromQL(unittest.TestCase):
    def test_accepts_exported_names(self):
        self.assertEqual(promql('sum by (fleet_controller) (rate(fleet_reconcile_duration_seconds_count'
                                '{job=~"$job",fleet_outcome=~"error|leaseLost"}[5m]))'), [])

    def test_rejects_unknown_metric(self):
        self.assertTrue(promql("rate(fleet_tasks_total[5m])"))

    def test_rejects_unexported_suffix(self):
        self.assertTrue(promql("fleet_inbox_depth_total"))

    def test_rejects_unknown_label(self):
        self.assertTrue(promql('fleet_effects_total{fleet_phase="Releasing"}'))

    def test_rejects_unknown_label_value(self):
        self.assertTrue(promql('fleet_forge_calls_total{fleet_outcome=~"known|notloggedin"}'))

    def test_rejects_unrecorded_series(self):
        self.assertTrue(promql("job:fleet_missing:rate5m > 0"))
        self.assertEqual(promql("job:fleet_x:rate5m > 0", frozenset({"job:fleet_x:rate5m"})), [])


class TraceQL(unittest.TestCase):
    def test_accepts_exported_spans(self):
        self.assertEqual(traceql('{ resource.service.name = "$service" && name = "forge poll"'
                                 ' && span.fleet.forge.host != nil && span.fleet.name =~ "$object" }'), [])

    def test_rejects_unknown_span_name(self):
        self.assertTrue(traceql('{ name = "reconcile" }'))

    def test_rejects_unknown_span_attribute(self):
        self.assertTrue(traceql("{ span.fleet.phase = \"Releasing\" }"))


class Repository(unittest.TestCase):
    def test_repository_is_clean(self):
        lint.problems.clear()
        lint.check_dashboards(CATALOG, lint.check_rules(CATALOG))
        self.assertEqual(lint.problems, [])


if __name__ == "__main__":
    unittest.main()
