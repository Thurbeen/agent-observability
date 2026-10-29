# Development

The tools need [uv](https://docs.astral.sh/uv/) only. Each script declares
its own dependencies.

```sh
uv run tools/dashboards.py --write   # regenerate dashboards/ after editing the generator
uv run tools/lint.py                 # check dashboards and rules against the catalogs
uv run tools/catalog.py              # check the catalogs against Fleet's and Thurbox's source
uv run tests/test_lint.py            # lint unit tests
uv run tests/smoke.py                # against a running quick-start stack
prek install                         # run the local checks on every commit
```

Edit `tools/dashboards.py`, not the JSON. CI fails when `dashboards/` differs
from the generator's output.

## Catalogs

`catalog/` lists every metric, label, label value, span and log event that
Fleet, Thurbox, Codex and Claude Code export. `tools/catalog.py` derives
Fleet, Thurbox and Codex from their source on GitHub at a pinned tag, and
Claude Code from its monitoring documentation, which has no tag. CI derives
them again on every change. `tools/lint.py` rejects any dashboard or rule
that names something a catalog does not list.

To move to a new release:

1. Run `uv run tools/catalog.py --write fleet --tag vX.Y.Z` (or `thurbox`, or
   `codex --tag rust-vX.Y.Z`).
2. Run `uv run tools/lint.py`. It lists every query that names something the
   new release does not export.
3. Update the compatibility table in the README.

## Synthetic data and screenshots

`tools/synthetic.py` simulates one Fleet manager and one Thurbox instance and
sends what they would export, oldest first, through the collector. It draws
every name from the catalogs, and a fixed seed makes it reproducible. The
smoke test uses it too.

The screenshots in `docs/screenshots/` come from `tools/screenshots.py`. It
runs the generator in follow mode against a fresh quick-start stack, waits
until Tempo serves TraceQL metrics on the backfilled spans, and captures
each dashboard in Grafana's kiosk mode, dark theme, 1600 px wide. It fails
when any panel shows "No data".

```sh
docker compose -f quickstart/compose.yaml down -v
docker compose -f quickstart/compose.yaml up -d
uv run --with playwright==1.64.1 playwright install chromium
uv run tools/screenshots.py
```

Pass `--chromium /usr/bin/chromium` to use an installed Chromium, and
`--collector`, `--grafana` and `--tempo` when the stack runs on other ports.
