#!/usr/bin/env python3
"""
Live dashboard for the Felicity watchlist plants.

Serves one web page (dashboard.html) showing an energy-flow panel per plant:
solar, grid, battery and load power, state of charge and solar energy today.
Data comes from probe.py, using the same .env and watchlist.txt as the
Telegram reports. Read-only: it only calls the device list and snapshot
endpoints. Standard library only, apart from what probe.py already needs.

    python dashboard.py                # http://<this-machine>:8080
    python dashboard.py --demo         # simulated plants, no Felicity login
    python dashboard.py --port 9000 --refresh 120

Settings (command line, or .env):
    DASHBOARD_HOST       address to listen on (default 0.0.0.0, all interfaces)
    DASHBOARD_PORT       default 8080
    DASHBOARD_REFRESH_S  seconds between refreshes while someone is viewing
                         (default 60, minimum 30)
    DASHBOARD_PASSWORD   if set, the browser asks for it (any username)

Felicity is only polled while the page is open somewhere. With no viewer for
two minutes the poller goes idle, and resumes on the next page load.
"""
from __future__ import annotations

import argparse
import base64
import hmac
import json
import math
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "dashboard.html"
IDLE_AFTER_S = 120      # stop polling this long after the last page request
LIST_REFRESH_S = 1800   # re-read the device list (plant membership) this often
MIN_REFRESH_S = 30      # Felicity's rate limits are unknown; do not go faster
ERROR_PAUSE_S = 60      # wait after a failed cycle before trying again


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


class State:
    """Thread-safe store shared by the poller and the web server."""

    def __init__(self, refresh_s: int, demo: bool):
        self.lock = threading.Lock()
        self.viewer = threading.Event()
        self.last_seen = 0.0
        self.order: list[str] = []
        self.plants: dict[str, dict] = {}
        self.unmatched: list[str] = []
        self.updated: float | None = None
        self.refreshing = False
        self.error: str | None = None
        self.refresh_s = refresh_s
        self.demo = demo

    def seen(self) -> None:
        self.last_seen = time.time()
        self.viewer.set()

    def viewer_active(self) -> bool:
        return time.time() - self.last_seen < IDLE_AFTER_S

    def set_roster(self, roster: list[tuple[str, str]], unmatched: list[str]) -> None:
        with self.lock:
            self.order = [pid for pid, _ in roster]
            self.unmatched = unmatched
            for pid, name in roster:
                self.plants.setdefault(pid, {"plantId": pid, "plant": name, "pending": True})
            for pid in list(self.plants):
                if pid not in self.order:
                    del self.plants[pid]

    def put(self, plant: dict) -> None:
        with self.lock:
            self.plants[plant["plantId"]] = plant

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "now": time.time(),
                "updated": self.updated,
                "refreshing": self.refreshing,
                "refresh_s": self.refresh_s,
                "error": self.error,
                "demo": self.demo,
                "unmatched": list(self.unmatched),
                "plants": [self.plants[pid] for pid in self.order if pid in self.plants],
            }


class LiveSource:
    """Reads the watchlist plants from the Felicity portal through probe.py."""

    def __init__(self, watchlist: Path):
        import probe  # also loads .env

        self.probe = probe
        user, password = os.environ.get("FELICITY_USER"), os.environ.get("FELICITY_PASS")
        if not user or not password:
            raise SystemExit("Set FELICITY_USER and FELICITY_PASS (in .env)")
        self.portal = probe.FelicityPortal(user, password)
        self.watchlist = watchlist
        self.groups: dict[str, list[dict]] = {}
        self.listed_at = 0.0
        self.watchlist_mtime: float | None = None
        self.roster: list[tuple[str, str]] = []
        self.unmatched: list[str] = []

    def plant_roster(self) -> tuple[list[tuple[str, str]], list[str]]:
        try:
            mtime = self.watchlist.stat().st_mtime
        except OSError:
            raise RuntimeError(f"cannot read {self.watchlist.name}") from None
        stale = time.time() - self.listed_at > LIST_REFRESH_S
        if stale or not self.groups:
            self.groups = self.probe.group_by_plant(self.portal.devices())
            self.listed_at = time.time()
        if stale or mtime != self.watchlist_mtime or not self.roster:
            wanted = self.probe.load_watchlist(str(self.watchlist))
            matched, self.unmatched = self.probe.match_plants(self.groups, wanted)
            self.roster = [(pid, self.probe.plant_name(self.groups[pid])) for pid in matched]
            self.watchlist_mtime = mtime
        return self.roster, self.unmatched

    def fetch(self, pid: str) -> dict:
        return self.probe.watch_plant(self.portal, pid, self.groups[pid])


