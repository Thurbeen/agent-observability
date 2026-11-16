# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml==6.0.3"]
# ///
"""The lint rejects what Fleet and Thurbox do not export: uv run tests/test_lint.py"""

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("lint", ROOT / "tools" / "lint.py")
lint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lint)
CATALOG = lint.Catalog([json.loads((ROOT / "catalog" / f"{p}.json").read_text()) for p in lint.PRODUCTS])


def promql(expr: str, recorded: frozenset = frozenset()) -> list[str]:
    lint.problems.clear()
    lint.check_promql("test", expr, CATALOG, set(recorded))
    return list(lint.problems)


def logql(expr: str) -> list[str]:
    lint.problems.clear()
    lint.check_logql("test", expr, CATALOG)
    return list(lint.problems)


def traceql(query: str) -> list[str]:
    lint.problems.clear()
    lint.check_traceql("test", query, CATALOG)
    return list(lint.problems)


class PromQL(unittest.TestCase):
    def test_accepts_exported_names(self):
        self.assertEqual(promql('sum by (fleet_controller) (rate(fleet_reconcile_duration_seconds_count'
                                '{job=~"$job",fleet_outcome=~"error|leaseLost"}[5m]))'), [])
        self.assertEqual(promql('sum by (fleet_phase) (fleet_tasks{fleet_phase=~"Stalled|Releasing"})'), [])
        self.assertEqual(promql('fleet_sessions{fleet_session_state="Lost"}'), [])

    def test_rejects_unknown_metric(self):
        self.assertTrue(promql("rate(fleet_tasks_total[5m])"))

    def test_rejects_unexported_suffix(self):
        self.assertTrue(promql("fleet_inbox_depth_total"))

    def test_rejects_unknown_label(self):
        self.assertTrue(promql('fleet_effects_total{fleet_state="Releasing"}'))

    def test_rejects_unknown_label_value(self):
        self.assertTrue(promql('fleet_forge_calls_total{fleet_outcome=~"known|notloggedin"}'))
        self.assertTrue(promql('fleet_sessions{fleet_session_state="lost"}'))

    def test_rejects_unrecorded_series(self):
        self.assertTrue(promql("job:fleet_missing:rate5m > 0"))
        self.assertEqual(promql("job:fleet_x:rate5m > 0", frozenset({"job:fleet_x:rate5m"})), [])


class ThurboxPromQL(unittest.TestCase):
    def test_rejects_the_session_metrics_v3_removed(self):
        self.assertTrue(promql('sum by (state) (thurbox_sessions{job="thurbox"})'))
        self.assertTrue(promql("thurbox_turn_duration_seconds_bucket"))

    def test_rejects_the_agent_labels_v3_removed(self):
        self.assertTrue(promql('claude_code_token_usage_tokens_total{thurbox_agent!=""}'))

    def test_accepts_the_task_fleet_adds_to_each_agent(self):
        self.assertEqual(promql('claude_code_token_usage_tokens_total{fleet_task!="",type="cacheRead"}'), [])


class LogQL(unittest.TestCase):
    def test_accepts_the_interface_log_lines(self):
        self.assertEqual(logql('{service_name="thurbox"} | detected_level =~ "warn|error"'), [])

    def test_rejects_the_session_events_v3_removed(self):
        self.assertTrue(logql('{service_name="thurbox"} |= "thurbox.session.gone"'))
        self.assertTrue(logql('{service_name="thurbox"} | line_format "{{.thurbox_session_name}}"'))


class TraceQL(unittest.TestCase):
    def test_accepts_exported_spans(self):
        self.assertEqual(traceql('{ resource.service.name = "$service" && name = "forge poll"'
                                 ' && span.fleet.forge.host != nil && span.fleet.name =~ "$object" }'), [])

    def test_rejects_unknown_span_name(self):
        self.assertTrue(traceql('{ name = "reconcile" }'))

    def test_rejects_unknown_span_attribute(self):
        self.assertTrue(traceql("{ span.fleet.phase = \"Releasing\" }"))

    def test_accepts_the_interface_and_runtime_spans(self):
        self.assertEqual(traceql('{ resource.service.name = "thurbox" && name =~ "fleet .+" } '
                                 '| rate() by (span.fleet.command)'), [])
        self.assertEqual(traceql('{ name =~ "runtime-mux .+" && span.runtime.outcome = "unknown" }'), [])


class SpanCoverage(unittest.TestCase):
    def uncovered(self, *queries: str) -> list[str]:
        lint.problems.clear()
        lint.check_span_coverage(CATALOG, list(queries))
        return list(lint.problems)

    def test_names_each_span_no_dashboard_queries(self):
        problems = self.uncovered('{ name = "forge poll" }')
        self.assertIn('dashboards: no dashboard queries the span "runtime-mux X" a catalog lists', problems)
        self.assertNotIn('dashboards: no dashboard queries the span "forge poll" a catalog lists', problems)

    def test_a_regex_on_the_name_covers_its_spans(self):
        problems = self.uncovered('{ name =~ "runtime .+" }', '{ name =~ "workspace-git .+" }')
        self.assertNotIn('dashboards: no dashboard queries the span "runtime X" a catalog lists', problems)
        self.assertNotIn('dashboards: no dashboard queries the span "workspace-git X" a catalog lists', problems)
        self.assertIn('dashboards: no dashboard queries the span "runtime-mux X" a catalog lists', problems)
class GatewayDashboard(unittest.TestCase):
    def test_gateway_panels_include_every_gateway_controller(self):
        board = json.loads((ROOT / "dashboards" / "fleet" / "fleet-gateway.json").read_text())
        exprs = [t["expr"] for p in lint.panels(board["panels"]) if p.get("title", "").startswith(("Gateway", "Mean Gateway"))
                 for t in p.get("targets", [])]
        self.assertTrue(exprs)
        for expr in exprs:
            for controller in ("ConversationController", "ListenerController", "GatewayController"):
                self.assertIn(controller, expr)


class Repository(unittest.TestCase):
    def test_repository_is_clean(self):
        lint.problems.clear()
        lint.check_dashboards(CATALOG, lint.check_rules(CATALOG))
        self.assertEqual(lint.problems, [])


if __name__ == "__main__":
    unittest.main()
