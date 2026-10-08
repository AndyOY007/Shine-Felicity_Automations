#!/usr/bin/env python3
"""
Scheduler for the Felicity watchlist report.

Runs `probe.py --watch <watchlist> --telegram` at the times set in
schedule.json, with separate start, end and interval for weekdays (Mon-Fri)
and weekends (Sat-Sun). Edits to schedule.json are picked up within 30 s; no
restart needed. Uses only the standard library.

    python scheduler.py            # run forever
    python scheduler.py --next     # print the next 10 run times and exit
    python scheduler.py --next 30  # print the next 30
    python scheduler.py --once     # send one report now and exit

schedule.json:
    {
      "timezone": "Africa/Accra",
      "watchlist": "watchlist.txt",
      "weekdays": {"start": "07:00", "end": "19:00", "interval_minutes": 60},
      "weekends": {"start": "08:00", "end": "18:00", "interval_minutes": 120}
    }

Reports go out at start, start + interval, ... up to and including end.
Use "00:00" to "23:59" for round-the-clock reports. Set "interval_minutes"
to 0, or add "enabled": false, to switch a day type off.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "schedule.json"
POLL_S = 30            # how often to re-check the clock and schedule.json
GRACE_S = 300          # a run later than this is skipped, not sent late
MIN_INTERVAL_MIN = 10  # Felicity's rate limits are unknown; do not go faster
DEFAULT_TIMEOUT_S = 900


class ConfigError(ValueError):
    pass


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def parse_hhmm(value, field: str) -> dtime:
    try:
        hour, minute = str(value).strip().split(":")
        return dtime(int(hour), int(minute))
    except (ValueError, TypeError):
        raise ConfigError(f'{field}: expected "HH:MM", got {value!r}') from None


def load_config(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text())
    except OSError as err:
        raise ConfigError(f"cannot read {path.name}: {err.strerror}") from None
    except json.JSONDecodeError as err:
        raise ConfigError(f"{path.name} is not valid JSON: {err}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name} must contain a JSON object")

    tz_name = raw.get("timezone", "Africa/Accra")
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"timezone: unknown zone {tz_name!r}") from None

    cfg = {
        "tz": tz,
        "tz_name": tz_name,
        "watchlist": str(raw.get("watchlist", "watchlist.txt")),
        "timeout": int(raw.get("run_timeout_seconds", DEFAULT_TIMEOUT_S)),
    }
    for key in ("weekdays", "weekends"):
        rule = raw.get(key) or {}
        if not isinstance(rule, dict):
            raise ConfigError(f"{key}: expected an object")
        try:
            interval = int(rule.get("interval_minutes", 0))
        except (ValueError, TypeError):
            raise ConfigError(f"{key}.interval_minutes: expected a whole number") from None
        enabled = bool(rule.get("enabled", True)) and interval > 0
        start = parse_hhmm(rule.get("start", "00:00"), f"{key}.start")
        end = parse_hhmm(rule.get("end", "23:59"), f"{key}.end")
        if enabled and interval < MIN_INTERVAL_MIN:
            raise ConfigError(f"{key}.interval_minutes: minimum is {MIN_INTERVAL_MIN}")
        if enabled and end < start:
            raise ConfigError(f"{key}: end {end:%H:%M} is before start {start:%H:%M} "
                              "(windows cannot cross midnight)")
        cfg[key] = {"enabled": enabled, "start": start, "end": end, "interval": interval}
    return cfg


def describe(cfg: dict) -> str:
    def one(key: str) -> str:
        r = cfg[key]
        if not r["enabled"]:
            return f"{key} off"
        return f"{key} {r['start']:%H:%M}-{r['end']:%H:%M} every {r['interval']} min"

    return f"{one('weekdays')}; {one('weekends')}; {cfg['tz_name']}; watchlist {cfg['watchlist']}"


def slots_for(day: date, cfg: dict) -> list[datetime]:
    rule = cfg["weekends" if day.weekday() >= 5 else "weekdays"]
    if not rule["enabled"]:
        return []
    slot = datetime.combine(day, rule["start"], tzinfo=cfg["tz"])
    end = datetime.combine(day, rule["end"], tzinfo=cfg["tz"])
    out = []
    while slot <= end:
        out.append(slot)
        slot += timedelta(minutes=rule["interval"])
    return out


def next_run(after: datetime, cfg: dict) -> datetime | None:
    for offset in range(8):  # a week ahead covers every weekday/weekend combination
        for slot in slots_for((after + timedelta(days=offset)).date(), cfg):
            if slot > after:
                return slot
    return None


def now(cfg: dict) -> datetime:
    return datetime.now(cfg["tz"])


def notify(text: str) -> None:
    """Best-effort Telegram notice, using probe.py's sender."""
    try:
        import probe
        probe.send_telegram(text)
    except Exception as err:  # never let a notice take the scheduler down
        log(f"could not send notice: {err}")


