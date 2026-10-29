# /// script
# requires-python = ">=3.12"
# ///
"""Send realistic synthetic Fleet and Thurbox telemetry to a collector.

    uv run tools/synthetic.py                         # the last 6 hours
    uv run tools/synthetic.py --hours 1 --collector http://localhost:4318
    uv run tools/synthetic.py --follow                # then one minute a minute

It simulates one Fleet manager and one Thurbox instance minute by minute
and sends what each would have exported, oldest first, over OTLP/HTTP:

- Fleet: cumulative metrics every 60 s and reconcile, effect, forge-poll
  and command spans;
- Thurbox: one `automation tick` a minute, with delta metrics, the session
  gauge and one log record per session event, and a few diagnostic logs;
- the agents Thurbox launches: Claude Code's token and cost counters and
  Codex's per-turn token histogram, under the thurbox.* resource attributes
  Thurbox adds. The opencode and pi sessions export nothing, as those agents
  have no usage telemetry.

Every metric, label value, span and log event name comes from catalog/, so
nothing here can drift from what the products export. A fixed seed makes
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
# The OpenTelemetry SDK's default histogram bounds, which Fleet v2.4 uses for
# a duration in seconds.
SDK_BOUNDS = [0, 5, 10, 25, 50, 75, 100, 250, 500, 750, 1000, 2500, 5000, 7500, 10000]
# Thurbox's DURATION_BOUNDS (src/telemetry/mod.rs), in seconds.
THURBOX_BOUNDS = [1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600, 7200, 28800]


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
    # Objects per pass and the mean reconcile in seconds.
    LOAD = {"TaskController": (9, 0.045), "ChangeRequestController": (4, 0.42), "ForgeController": (2, 0.12),
            "ConversationController": (3, 0.02), "ListenerController": (1, 0.008)}
    TASKS = ["docs-refresh", "fix-flaky-test", "renovate-sweep", "release-notes", "api-pagination",
             "cache-eviction", "ci-speedup", "otel-upgrade", "login-timeout"]

    def __init__(self, rng: random.Random, start: int, steps: int) -> None:
        self.rng, self.start, self.steps = rng, start, steps
        self.cat = load("fleet")
        self.version = self.cat["fleet"]["tag"].lstrip("v")
        self.reconciles: dict[tuple, list[float]] = defaultdict(list)
        self.effects: Counter = Counter()
        self.forge: Counter = Counter()
        self.inbox = 1
        # A forge throttles for 25 minutes, about four hours ago, and another
        # forge's login expires for a few minutes.
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

        def span(name: str, parent: str, start: int, dur_s: float, a: dict, error: bool = False) -> str:
            sid = rng.randbytes(8).hex()
            s = {"traceId": trace, "spanId": sid, "parentSpanId": parent, "name": name, "kind": 1,
                 "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + int(dur_s * 1e9)),
                 "attributes": attrs(a)}
            if error:
                s["status"] = {"code": 2, "message": "reconcile failed"}
            spans.append(s)
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
                    span(f"effect {effect}", sid, offset + 2 * 10**6, rng.uniform(0.05, 0.9),
                         {**ident, "fleet.effect": effect, "fleet.outcome": eo}, error=eo == "failed")
            offset += int(d * 1e9)
        # Rare outcomes, once each at a fixed point, so any run shows them.
        if i == int(self.steps * 0.5):
            self.effects[("deliver", "failed")] += 1
            self.reconciles[("TaskController", "error")].append(0.05)
        if i == int(self.steps * 0.55):
            self.effects[("deliver", "unknown")] += 1
        # The forge polls of the untraced pass.
        for k in range(self.LOAD["ChangeRequestController"][0]):
            self.forge_outcome(i, "github.com" if k % 3 else "gitlab.com")

        traces = [spans]
        if rng.random() < 0.15:  # a worker's fleet command, from its own session
            trace = secrets.token_hex(16)
            spans = []
            command = rng.choice(["result", "declare", "get", "list"])
            code = 0 if rng.random() > 0.05 else 1
            span(f"fleet {command}", "", t - 20 * 10**9, rng.uniform(0.05, 0.4),
                 {"fleet.command": command, "process.exit.code": code}, error=code != 0)
            traces.append(spans)

        self.inbox = max(0, min(6, self.inbox + rng.choice([-1, 0, 0, 0, 1])))
        return self.metrics(t), {"resourceSpans": [
            {"resource": self.resource(), "scopeSpans": [{"scope": {"name": "fleet"}, "spans": s}]} for s in traces]}

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

    def resource(self) -> dict:
        return {"attributes": attrs({"service.name": "fleet", "service.version": self.version,
                                     "host.name": "devbox"})}

    def metrics(self, t: int) -> dict:
        start, now = str(self.start), str(t)

        def point(labels: dict, **v) -> dict:
            return {"attributes": attrs(labels), "startTimeUnixNano": start, "timeUnixNano": now, **v}

        ms = [
            {"name": "fleet.reconcile.duration", "unit": "s", "histogram": {"aggregationTemporality": 2, "dataPoints": [
                point({"fleet.controller": c, "fleet.outcome": o}, **histogram_point(obs, SDK_BOUNDS))
                for (c, o), obs in sorted(self.reconciles.items())]}},
            {"name": "fleet.effects", "unit": "{effect}", "sum": {"aggregationTemporality": 2, "isMonotonic": True,
                "dataPoints": [point({"fleet.effect": e, "fleet.outcome": o}, asInt=str(n))
                               for (e, o), n in sorted(self.effects.items())]}},
            {"name": "fleet.forge.calls", "unit": "{call}", "sum": {"aggregationTemporality": 2, "isMonotonic": True,
                "dataPoints": [point({"fleet.forge.host": h, "fleet.outcome": o}, asInt=str(n))
                               for (h, o), n in sorted(self.forge.items())]}},
            {"name": "fleet.inbox.depth", "unit": "{entry}", "gauge": {"dataPoints": [point({}, asInt=str(self.inbox))]}},
        ]
        return {"resourceMetrics": [{"resource": self.resource(),
                                     "scopeMetrics": [{"scope": {"name": "fleet"}, "metrics": [m for m in ms if
                                         next(iter(v for k, v in m.items() if k not in ("name", "unit")))["dataPoints"]]}]}]}


class Session:
    def __init__(self, sid: str, name: str, agent: str, backend: str, created: int) -> None:
        self.id, self.name, self.agent, self.backend, self.created = sid, name, agent, backend, created
        self.state, self.since, self.until = "working", created, created


class Thurbox:
    """One Thurbox instance: its `automation tick` once a minute."""

    NAMES = ["fix-login-timeout", "docs-config-pass", "flaky-e2e-hunt", "otel-dashboards", "renovate-batch",
             "api-pagination", "release-2-78", "perf-render-loop", "remote-hosts-ssh", "cli-json-output",
             "theme-contrast", "session-restore", "hook-latency", "worktree-gc", "search-index"]
    AGENTS = [("claude", 0.45), ("codex", 0.25), ("opencode", 0.15), ("pi", 0.15)]
    WARNINGS = [
        "Control mode error, reconnecting: connection reset by peer",
        "could not list windows on devbox: ssh: connect to host devbox port 22: Connection timed out",
        "could not publish the perf snapshot: database is locked",
        "auto-update failed: the release feed did not answer",
    ]

    def __init__(self, rng: random.Random, start: int, steps: int) -> None:
        self.rng, self.steps = rng, steps
        self.cat = load("thurbox")
        self.version = self.cat["thurbox"]["tag"].lstrip("v")
        self.states = values(self.cat, "thurbox.sessions", "state")
        self.reasons = values(self.cat, "thurbox.session.lifecycle", "reason")
        self.events = set(self.cat["logs"]["events"])
        self.claude, self.codex = load("claude-code"), load("codex")
        self.usage: dict[str, tuple[Session, list[dict[str, int]]]] = {}
        self.sessions: list[Session] = []
        self.counter = 0
        self.last = start
        self.unreachable = range(int(steps * 0.55), int(steps * 0.55) + 18)
        for k in range(9):
            s = self.new_session(start - rng.randint(5, 240) * MINUTE)
            s.state, s.since = rng.choice(["working", "done", "idle", "blocked", "working"]), start
            s.until = start + int(rng.uniform(1, 20) * MINUTE)

    def new_session(self, at: int) -> Session:
        r, acc = self.rng.random(), 0.0
        agent = self.AGENTS[-1][0]
        for name, p in self.AGENTS:
            acc += p
            if r < acc:
                agent = name
                break
        backend = "ssh:devbox" if self.rng.random() < 0.3 else "local-tmux"
        name = self.NAMES[self.counter % len(self.NAMES)]
        self.counter += 1
        s = Session(f"{self.rng.randbytes(4).hex()}-{self.rng.randbytes(2).hex()}-4{self.rng.randbytes(2).hex()[1:]}"
                    f"-a{self.rng.randbytes(2).hex()[1:]}-{self.rng.randbytes(6).hex()}", name, agent, backend, at)
        self.sessions.append(s)
        return s

    def dwell(self, state: str) -> float:
        """Minutes a session stays in a state."""
        median = {"working": 6, "blocked": 7, "done": 12, "idle": 25, "background": 4}.get(state, 30)
        return min(self.rng.lognormvariate(math.log(median), 0.8), 180)

    def step(self, i: int, t: int) -> tuple[dict, dict]:
        rng, events = self.rng, []
        measures: list[tuple[str, dict, float]] = []

        def event(s: Session, kind: str, reason: str, at: int, **extra) -> None:
            name = {"created": "thurbox.session.created", "gone": "thurbox.session.gone"}.get(
                kind, "thurbox.session.state_changed" if reason == "state" else "thurbox.session.changed")
            if name not in self.events:
                sys.exit(f"synthetic: {name} is not a Thurbox log event")
            events.append((at, name, s, extra if reason == "state" else {"reason": pick(self.reasons, reason), **extra}))
            if reason == "state":
                measures.append(("thurbox.session.transitions", {"agent": s.agent, **extra}, 1))
            else:
                measures.append(("thurbox.session.lifecycle", {"agent": s.agent, "reason": reason}, 1))
            if reason == "exited":
                measures.append(("thurbox.session.exits", {"agent": s.agent, "exit_code": extra["exit_code"]}, 1))
            if kind == "gone":
                measures.append(("thurbox.session.lifetime", {"agent": s.agent, "reason": reason},
                                 (at - s.created) / 1e9))

        def move(s: Session, to: str, at: int) -> None:
            secs = (at - s.since) / 1e9
            if s.state == "working":
                measures.append(("thurbox.turn.duration", {"agent": s.agent, "outcome": pick(self.states, to)}, secs))
                self.turn_usage(s, secs)
            elif s.state == "blocked":
                measures.append(("thurbox.blocked.duration", {"agent": s.agent}, secs))
            event(s, "changed", "state", at, **{"from": pick(self.states, s.state), "to": pick(self.states, to)})
            s.state, s.since, s.until = to, at, at + int(self.dwell(to) * MINUTE)

        # New sessions, about one every 25 minutes.
        if rng.random() < 0.04 or len(self.sessions) < 5:
            at = t - rng.randint(1, 59) * 10**9
            s = self.new_session(at)
            event(s, "created", "spawned", at)
            event(s, "changed", "state", at + 2 * 10**9, to="working")
            s.since, s.until = at, at + int(self.dwell("working") * MINUTE)

        for s in list(self.sessions):
            at = t - rng.randint(1, 58) * 10**9
            if s.state in ("lost", "failed", "stopped") or s.until > t:
                if s.state == "failed" and s.until <= t:
                    event(s, "changed", "relaunched", at)
                    s.state, s.since, s.until = "working", at, at + int(self.dwell("working") * MINUTE)
                continue
            if s.state == "working":
                r = rng.random()
                if i == int(self.steps * 0.7) and s.backend == "local-tmux" and not any(
                        x.state == "lost" for x in self.sessions):
                    event(s, "changed", "lost", at)  # its pane went with nobody asking
                    s.state = "lost"
                    continue
                move(s, "done" if r < 0.6 else "blocked" if r < 0.85 else "idle" if r < 0.95 else "background", at)
            elif s.state in ("blocked", "background"):
                move(s, "working", at)
            elif s.state == "done":
                if rng.random() < 0.15:
                    event(s, "changed", "exited", at, exit_code=0 if rng.random() < 0.85 else 1)
                    event(s, "gone", "soft_deleted", at + 3 * 10**9)
                    self.sessions.remove(s)
                else:
                    move(s, "working" if rng.random() < 0.7 else "idle", at)
            elif s.state == "idle":
                move(s, "working", at)
        if i == int(self.steps * 0.4):  # one launch fails and is relaunched
            s = self.new_session(t - 30 * 10**9)
            event(s, "created", "spawned", t - 30 * 10**9)
            event(s, "changed", "launch_failed", t - 25 * 10**9)
            s.state, s.until = "failed", t + 8 * MINUTE

        gauge = Counter()
        groups = {(s.agent, s.backend) for s in self.sessions}
        for s in self.sessions:
            remote_down = s.backend.startswith("ssh:") and i in self.unreachable
            gauge[(s.agent, s.backend, "unreachable" if remote_down else s.state)] += 1
        diagnostics = []
        if rng.random() < 0.03 or i in (self.unreachable.start, self.unreachable.start + 4):
            text = self.WARNINGS[1] if i in self.unreachable else rng.choice(self.WARNINGS)
            diagnostics.append((t - rng.randint(1, 59) * 10**9, text))
        metrics = self.metrics(t, measures, [((a, b, st), gauge[(a, b, st)]) for a, b in sorted(groups)
                                             for st in self.states])
        metrics["resourceMetrics"] += self.agent_metrics(t)
        self.last = t
        return metrics, self.logs(sorted(events, key=lambda e: e[0]), diagnostics)

    def turn_usage(self, s: Session, secs: float) -> None:
        """One turn's tokens, the way the session's agent reports them."""
        rng, minutes = self.rng, max(secs / 60, 0.5)
        if s.agent == "claude":
            types = values(self.claude, "claude_code.token.usage", "type")
            self.turn(s, {
                pick(types, "input"): int(rng.uniform(800, 4000) * minutes ** 0.5),
                pick(types, "cacheRead"): int(rng.uniform(30_000, 90_000) * minutes ** 0.7),
                pick(types, "cacheCreation"): int(rng.uniform(2_000, 12_000) * minutes ** 0.5),
                pick(types, "output"): int(rng.uniform(400, 2_500) * minutes ** 0.6),
            })
        elif s.agent == "codex":
            types = values(self.codex, "codex.turn.token_usage", "token_type")
            given = int(rng.uniform(25_000, 80_000) * minutes ** 0.7)
            output = int(rng.uniform(500, 3_000) * minutes ** 0.6)
            # Codex counts cached input inside input, and reasoning inside output.
            self.turn(s, {
                pick(types, "input"): given, pick(types, "cached_input"): int(given * rng.uniform(0.75, 0.92)),
                pick(types, "cache_write_input"): 0, pick(types, "output"): output,
                pick(types, "reasoning_output"): int(output * rng.uniform(0.3, 0.6)),
                pick(types, "total"): given + output,
            })

    def turn(self, s: Session, tokens: dict[str, int]) -> None:
        self.usage.setdefault(s.id, (s, []))[1].append(tokens)

    def agent_metrics(self, t: int) -> list[dict]:
        """This minute's agent telemetry, one resource per session, as deltas."""
        start, now, out = str(self.last), str(t), []
        for s, turns in self.usage.values():
            tokens = {k: sum(t[k] for t in turns) for k in turns[0]}
            resource = {"thurbox.session.id": s.id, "thurbox.session.name": s.name, "thurbox.agent": s.agent}
            if s.agent == "claude":
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
                service = {"service.name": "claude-code", "service.version": "2.1.312"}
            else:
                metrics = [{"name": "codex.turn.token_usage", "histogram": {"aggregationTemporality": 1, "dataPoints": [
                    {"attributes": attrs({"token_type": k}), "startTimeUnixNano": start, "timeUnixNano": now,
                     **histogram_point([float(t[k]) for t in turns], SDK_BOUNDS)} for k in tokens]}}]
                service = {"service.name": "codex_cli_rs", "service.version": "0.166.0"}
            for m in metrics:
                if m["name"] not in {c["name"] for c in self.claude["metrics"] + self.codex["metrics"]}:
                    sys.exit(f"synthetic: {m['name']} is not in an agent catalog")
            out.append({"resource": {"attributes": attrs({**service, **resource})},
                        "scopeMetrics": [{"scope": {"name": service["service.name"]}, "metrics": metrics}]})
        self.usage = {}
        return out

    def resource(self) -> dict:
        return {"attributes": attrs({"service.name": "thurbox", "service.version": self.version,
                                     "host.name": "workstation", "thurbox.instance": "thurbox"})}

    def metrics(self, t: int, measures: list, gauge: list) -> dict:
        start, now = str(self.last), str(t)
        sums: dict[str, Counter] = defaultdict(Counter)
        hists: dict[str, dict] = defaultdict(lambda: defaultdict(list))
        for name, labels, v in measures:
            key = tuple(sorted(labels.items()))
            if name.endswith(("duration", "lifetime")):
                hists[name][key].append(v)
            else:
                sums[name][key] += 1
        units = {m["name"]: m["unit"] for m in self.cat["metrics"]}
        out = []
        for name, series in sorted(sums.items()):
            out.append({"name": name, "unit": units[name], "sum": {"aggregationTemporality": 1, "isMonotonic": True,
                "dataPoints": [{"attributes": attrs(dict(k)), "startTimeUnixNano": start, "timeUnixNano": now,
                                "asInt": str(n)} for k, n in sorted(series.items())]}})
        for name, series in sorted(hists.items()):
            out.append({"name": name, "unit": units[name], "histogram": {"aggregationTemporality": 1,
                "dataPoints": [{"attributes": attrs(dict(k)), "startTimeUnixNano": start, "timeUnixNano": now,
                                **histogram_point(obs, THURBOX_BOUNDS)} for k, obs in sorted(series.items())]}})
        out.append({"name": "thurbox.sessions", "unit": units["thurbox.sessions"], "gauge": {"dataPoints": [
            {"attributes": attrs({"state": st, "agent": a, "backend": b}), "timeUnixNano": now, "asInt": str(n)}
            for (a, b, st), n in gauge]}})
        return {"resourceMetrics": [{"resource": self.resource(),
                                     "scopeMetrics": [{"scope": {"name": "thurbox"}, "metrics": out}]}]}

    def logs(self, events: list, diagnostics: list) -> dict:
        records = [{"timeUnixNano": str(at), "observedTimeUnixNano": str(at), "severityNumber": 9,
                    "severityText": "INFO", "eventName": name, "body": {"stringValue": name},
                    "attributes": attrs({"thurbox.session.id": s.id, "thurbox.session.name": s.name,
                                         "agent": s.agent, **detail})}
                   for at, name, s, detail in events]
        warnings = [{"timeUnixNano": str(at), "observedTimeUnixNano": str(at), "severityNumber": 13,
                     "severityText": "WARN", "body": {"stringValue": text}} for at, text in diagnostics]
        scopes = [{"scope": {"name": "thurbox"}, "logRecords": records}]
        if warnings:
            scopes.append({"scope": {"name": "thurbox"}, "logRecords": warnings})
        return {"resourceLogs": [{"resource": self.resource(), "scopeLogs": scopes}]}


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
    fleet, thurbox = Fleet(rng, start - 10 * MINUTE, steps), Thurbox(rng, start, steps)

    def tick(i: int) -> None:
        t = start + i * MINUTE
        metrics, traces = fleet.step(i, t)
        send(a.collector, "metrics", metrics)
        send(a.collector, "traces", traces)
        metrics, logs = thurbox.step(i, t)
        send(a.collector, "metrics", metrics)
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
