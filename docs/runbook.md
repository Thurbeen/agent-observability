# Runbook

Each section matches one alert in `rules/fleet.rules.yml` or
`rules/thurbox.rules.yml`.

- **Fleet:** the `job` label names the Fleet manager. Run every `fleet`
  command with the same `--config` as that manager.
- **Thurbox:** `job` is always `thurbox`, and `host_name` names the machine
  when Prometheus promotes `host.name`. Run `thurbox-cli` on that machine.

## FleetNotReconciling

The manager recorded no object reconcile for 10 minutes, or its metrics
stopped arriving. `fleet up` sweeps every second by default, and VEGA's Task
always exists, so a healthy manager never goes quiet.

1. Check that `fleet up` runs, for example with your service manager's status
   command. Read its stderr for `warning: telemetry is off`.
2. If `fleet up` runs, check the export path. The collector must be up, and
   `OTEL_EXPORTER_OTLP_ENDPOINT` must point at it with an `http://` URL.
3. If the metrics arrive but stay flat, look for a stuck reconcile in the
   **Fleet traces** dashboard. A `reconcile pass` span that never ends shows a
   blocked sweep.

The alert stops one day after the last sample. A manager that is gone for
good stops alerting then.

## FleetReconcileErrors

More than 5% of object reconciles ended with the outcome `error` for 15
minutes. An error is a store or adapter failure, not an Unknown observation.

1. Open **Fleet overview**, panel *Reconcile problems*, to find the
   controller.
2. Open **Fleet traces**, panel *Errors*, for the failing objects.
3. Read the manager's stderr for the error text. Fleet never exports error
   text as telemetry.

## FleetLeaseLost

A reconcile ended because another process holds the manager lease. Two
managers share one store, or a manager paused longer than its lease.

1. Make sure exactly one `fleet up` runs for each store.
2. If one manager runs, check the host for long pauses, such as suspend or
   swap pressure. Raise `--lease-ttl-ms` if the pauses are expected.

## FleetEffectsFailing

An effect ended with the outcome `failed`. The `fleet_effect` label names
the effect: spawn, brief, deliver, release or markReady.

1. Find the object in **Fleet traces**, panel *Effects*. Filter on the
   failed outcome.
2. Run `fleet get task NAME` and read its conditions.
3. A failed spawn or release usually comes from the runtime (the session
   host). A failed markReady comes from the forge.

## FleetEffectOutcomesUnknown

Fleet could not observe the outcome of three or more effects in 30 minutes.
Fleet persists each effect's intent first, and it recovers a missing receipt
by observing the effect's key. It never repeats the effect blindly. Repeated
unknown outcomes mean the runtime or the forge does not answer.

1. Check that the runtime host is reachable: the Task condition
   `HostReachable`.
2. Find the effects in **Fleet traces**, panel *Effects*.

## FleetForgeThrottled

A forge throttled Fleet's change polls for about 30 minutes. Fleet defers
the next polls to that host, so ChangeRequest updates arrive late.

1. Open **Fleet forge polling** to see the call rate per host.
2. Check whether other tools share the same forge account and its rate
   limit.
3. Reduce the number of open ChangeRequests that Fleet follows, or wait for
   the limit to reset.

## FleetForgeNotLoggedIn

Change polls fail with `notLoggedIn` or `refused`. These outcomes do not
recover on their own.

1. Check the forge CLI login on the manager's host, for example
   `gh auth status`, as the user that runs `fleet up`.
2. Check that the account can read the repository of the change.

## FleetForgeUnanswered

More than half of the polls to one forge host got no answer for 15 minutes:
`timeout`, `incomplete`, `unreadable` or `unavailable`. Fleet treats each
one as Unknown, never as absence, so no ChangeRequest moves on a missing
answer.

1. Check the forge's status page and the network path from the manager.
2. Open **Fleet forge polling**, panel *Polls by host and outcome*, for the
   exact outcome.

## FleetInboxBacklog

Inbox entries waited for about an hour. Fleet delivers an entry only when the
Task's session is idle. A worker that never goes idle keeps its entries.

1. Fleet v2.4 exports the depth without a Task attribute. Find the Task in
   **Fleet traces**, panel *Effects*, with `effect deliver` spans, or run
   `fleet list task`.
2. Check the Task's session. If it is busy on purpose, the backlog is
   expected.

## ThurboxSessionsLost

A session's pane went with nobody asking, for 5 minutes. A multiplexer crash
takes every pane on its server. On psmux, three relaunches within ten minutes
also end in `lost`.

1. Run `thurbox-cli session list` and find the sessions in state `lost`.
2. Run `thurbox-cli session restart <id>` to bring one back, or
   `thurbox-cli session delete <id>` when it is no longer needed.
3. If every local session went lost at once, check that the multiplexer
   server still runs. A `watch` started with another `TMUX_TMPDIR` finds no
   server and reports every local session lost.

## ThurboxLaunchFailed

A session's agent died before it reported anything, for 5 minutes. Nothing
runs in the session.

1. Open **Thurbox session events** and find the session's
   `changed launch_failed` line for its name and agent.
2. Run `thurbox-cli session list --json`. The session's record holds the
   launch's exit code and output. Fix the cause: a missing agent binary, a
   bad argument in `agents.toml`, or a login the agent needs.
3. Run `thurbox-cli session restart <id>`.

## ThurboxHostUnreachable

Thurbox could not reach a remote host for 15 minutes. Its sessions show as
`unreachable` placeholders. They are not lost: they come back when the host
answers.

1. Check that the host is up and that `ssh <host>` works from this machine
   with the settings in `hosts.toml`.
2. The **Interface warnings** panel of **Thurbox session events** shows the
   connection errors when `export_diagnostic_logs` is on.

## ThurboxWaitingOnHuman

Sessions have been blocked on a human, without a break, for an hour. An agent
waits on a permission prompt or a question. This alert is `info`: route it to
yourself, not to a pager.

1. Open **Thurbox sessions**, panel *Sessions by state*, and Thurbox's
   session list to find the blocked sessions.
2. Answer the prompt in each session.
