# /// script
# requires-python = ">=3.12"
# ///
"""Send realistic synthetic Fleet and Thurbox telemetry to a collector.

    uv run tools/synthetic.py                         # the last 6 hours
    uv run tools/synthetic.py --hours 1 --collector http://localhost:4318
    uv run tools/synthetic.py --follow                # then one minute a minute

It simulates one Fleet manager and one thurbox interface minute by minute
and sends what each would have exported, oldest first, over OTLP/HTTP:

- Fleet: cumulative metrics every 60 s, with the Tasks by phase and their
  sessions by state, and reconcile, effect, runtime, forge-poll and command
  spans;
- the agents Fleet launches: Claude Code's token and cost counters and
  Codex's per-turn token histogram, under the fleet.task and fleet.uid
  resource attributes Fleet adds. The opencode and pi Tasks export nothing,
  as those agents have no usage telemetry;
- the thurbox interface: a client span around each fleet command it runs,
  and a few WARN log lines.

Every metric, label value and span name comes from catalog/, so nothing
here can drift from what the products export. A fixed seed makes
the run reproducible; only the clock moves.
"""

import argparse
import json
import math
import random
import secrets
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MINUTE = 60 * 10**9
SDK_BOUNDS = [0, 5, 10, 25, 50, 75, 100, 250, 500, 750, 1000, 2500, 5000, 7500, 10000]


def load(product: str) -> dict:
    return json.loads((ROOT / "catalog" / f"{product}.json").read_text())


def values(catalog: dict, metric: str, attr: str) -> list[str]:
    for m in catalog["metrics"]:
        if m["name"] == metric:
            return m["attributes"][attr]
    sys.exit(f"synthetic: {metric} is not in the catalog")


def pick(choices: list[str], value: str) -> str:
    """A label value, checked against the catalog."""
    if value not in choices:
        sys.exit(f"synthetic: {value!r} is not one of {choices}")
    return value


def attrs(d: dict) -> list[dict]:
    out = []
    for k, v in d.items():
        if v is None:
            continue
        out.append({"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": str(v)}})
    return out


def histogram_point(observations: list[float], bounds: list[float]) -> dict:
    buckets = [0] * (len(bounds) + 1)
    for v in observations:
        buckets[next((i for i, b in enumerate(bounds) if v <= b), len(bounds))] += 1
    return {"count": str(len(observations)), "sum": sum(observations), "explicitBounds": bounds,
            "bucketCounts": [str(b) for b in buckets]}


