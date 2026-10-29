# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright==1.64.1"]
# ///
"""Capture every dashboard as a PNG in docs/screenshots/.

    docker compose -f quickstart/compose.yaml down -v   # start from empty storage
    docker compose -f quickstart/compose.yaml up -d
    uv run --with playwright==1.64.1 playwright install chromium
    uv run tools/screenshots.py      # or --chromium /usr/bin/chromium

It runs tools/synthetic.py --follow against the stack's collector, so six
hours of data end at the moment of capture. Each dashboard renders in Grafana's kiosk mode, dark theme, over the last
6 hours, 1600 px wide and as tall as its panels. The run fails when a panel
shows "No data", so a screenshot never has an empty panel. Tempo answers
TraceQL metrics on backfilled spans only once it has flushed them, which
takes a few minutes; the script waits for that first.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
WIDTH = 1600


def get(url: str) -> dict | list | None:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001 - a backend that is still starting
        return None


def wait(what: str, check, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            print(f"ok   {what}", flush=True)
            return
        time.sleep(5)
    sys.exit(f"FAIL {what}")


def tempo_ready(tempo: str) -> bool:
    end = int(time.time())
    q = urllib.parse.urlencode({"q": '{ name = "reconcile pass" } | quantile_over_time(duration, .5)',
                                "start": end - 6 * 3600, "end": end, "step": 600})
    r = get(f"{tempo}/api/metrics/query_range?{q}")
    return bool(r and r.get("series") and len(r["series"][0]["samples"]) > 30)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--collector", default="http://localhost:4318")
    p.add_argument("--grafana", default="http://localhost:3000")
    p.add_argument("--tempo", default="http://localhost:3200")
    p.add_argument("--chromium", help="a Chromium binary to use instead of Playwright's own")
    a = p.parse_args()

    wait("Grafana is up", lambda: get(f"{a.grafana}/api/health") is not None, 120)
    synthetic = subprocess.Popen([sys.executable, str(ROOT / "tools" / "synthetic.py"), "--follow",
                                  "--collector", a.collector], stdout=subprocess.PIPE, text=True)
    try:
        print(synthetic.stdout.readline().strip())
        capture(a)
    finally:
        synthetic.terminate()


def capture(a: argparse.Namespace) -> None:
    uids = sorted(json.loads(f.read_text())["uid"] for f in (ROOT / "dashboards").glob("*/*.json"))
    wait("Grafana provisioned every dashboard",
         lambda: {d["uid"] for d in get(f"{a.grafana}/api/search?tag=agent-observability") or []} >= set(uids), 120)
    wait("Tempo serves TraceQL metrics on the backfilled spans", lambda: tempo_ready(a.tempo), 1200)

    OUT.mkdir(parents=True, exist_ok=True)
    empty = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=a.chromium)
        page = browser.new_page(viewport={"width": WIDTH, "height": 1000}, device_scale_factor=1,
                                color_scheme="dark")
        for uid in uids:
            page.goto(f"{a.grafana}/d/{uid}?orgId=1&kiosk&theme=dark&from=now-6h&to=now&refresh=",
                      wait_until="networkidle")
            page.wait_for_selector("[data-viz-panel-key], .react-grid-item", timeout=60_000)
            # Grafana loads panels as they scroll into view: make the window as
            # tall as the dashboard, then let every panel load.
            height = page.evaluate("Math.max(...[...document.querySelectorAll('[data-viz-panel-key], "
                                   ".react-grid-item')].map(e => e.getBoundingClientRect().bottom))")
            page.set_viewport_size({"width": WIDTH, "height": int(height) + 64})
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(3000)
            if page.get_by_text("No data", exact=True).count():
                empty.append(uid)
            page.mouse.move(0, 0)
            page.screenshot(path=OUT / f"{uid}.png", full_page=True)
            print(f"ok   {uid}.png ({WIDTH}x{int(height) + 64})", flush=True)
        browser.close()
    if empty:
        sys.exit(f"FAIL panels without data in: {', '.join(empty)}")


if __name__ == "__main__":
    main()