class DemoSource:
    """Simulated plants that vary over time, for previewing the page."""

    PLANTS = [
        ("d1", "Demo Clinic, 12 kW off-grid", 2, 2, "off"),
        ("d2", "Demo School, 15 kW hybrid", 1, 3, "export"),
        ("d3", "Demo Office, 8 kW hybrid", 1, 1, "import"),
        ("d4", "Demo Cold Store, 20 kW off-grid", 2, 4, "low"),
        ("d5", "Demo Pump Station, 6 kW", 1, 1, "warn"),
        ("d6", "Demo Guest House, 5 kW", 1, 1, "dead"),
    ]

    def plant_roster(self) -> tuple[list[tuple[str, str]], list[str]]:
        return [(p[0], p[1]) for p in self.PLANTS], ["Demo entry with a typo"]

    def fetch(self, pid: str) -> dict:
        time.sleep(0.2)
        _, name, inv, batt, kind = next(p for p in self.PLANTS if p[0] == pid)
        t = time.time() / 20.0
        wave = 0.5 + 0.5 * math.sin(t + hash(pid) % 7)
        plant = {"plantId": pid, "plant": name, "inverters": inv, "batteries": batt,
                 "status": f"NMx{inv + batt}", "mode": "Off-Grid Mode", "warnings": [], "errors": [],
                 "batt_capacity_unknown": 0, "reserve_pct": 20.0,
                 "offline": False, "offline_devices": [], "last_data": None, "savings": None}
        if kind == "dead":
            plant.update(pv_W=None, load_W=None, grid_in_W=None, batt_W=None, soc_avg=None,
                         soc_min=None, pv_today_kWh=None, mode=None, status="OLx2",
                         batt_rated_kWh=None, batt_remaining_kWh=None, backup_h=None,
                         offline=True, last_data="2026-08-04 02:25", offline_devices=[
                             {"sn": "020306004825200179", "model": "IVEM6048", "kind": "inverter",
                              "since": "2026-08-04 02:25"}])
            return plant
        pv = round(9000 * wave) if kind != "low" else 0
        load = round(1800 + 2500 * (1 - wave))
        grid = {"import": round(900 + 600 * wave), "export": -round(2200 * wave)}.get(kind, 0)
        plant.update(pv_W=pv, load_W=load, grid_in_W=grid, batt_W=pv + grid - load,
                     soc_avg=round(55 + 40 * wave, 1), soc_min=round(52 + 40 * wave, 1),
                     pv_today_kWh=round(31.4 * wave + 4, 1))
        if kind in ("import", "export"):
            plant["mode"] = "Line Mode"
        if kind == "low":
            plant.update(soc_avg=17.0, soc_min=14.0, pv_today_kWh=0.4, offline_devices=[
                {"sn": "073004850025270356", "model": "FLA48500", "kind": "battery", "since": "2026-10-06 09:10"}])
        if kind == "warn":
            plant["warnings"] = ["020308004825210463: PV2 low voltage"]
            plant["batt_capacity_unknown"] = 1
        day_of_month = time.localtime().tm_mday
        today = plant["pv_today_kWh"]
        month = round(today + 38.0 * inv * (day_of_month - 1), 1)
        plant["savings"] = {"currency": "GHS", "tariff_per_kwh": 1.85, "export_rate_per_kwh": 0.0,
                            "today": round(today * 1.85, 2), "today_used_kWh": today,
                            "month": round(month * 1.85, 2), "month_used_kWh": month,
                            "year": None, "year_used_kWh": None, "total": None, "total_used_kWh": None}
        rated = 10.0 * batt
        remaining = rated * plant["soc_avg"] / 100
        plant.update(batt_rated_kWh=rated, batt_remaining_kWh=round(remaining, 1),
                     backup_h=round(max(remaining - rated * 0.2, 0) / (load / 1000), 1))
        return plant