class Fleet:
    """One Fleet v2 manager: cumulative metrics, spans."""

    KINDS = {"TaskController": "Task", "ChangeRequestController": "ChangeRequest", "ForgeController": "Forge",
             "ConversationController": "Conversation", "ListenerController": "Listener"}
    LOAD = {"TaskController": (9, 0.045), "ChangeRequestController": (4, 0.42), "ForgeController": (2, 0.12),
            "ConversationController": (3, 0.02), "ListenerController": (1, 0.008)}
    TASKS = ["docs-refresh", "fix-flaky-test", "renovate-sweep", "release-notes", "api-pagination",
             "cache-eviction", "ci-speedup", "otel-upgrade", "login-timeout"]
    AGENTS = ["claude", "claude", "codex", "claude", "opencode", "codex", "claude", "pi", "claude"]
    VERBS = ["start", "deliver", "sessions", "release", "stop"]

    def __init__(self, rng: random.Random, start: int, steps: int) -> None:
        self.rng, self.start, self.steps = rng, start, steps
        self.cat = load("fleet")
        self.claude, self.codex = load("claude-code"), load("codex")
        self.version = self.cat["fleet"]["tag"].lstrip("v")
        self.reconciles: dict[tuple, list[float]] = defaultdict(list)
        self.effects: Counter = Counter()
        self.forge: Counter = Counter()
        self.amendments: Counter = Counter({"appended": 1})
        self.runtime: dict[tuple, list[float]] = defaultdict(list)
        self.usage: dict[str, list[dict[str, int]]] = defaultdict(list)
        self.last = start
        self.inbox = 1
        self.phases = values(self.cat, "fleet.tasks", "fleet.phase")
        self.states = values(self.cat, "fleet.sessions", "fleet.session.state")
        self.stalled = range(int(steps * 0.4), steps + 1)
        self.lost = range(int(steps * 0.7), int(steps * 0.7) + 9)
        self.throttle = range(int(steps * 0.3), int(steps * 0.3) + 25)
        self.logged_out = range(int(steps * 0.8), int(steps * 0.8) + 4)

    def outcome(self, controller: str) -> str:
        r = self.rng.random()
        o = "unchanged"
        if r < 0.0004:
            o = "error"
        elif r < 0.0012:
            o = "conflict"
        elif r < 0.00125:
            o = "leaseLost"
        elif r < 0.12 if controller in ("TaskController", "ConversationController") else r < 0.05:
            o = "changed"
        return pick(values(self.cat, "fleet.reconcile.duration", "fleet.outcome"), o)

    def step(self, i: int, t: int) -> tuple[dict, dict]:
        rng, spans = self.rng, []
        trace = secrets.token_hex(16)
        callees: dict[str, list[dict]] = {"runtime-mux": [], "workspace-git": []}

        def span(name: str, parent: str, start: int, dur_s: float, a: dict, error: bool = False,
                 into: list[dict] | None = None) -> str:
            sid = rng.randbytes(8).hex()
            s = {"traceId": trace, "spanId": sid, "parentSpanId": parent, "name": name, "kind": 1,
                 "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + int(dur_s * 1e9)),
                 "attributes": attrs(a)}
            if error:
                s["status"] = {"code": 2, "message": "reconcile failed"}
            (spans if into is None else into).append(s)
            return sid

        # Two passes a minute; the second one is traced.
        pass_start = t - 30 * 10**9
        offset = pass_start + 2 * 10**6
        pass_total, children = 0.0, []
        for controller, (n, mean) in self.LOAD.items():
            for k in range(n * 2):
                o = self.outcome(controller)
                d = rng.lognormvariate(math.log(mean), 0.5)
                self.reconciles[(controller, o)].append(d)
                if k < n:
                    children.append((controller, o, d, k))
                    pass_total += d
        root = span("reconcile pass", "", pass_start, pass_total + 0.01, {"fleet.pass": i + 1})
        for controller, o, d, k in children:
            kind = self.KINDS[controller]
            name = self.TASKS[(i + k) % len(self.TASKS)] if kind in ("Task", "ChangeRequest") else f"{kind.lower()}-{k}"
            ident = {"fleet.kind": kind, "fleet.name": name, "fleet.uid": f"u{1000 + k}-{kind.lower()}",
                     "fleet.generation": 1 + (i // 40) % 5}
            sid = span(f"reconcile {kind}", root, offset, d,
                       {**ident, "fleet.controller": controller, "fleet.outcome": o}, error=o == "error")
            if kind == "ChangeRequest":
                host = "github.com" if k % 3 else "gitlab.com"
                fo = self.forge_outcome(i, host)
                span("forge poll", sid, offset + 10**6, d * 0.9, {"fleet.forge.host": host, "fleet.outcome": fo},
                     error=fo not in ("known", "absent"))
            if kind == "Task" and o == "changed":
                effect, eo = self.effect(i)
                if effect:
                    d_effect = rng.uniform(0.05, 0.9)
                    esid = span(f"effect {effect}", sid, offset + 2 * 10**6, d_effect,
                                {**ident, "fleet.effect": effect, "fleet.outcome": eo}, error=eo == "failed")
                    verb, ro = self.runtime_call(effect, eo, d_effect * 0.8)
                    rsid = span(f"runtime {verb}", esid, offset + 3 * 10**6, d_effect * 0.8,
                                {"fleet.runtime": "runtime-mux", "fleet.runtime.verb": verb, "fleet.outcome": ro},
                                error=ro == "unknown")
                    msid = span(f"runtime-mux {verb}", rsid, offset + 4 * 10**6, d_effect * 0.7,
                                {"runtime.verb": verb, "runtime.outcome": ro}, error=ro == "unknown",
                                into=callees["runtime-mux"])
                    workspace_verb = {"start": "make", "release": "release"}.get(verb)
                    if workspace_verb:
                        span(f"workspace-git {workspace_verb}", msid, offset + 5 * 10**6, d_effect * 0.4,
                             {"workspace.verb": workspace_verb}, into=callees["workspace-git"])
            offset += int(d * 1e9)
        # Rare outcomes, once each at a fixed point, so any run shows them.
        if i == int(self.steps * 0.5):
            self.effects[("deliver", "failed")] += 1
            self.reconciles[("TaskController", "error")].append(0.05)
        if i == int(self.steps * 0.55):
            self.effects[("deliver", "unknown")] += 1
            self.amendments["conflict"] += 1
        if rng.random() < 0.03:
            self.amendments[pick(values(self.cat, "fleet.task.amendments", "fleet.outcome"), "appended")] += 1
        for k in range(self.LOAD["ChangeRequestController"][0]):
            self.forge_outcome(i, "github.com" if k % 3 else "gitlab.com")
        for _ in range(len(self.TASKS)):
            self.runtime_call("probe", "", rng.lognormvariate(math.log(0.03), 0.4))
        for k, task in enumerate(self.TASKS):
            if rng.random() < 0.3:
                self.turn_usage(task, self.AGENTS[k], rng.uniform(1, 12))

        traces = [spans]
        if rng.random() < 0.15:
            trace = secrets.token_hex(16)
            spans = []
            command = rng.choice(["result", "declare", "get", "list"])
            code = 0 if rng.random() > 0.05 else 1
            span(f"fleet {command}", "", t - 20 * 10**9, rng.uniform(0.05, 0.4),
                 {"fleet.command": command, "process.exit.code": code}, error=code != 0)
            traces.append(spans)

        self.inbox = max(0, min(6, self.inbox + rng.choice([-1, 0, 0, 0, 1])))
        metrics = self.metrics(t)
        metrics["resourceMetrics"] += self.agent_metrics(t)
        self.last = t
        return metrics, {"resourceSpans": [
            {"resource": self.resource(), "scopeSpans": [{"scope": {"name": "fleet"}, "spans": s}]} for s in traces] + [
            {"resource": self.resource(service), "scopeSpans": [{"scope": {"name": service}, "spans": s}]}
            for service, s in callees.items() if s]}

    def forge_outcome(self, i: int, host: str) -> str:
        r = self.rng.random()
        if i in self.throttle and host == "github.com":
            o = "throttled" if r < 0.6 else "known"
        elif i in self.logged_out and host == "gitlab.com":
            o = "notLoggedIn"
        else:
            o = "known" if r < 0.86 else "absent" if r < 0.985 else "timeout" if r < 0.995 else "unavailable"
        self.forge[(host, pick(values(self.cat, "fleet.forge.calls", "fleet.outcome"), o))] += 1
        return o

    def effect(self, i: int) -> tuple[str | None, str]:
        r = self.rng.random()
        if r < 0.25:
            effect, o = "deliver", "delivered" if self.rng.random() < 0.93 else "already"
        elif r < 0.33:
            effect, o = "spawn", "spawned"
        elif r < 0.43:
            effect, o = "brief", "delivered"
        elif r < 0.5:
            effect, o = "release", "released"
        elif r < 0.53:
            effect, o = "markReady", "markedReady"
        else:
            return None, ""
        if self.rng.random() < 0.03:
            o = "failed" if self.rng.random() < 0.6 else "unknown"
        self.effects[(pick(values(self.cat, "fleet.effects", "fleet.effect"), effect),
                      pick(values(self.cat, "fleet.effects", "fleet.outcome"), o))] += 1
        return effect, o

    def runtime_call(self, effect: str, outcome: str, secs: float) -> tuple[str, str]:
        verb = {"spawn": "start", "brief": "deliver", "deliver": "deliver", "release": "release",
                "probe": "sessions"}.get(effect, "sessions")
        o = "unknown" if outcome in ("failed", "unknown") else "known"
        self.runtime[(pick(self.VERBS, verb), pick(values(self.cat, "fleet.runtime.call.duration", "fleet.outcome"),
                                                   o))].append(secs)
        return verb, o

    def turn_usage(self, task: str, agent: str, minutes: float) -> None:
        """One turn's tokens, the way the Task's agent reports them."""
        rng = self.rng
        if agent == "claude":
            types = values(self.claude, "claude_code.token.usage", "type")
            self.usage[task].append({
                pick(types, "input"): int(rng.uniform(800, 4000) * minutes ** 0.5),
                pick(types, "cacheRead"): int(rng.uniform(30_000, 90_000) * minutes ** 0.7),
                pick(types, "cacheCreation"): int(rng.uniform(2_000, 12_000) * minutes ** 0.5),
                pick(types, "output"): int(rng.uniform(400, 2_500) * minutes ** 0.6),
            })
        elif agent == "codex":
            types = values(self.codex, "codex.turn.token_usage", "token_type")
            given = int(rng.uniform(25_000, 80_000) * minutes ** 0.7)
            output = int(rng.uniform(500, 3_000) * minutes ** 0.6)
            self.usage[task].append({
                pick(types, "input"): given, pick(types, "cached_input"): int(given * rng.uniform(0.75, 0.92)),
                pick(types, "cache_write_input"): 0, pick(types, "output"): output,
                pick(types, "reasoning_output"): int(output * rng.uniform(0.3, 0.6)),
                pick(types, "total"): given + output,
            })

    def agent_metrics(self, t: int) -> list[dict]:
        """This minute's agent telemetry, one resource per Task, as deltas."""
        start, now, out = str(self.last), str(t), []
        for task, turns in self.usage.items():
            agent = self.AGENTS[self.TASKS.index(task)]
            tokens = {k: sum(t[k] for t in turns) for k in turns[0]}
            resource = {"service.name": agent, "fleet.task": task, "fleet.uid": f"u{1000 + self.TASKS.index(task)}-task"}
            if set(resource) - {"service.name"} - set(self.cat["agent_resource"]):
                sys.exit("synthetic: a resource attribute Fleet does not add to an agent")
            if agent == "claude":
                cost = (tokens["input"] * 3 + tokens["cacheRead"] * 0.3 + tokens["cacheCreation"] * 3.75
                        + tokens["output"] * 15) / 1e6
                point = lambda labels, **v: {"attributes": attrs(labels), "startTimeUnixNano": start,
                                             "timeUnixNano": now, **v}
                metrics = [
                    {"name": "claude_code.token.usage", "unit": "tokens", "sum": {
                        "aggregationTemporality": 1, "isMonotonic": True,
                        "dataPoints": [point({"type": k}, asInt=str(v)) for k, v in tokens.items()]}},
                    {"name": "claude_code.cost.usage", "unit": "USD", "sum": {
                        "aggregationTemporality": 1, "isMonotonic": True, "dataPoints": [point({}, asDouble=cost)]}},
                ]
            else:
                metrics = [{"name": "codex.turn.token_usage", "histogram": {"aggregationTemporality": 1, "dataPoints": [
                    {"attributes": attrs({"token_type": k}), "startTimeUnixNano": start, "timeUnixNano": now,
                     **histogram_point([float(t[k]) for t in turns], SDK_BOUNDS)} for k in tokens]}}]
            for m in metrics:
                if m["name"] not in {c["name"] for c in self.claude["metrics"] + self.codex["metrics"]}:
                    sys.exit(f"synthetic: {m['name']} is not in an agent catalog")
            out.append({"resource": {"attributes": attrs(resource)},
                        "scopeMetrics": [{"scope": {"name": agent}, "metrics": metrics}]})
        self.usage = defaultdict(list)
        return out

    def task_phases(self, i: int) -> tuple[Counter, Counter]:
        phases, states = Counter(), Counter()
        for k in range(len(self.TASKS)):
            phase = ["Briefed", "Briefed", "Concluded", "Spawned", "Briefed", "Concluded", "Briefed", "Briefed",
                     "Releasing" if i % 30 < 3 else "Concluded"][k]
            if k == 7 and i in self.stalled:
                phase = "Stalled"
            phases[pick(self.phases, phase)] += 1
            if phase in ("Stalled", "Releasing"):
                continue
            state = "Running" if phase in ("Spawned", "Briefed") else "Idle"
            if k == 1 and i in self.lost:
                state = "Lost"
            if k == 5 and i % 90 > 60:
                state = "Paused"
            states[pick(self.states, state)] += 1
        return phases, states

    def resource(self, service: str = "fleet") -> dict:
        return {"attributes": attrs({"service.name": service, "service.version": self.version})}

    def metrics(self, t: int) -> dict:
        start, now = str(self.start), str(t)
        phases, states = self.task_phases((t - self.start) // MINUTE - 10)

        def point(labels: dict, **v) -> dict:
            return {"attributes": attrs(labels), "startTimeUnixNano": start, "timeUnixNano": now, **v}

        ms = [
            {"name": "fleet.reconcile.duration", "unit": "s", "histogram": {"aggregationTemporality": 2, "dataPoints": [
                point({"fleet.controller": c, "fleet.outcome": o}, **histogram_point(obs, SDK_BOUNDS))
                for (c, o), obs in sorted(self.reconciles.items())]}},
            {"name": "fleet.runtime.call.duration", "unit": "s", "histogram": {"aggregationTemporality": 2,
                "dataPoints": [point({"fleet.runtime": "runtime-mux", "fleet.runtime.verb": v, "fleet.outcome": o},
                                     **histogram_point(obs, SDK_BOUNDS))
                               for (v, o), obs in sorted(self.runtime.items())]}},
            {"name": "fleet.effects", "unit": "{effect}", "sum": {"aggregationTemporality": 2, "isMonotonic": True,
                "dataPoints": [point({"fleet.effect": e, "fleet.outcome": o}, asInt=str(n))
                               for (e, o), n in sorted(self.effects.items())]}},
            {"name": "fleet.forge.calls", "unit": "{call}", "sum": {"aggregationTemporality": 2, "isMonotonic": True,
                "dataPoints": [point({"fleet.forge.host": h, "fleet.outcome": o}, asInt=str(n))
                               for (h, o), n in sorted(self.forge.items())]}},
            {"name": "fleet.task.amendments", "unit": "{amendment}", "sum": {"aggregationTemporality": 2,
                "isMonotonic": True, "dataPoints": [point({"fleet.outcome": o}, asInt=str(n))
                                                    for o, n in sorted(self.amendments.items())]}},
            {"name": "fleet.inbox.depth", "unit": "{entry}", "gauge": {"dataPoints": [point({}, asInt=str(self.inbox))]}},
            {"name": "fleet.tasks", "unit": "{task}", "gauge": {"dataPoints": [
                point({"fleet.phase": ph}, asInt=str(phases[ph])) for ph in self.phases]}},
            {"name": "fleet.sessions", "unit": "{session}", "gauge": {"dataPoints": [
                point({"fleet.session.state": st}, asInt=str(states[st])) for st in self.states]}},
        ]
        return {"resourceMetrics": [{"resource": self.resource(),
                                     "scopeMetrics": [{"scope": {"name": "fleet"}, "metrics": [m for m in ms if
                                         next(iter(v for k, v in m.items() if k not in ("name", "unit")))["dataPoints"]]}]}]}


class Thurbox:
    """One thurbox interface: the fleet commands it runs, and its warnings."""

    COMMANDS = [("list", 0.5), ("version", 0.2), ("apply", 0.1), ("send", 0.1), ("delete", 0.05), ("get", 0.05)]
    WARNINGS = [
        "fleet watch exited (exit status: 1): the store is locked",
        "fleet version: the command timed out after 5s",
        "could not publish the perf snapshot: database is locked",
        "auto-update failed: the release feed did not answer",
    ]

    def __init__(self, rng: random.Random) -> None:
        self.rng, self.ticks = rng, 0
        self.cat = load("thurbox")
        self.version = self.cat["thurbox"]["tag"].lstrip("v")
        if "fleet {verb}" not in self.cat["spans"]["names"]:
            sys.exit("synthetic: the interface no longer spans its fleet commands")

    def step(self, t: int) -> tuple[dict, dict]:
        rng, spans = self.rng, []
        for _ in range(rng.randint(2, 6)):
            r, acc, command = rng.random(), 0.0, "list"
            for name, w in self.COMMANDS:
                acc += w
                if r < acc:
                    command = name
                    break
            begin = t - rng.randint(1, 59) * 10**9
            spans.append({"traceId": secrets.token_hex(16), "spanId": rng.randbytes(8).hex(), "parentSpanId": "",
                          "name": f"fleet {command}", "kind": 3, "startTimeUnixNano": str(begin),
                          "endTimeUnixNano": str(begin + int(rng.lognormvariate(math.log(0.08), 0.6) * 1e9)),
                          "attributes": attrs({"fleet.command": command})})
        warnings = []
        self.ticks += 1
        if rng.random() < 0.03 or self.ticks % 40 == 1:
            at = t - rng.randint(1, 59) * 10**9
            warnings.append({"timeUnixNano": str(at), "observedTimeUnixNano": str(at), "severityNumber": 13,
                             "severityText": "WARN", "body": {"stringValue": rng.choice(self.WARNINGS)}})
        return ({"resourceSpans": [{"resource": self.resource(), "scopeSpans": [
                    {"scope": {"name": "thurbox"}, "spans": spans}]}]},
                {"resourceLogs": [{"resource": self.resource(), "scopeLogs": [
                    {"scope": {"name": "thurbox"}, "logRecords": warnings}]}]})

    def resource(self) -> dict:
        return {"attributes": attrs({"service.name": self.cat["service"], "service.version": self.version,
                                     "thurbox.instance": "thurbox"})}


def send(collector: str, signal: str, body: dict) -> None:
    req = urllib.request.Request(f"{collector}/v1/{signal}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    for attempt in range(30):
        try:
            with urllib.request.urlopen(req, timeout=10):
                return
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            if attempt == 29:
                sys.exit(f"synthetic: the collector refused {signal}: {e}")
            time.sleep(2)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--collector", default="http://localhost:4318", help="the collector's OTLP/HTTP address")
    p.add_argument("--hours", type=float, default=6, help="how much history to send, ending now")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--follow", action="store_true", help="keep sending, one minute every minute")
    a = p.parse_args()
    steps = int(a.hours * 60)
    end = time.time_ns() // MINUTE * MINUTE
    start = end - steps * MINUTE
    rng = random.Random(a.seed)
    fleet, thurbox = Fleet(rng, start - 10 * MINUTE, steps), Thurbox(rng)

    def tick(i: int) -> None:
        t = start + i * MINUTE
        metrics, traces = fleet.step(i, t)
        send(a.collector, "metrics", metrics)
        send(a.collector, "traces", traces)
        traces, logs = thurbox.step(t)
        send(a.collector, "traces", traces)
        send(a.collector, "logs", logs)

    for i in range(steps + 1):
        tick(i)
    print(f"synthetic: sent {a.hours:g} h of Fleet and Thurbox telemetry to {a.collector}", flush=True)
    i = steps
    while a.follow:
        i += 1
        time.sleep(max(0, (start + i * MINUTE - time.time_ns()) / 1e9))
        tick(i)


if __name__ == "__main__":
    main()