def run_report(cfg: dict) -> int:
    """Run one watchlist report in a child process, so a crash or hang in a
    run cannot stop the scheduler. probe.py sends its own failure notices."""
    cmd = [sys.executable, str(HERE / "probe.py"), "--watch", cfg["watchlist"], "--telegram"]
    log("run: starting watchlist report")
    try:
        result = subprocess.run(cmd, cwd=HERE, env=dict(os.environ, TZ=cfg["tz_name"]),
                                timeout=cfg["timeout"])
    except subprocess.TimeoutExpired:
        log(f"run: killed after {cfg['timeout']}s")
        notify(f"Felicity watchlist report failed\nRun exceeded {cfg['timeout']}s and was stopped")
        return 1
    except OSError as err:
        log(f"run: could not start probe.py: {err}")
        return 1
    log(f"run: finished, exit code {result.returncode}")
    return result.returncode


def announce(target: datetime | None) -> None:
    log(f"next run: {target:%a %Y-%m-%d %H:%M}" if target else
        "no runs scheduled (weekdays and weekends are both off)")


def serve(path: Path, cfg: dict) -> None:
    log(f"scheduler started: {describe(cfg)}")
    mtime = path.stat().st_mtime
    target = next_run(now(cfg), cfg)
    announce(target)
    while True:
        try:
            current = path.stat().st_mtime
        except OSError:
            current = mtime  # file briefly missing during an edit; keep going
        if current != mtime:
            mtime = current
            try:
                cfg = load_config(path)
            except ConfigError as err:
                log(f"schedule.json rejected, keeping the previous schedule: {err}")
            else:
                log(f"schedule.json reloaded: {describe(cfg)}")
                target = next_run(now(cfg), cfg)
                announce(target)
        if target is None:
            time.sleep(POLL_S)
            continue
        wait = (target - now(cfg)).total_seconds()
        if wait > 0:
            time.sleep(min(wait, POLL_S))
            continue
        if -wait > GRACE_S:
            log(f"skipped {target:%a %H:%M}: {-wait / 60:.0f} min late (machine asleep or previous run still going)")
        else:
            run_report(cfg)
        target = next_run(now(cfg), cfg)
        announce(target)


def main() -> int:
    ap = argparse.ArgumentParser(description="Scheduler for the Felicity watchlist report")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="path to schedule.json")
    ap.add_argument("--next", type=int, nargs="?", const=10, metavar="N",
                    help="print the next N run times (default 10) and exit")
    ap.add_argument("--once", action="store_true", help="send one report now and exit")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as err:
        log(f"config error: {err}")
        return 2
    if args.next:
        print(describe(cfg))
        at = now(cfg)
        for _ in range(args.next):
            at = next_run(at, cfg)
            if at is None:
                print("no runs scheduled")
                break
            print(f"{at:%a %Y-%m-%d %H:%M}")
        return 0
    if args.once:
        return run_report(cfg)
    try:
        serve(args.config, cfg)
    except KeyboardInterrupt:
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())