def poll_loop(state: State, source) -> None:
    while True:
        if not state.viewer_active():
            state.viewer.clear()
            if not state.viewer_active():  # re-check to avoid missing a request
                log("poller idle: no viewers")
                state.viewer.wait()
                log("poller resumed: page opened")
        started = time.time()
        state.refreshing = True
        try:
            roster, unmatched = source.plant_roster()
            state.set_roster(roster, unmatched)
            for pid, _ in roster:
                if not state.viewer_active():
                    break
                plant = source.fetch(pid)
                plant["fetched"] = time.time()
                state.put(plant)
            else:
                state.updated = time.time()
            state.error = None
            pause = max(state.refresh_s - (time.time() - started), 5)
        except Exception as err:  # the poller thread must never die
            state.error = f"{type(err).__name__}: {err}"
            log(f"refresh failed: {state.error}")
            pause = ERROR_PAUSE_S
        finally:
            state.refreshing = False
        time.sleep(pause)


def make_handler(state: State, password: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FelicityDashboard"

        def log_message(self, fmt, *args):  # keep the journal quiet
            pass

        def authorised(self) -> bool:
            if not password:
                return True
            header = self.headers.get("Authorization", "")
            if header.startswith("Basic "):
                try:
                    supplied = base64.b64decode(header[6:]).decode("utf-8", "replace").partition(":")[2]
                except ValueError:
                    supplied = ""
                if hmac.compare_digest(supplied.encode(), password.encode()):
                    return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Felicity dashboard"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def reply(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return self.reply(200, b"ok\n", "text/plain")
            if not self.authorised():
                return
            if path == "/api/state":
                state.seen()
                return self.reply(200, json.dumps(state.snapshot()).encode(), "application/json")
            if path in ("/", "/index.html"):
                try:
                    return self.reply(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                except OSError:
                    return self.reply(500, b"dashboard.html is missing\n", "text/plain")
            self.reply(404, b"not found\n", "text/plain")

    return Handler


def main() -> int:
    env = os.environ.get
    ap = argparse.ArgumentParser(description="Live dashboard for the Felicity watchlist plants")
    ap.add_argument("--demo", action="store_true", help="simulated plants, no Felicity login")
    ap.add_argument("--watchlist", default="watchlist.txt", help="watchlist file (default watchlist.txt)")
    ap.add_argument("--host", help="address to listen on (default 0.0.0.0)")
    ap.add_argument("--port", type=int, help="port (default 8080)")
    ap.add_argument("--refresh", type=int, help="seconds between refreshes (default 60, minimum 30)")
    args = ap.parse_args()

    source = DemoSource() if args.demo else LiveSource(HERE / args.watchlist)  # loads .env
    host = args.host or env("DASHBOARD_HOST", "0.0.0.0")
    port = args.port or int(env("DASHBOARD_PORT", "8080"))
    refresh = args.refresh or int(env("DASHBOARD_REFRESH_S", "60"))
    if not args.demo and refresh < MIN_REFRESH_S:
        log(f"refresh raised from {refresh}s to the {MIN_REFRESH_S}s minimum")
        refresh = MIN_REFRESH_S
    password = env("DASHBOARD_PASSWORD") or None

    state = State(refresh, args.demo)
    threading.Thread(target=poll_loop, args=(state, source), daemon=True).start()
    try:
        server = ThreadingHTTPServer((host, port), make_handler(state, password))
    except OSError as err:
        log(f"cannot listen on {host}:{port}: {err.strerror}")
        return 1
    log(f"dashboard on http://{host}:{port}  refresh {refresh}s  "
        f"password {'on' if password else 'off'}{'  DEMO DATA' if args.demo else ''}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())