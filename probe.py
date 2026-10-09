#!/usr/bin/env python3
"""
Felicity Solar (Shine / FSolar) portal probe. No OpenAPI account required.

Logs in the way the web portal does (POST /userlogin) with an ordinary
Shine/FSolar account, lists the devices visible to that account and prints
key telemetry from each device's snapshot.

These are the portal's internal endpoints, not the documented /openApi/ ones.
They are undocumented and can change without notice.

    pip install requests pycryptodome
    export FELICITY_USER='you@example.com' FELICITY_PASS='...'
    python3 probe.py            # summary line per device
    python3 probe.py --raw      # full JSON, use this to map fields
    python3 probe.py --sn 0205...  # one device only
    python3 probe.py --limit 10    # first 10 devices only
    python3 probe.py --plants      # one line per plant, from the device
                                            # list only (no per-device calls)
    python3 probe.py --plants --json   # same, machine-readable
    python3 probe.py --list-plants # plantId, name, device count
    python3 probe.py --watch watchlist.txt             # selected plants only
    python3 probe.py --watch watchlist.txt --telegram  # and send the report
    python3 probe.py --discover    # show a device-list row and the
                                   # portal's plant-related endpoints
    python3 probe.py --telegram-chats   # list chat IDs the bot can see
    python3 probe.py --telegram-test    # send a test message

Settings are read from the environment, and from a `.env` file next to this
script if one exists (KEY=VALUE lines; real environment variables win).

Optional env:
    FELICITY_CA_BUNDLE   path to a PEM bundle if TLS verification fails
    FELICITY_PUBKEY      base64 RSA public key, skips scraping it from the portal
    FELICITY_TOKEN_FILE  token cache path (default ~/.felicity_token.json)
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   required for --telegram
    BATTERY_RESERVE_PCT  state of charge treated as empty when estimating
                         backup time (default 20)

Savings: put the price of the electricity that solar replaces in savings.json
next to this script (a default, and optionally one per plant). Each plant then
reports money saved today, this month, this year and in total.

Watchlist file: one plant per line, either a plantId or a plant name
(case-insensitive; exact match first, then substring). `#` starts a comment.
"""
import argparse
import base64
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA



def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file without overriding the real
    environment, so cron and systemd need no shell wrapper."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


load_env_file(Path(__file__).resolve().parent / ".env")

PORTAL_LOGIN = "https://shine.felicitysolar.com/login"
API = "https://shine-api.felicitysolar.com"
# Key published in the OpenAPI doc. Used only if scraping the portal's key fails.
DOC_PUBKEY = (
    "MFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBAK0GDivaRzIKeTmQnAxAYh2LChuHWDp0yHZ0zIvm"
    "+Eoi7J+rx7phqR7EtkBDO3HWqAXVkNDeeQaU32P5w1Q4FVUCAwEAAQ=="
)
TOKEN_FILE = Path(os.environ.get("FELICITY_TOKEN_FILE", "~/.felicity_token.json")).expanduser()
VERIFY = os.environ.get("FELICITY_CA_BUNDLE") or True
TIMEOUT = (10, 30)   # (connect, read) seconds
SWEEP_DEADLINE_S = 300  # give up on a device-list sweep after this long
PAGE_SIZE = 50
POLITE_DELAY_S = 1.0  # rate limits are unknown, so stay slow
TOKEN_EXPIRED = (998, 999)
RETRIES = 3          # on timeouts / connection / DNS errors
BACKOFF_S = 2.0      # 2 s, 4 s, 8 s
MAX_BUNDLES = 600
KEYWORDS = re.compile(r"plant|station|site|overview|statistic|energy|home", re.I)
PATH_RE = re.compile(r"""["'`](/?[A-Za-z][A-Za-z0-9_\-]*(?:/[A-Za-z0-9_\-{}$.:]+)+)["'`]""")
ASSET_RE = re.compile(r"""["'`]([^"'`\s]+\.js)["'`]""")
STATIC_EXT = (".js", ".css", ".png", ".jpg", ".svg", ".vue", ".json", ".ico", ".gif", ".woff", ".ttf")


def log(msg: str) -> None:
    """Progress and diagnostics on stderr, so stdout stays clean for reports."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def jwt_exp(token: str) -> int:
    """Read `exp` (epoch seconds) from the JWT payload without verifying it."""
    payload = token.replace("Bearer_", "").split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return int(json.loads(base64.urlsafe_b64decode(payload))["exp"])


def encrypt_password(password: str, key_b64: str) -> str:
    key = RSA.import_key(f"-----BEGIN PUBLIC KEY-----\n{key_b64}\n-----END PUBLIC KEY-----")
    return base64.b64encode(PKCS1_v1_5.new(key).encrypt(password.encode())).decode()


def scrape_public_key(s: requests.Session) -> str | None:
    """Pull the RSA key out of the portal's JS bundle. Fragile by nature."""
    try:
        html = s.get(PORTAL_LOGIN, timeout=TIMEOUT).text
        text = html
        m = re.search(r'(?:href|src)=["\']([^"\']*/index\.[^"\']*\.js)["\']', html, re.I)
        if m:
            main_js = s.get(urljoin(PORTAL_LOGIN, m.group(1)), timeout=TIMEOUT).text
            text += main_js
            route = re.search(
                r'path:\s*["\']/login["\'][\s\S]*?component:\s*\(\)\s*=>[\s\S]*?\[(.*?)\]', main_js
            )
            if route:
                for src in re.findall(r'["\']([^"\']*/index\.[^"\']*\.js)["\']', route.group(1)):
                    r = s.get(urljoin(PORTAL_LOGIN, src), timeout=TIMEOUT)
                    if r.ok:
                        text += r.text
        m = re.search(r"setPublicKey\s*\(\s*([A-Za-z0-9_$]+)\s*\)", text)
        if not m:
            return None
        values = re.findall(re.escape(m.group(1)) + r"\s*=\s*(['\"`])(.*?)\1", text)
        return max((v[1] for v in values), key=len) if values else None
    except requests.RequestException:
        return None


class FelicityPortal:
    def __init__(self, user: str, password: str):
        self.user, self.password = user, password
        self.s = requests.Session()
        self.s.verify = VERIFY
        self.s.headers.update({"accept": "application/json, text/plain, */*"})
        self._token: str | None = None

    # --- auth -------------------------------------------------------------
    def _load_cached(self) -> str | None:
        try:
            cache = json.loads(TOKEN_FILE.read_text())
            if cache["user"] == self.user and cache["exp"] - 300 > time.time():
                return cache["token"]
        except (OSError, ValueError, KeyError):
            pass
        return None

    def _login(self) -> str:
        key = os.environ.get("FELICITY_PUBKEY")
        if not key:
            log("login: fetching RSA key from the portal")
            key = scrape_public_key(self.s)
            if not key:
                log("login: portal key not found, using the key from the OpenAPI document")
                key = DOC_PUBKEY
        log("login: POST /userlogin")
        r = self.s.post(
            f"{API}/userlogin",
            json={
                "userName": self.user,
                "password": encrypt_password(self.password, key),
                "version": "1.0",
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        body = r.json()
        data = body.get("data")
        if not isinstance(data, dict) or not data.get("token"):
            # 1002006 = wrong password (or wrong RSA key), 1002001 = not activated
            raise RuntimeError(f"Felicity login failed: code={body.get('code')} message={body.get('message')}")
        token = data["token"]
        TOKEN_FILE.write_text(json.dumps({"user": self.user, "token": token, "exp": jwt_exp(token)}))
        TOKEN_FILE.chmod(0o600)
        log("login: ok, token cached")
        return token

    def token(self, force: bool = False) -> str:
        if force or not self._token:
            self._token = (None if force else self._load_cached()) or self._login()
        return self._token

    # --- requests ---------------------------------------------------------
    def _send(self, path: str, payload: dict, token: str) -> dict:
        for i in range(RETRIES + 1):
            try:
                r = self.s.post(f"{API}{path}", json=payload,
                                headers={"authorization": token}, timeout=TIMEOUT)
                r.raise_for_status()
                return r.json()  # errors arrive as HTTP 200 with a body-level code
            except (requests.ConnectionError, requests.Timeout) as err:
                if i == RETRIES:
                    raise
                wait = BACKOFF_S * 2 ** i
                log(f"{path}: {type(err).__name__}, retry {i + 1}/{RETRIES} in {wait:.0f}s")
                time.sleep(wait)
        raise RuntimeError("unreachable")

    def post(self, path: str, payload: dict) -> dict:
        body = self._send(path, payload, self.token())
        if body.get("code") in TOKEN_EXPIRED:
            log(f"{path}: token rejected (code {body.get('code')}), logging in again")
            body = self._send(path, payload, self.token(force=True))
        return body

    def device_page(self, page: int, size: int = PAGE_SIZE) -> dict:
        body = self.post(
            "/device/list_device_all_type",
            {"pageNum": page, "pageSize": size, "deviceSn": "",
             "status": "", "sampleFlag": "", "oscFlag": ""},
        )
        if body.get("code") not in (200, 0) or not isinstance(body.get("data"), dict):
            raise RuntimeError(
                f"device list page {page} failed: code={body.get('code')} message={body.get('message')}"
            )
        return body["data"]

    def devices(self) -> list[dict]:
        out, seen, page, started = [], set(), 1, time.monotonic()
        while page <= 100:
            if time.monotonic() - started > SWEEP_DEADLINE_S:
                raise RuntimeError(f"device list sweep exceeded {SWEEP_DEADLINE_S}s at page {page}")
            t0 = time.monotonic()
            data = self.device_page(page)
            rows = data.get("dataList") or []
            fresh = [r for r in rows if r.get("deviceSn") not in seen]
            seen.update(r.get("deviceSn") for r in fresh)
            out.extend(fresh)
            total, pages = num(data.get("total")), num(data.get("totalPage"))
            log(f"device list: page {page}/{int(pages) if pages else '?'}, "
                f"{len(out)}/{int(total) if total else '?'} devices, {time.monotonic() - t0:.1f}s")
            if (not fresh or len(rows) < PAGE_SIZE
                    or (pages and page >= pages) or (total and len(out) >= total)):
                break
            page += 1
            time.sleep(POLITE_DELAY_S)
        return out

    def snapshot(self, sn: str) -> dict:
        body = self.post(
            "/device/get_device_snapshot",
            {"deviceSn": sn, "deviceType": "BP", "dateStr": time.strftime("%Y-%m-%d %H:%M:%S")},
        )
        data = body.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"code={body.get('code')} message={body.get('message')}")
        return data


def first(d: dict, *keys):
    """First non-empty value among keys. Field names differ by model/firmware."""
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def summarise(sn: str, d: dict) -> str:
    fields = [
        ("type", first(d, "productTypeEnum")),
        ("model", first(d, "deviceModel", "modelName", "model")),
        ("mode", first(d, "workModeStr", "operMStr", "workMode")),
        ("pv_W", first(d, "pvTotalPower", "pvPower")),
        ("grid_W", first(d, "acTtlInpower")),  # negative = exporting (per OpenAPI doc)
        ("load_W", first(d, "totalConsumPower", "acTotalOutActPower")),
        ("batt_W", first(d, "emsPower", "bmsPower")),
        ("soc_%", first(d, "emsSoc", "battSoc")),
        ("pv_today", first(d, "ePvToday", "epvToday", "eToday", "etoday")),
        ("warn", first(d, "warnMsg")),
        ("wifi", first(d, "wifiSignal")),
    ]
    return f"{sn}  " + "  ".join(f"{k}={v}" for k, v in fields if v is not None)


# --- plant-level view ---------------------------------------------------------
# Built purely from /device/list_device_all_type rows, which already carry
# plantId, plantName, status and live power/SOC fields with explicit units.

def num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def watts(row: dict, key: str) -> float | None:
    """Value of `key` in W, using the row's `<key>Unit` field (W or kW)."""
    v = num(row.get(key))
    if v is None:
        return None
    unit = str(row.get(key + "Unit") or "W").strip().lower()
    return v * 1000 if unit == "kw" else v


def is_battery(row: dict) -> bool:
    """Battery devices carry a battery capacity in the device list; inverter rows leave it null."""
    return str(row.get("deviceType") or "").upper() == "BP" or row.get("battCapacity") is not None


def has_live(row: dict) -> bool:
    return any(row.get(k) is not None for k in ("pvTotalPower", "bmsPower", "battSoc", "totalPower"))


def tally(values) -> str:
    counts: dict[str, int] = {}
    for v in values:
        counts[str(v or "?")] = counts.get(str(v or "?"), 0) + 1
    return " ".join(f"{k}x{n}" for k, n in sorted(counts.items()))


def group_by_plant(devices: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for d in devices:
        groups.setdefault(str(d.get("plantId") or f"name:{d.get('plantName')}"), []).append(d)
    return groups


def plant_summaries(devices: list[dict]) -> list[dict]:
    groups = group_by_plant(devices)

    out = []
    for pid, rows in groups.items():
        inverters = [r for r in rows if not is_battery(r)]
        batteries = [r for r in rows if is_battery(r)]

        def values(fn, first, second):
            """Non-null values from `first`; fall back to `second` so the same
            battery is never counted from both the inverter and the pack."""
            vals = [v for v in map(fn, first) if v is not None]
            return vals or [v for v in map(fn, second) if v is not None]

        def total(vals):
            return round(sum(vals), 1) if vals else None

        # battery power: the inverter sees the whole bank, packs only what is monitored
        batt = values(lambda r: watts(r, "bmsPower"), inverters, batteries)
        # SOC: packs are the source; inverters report 0 when they have no BMS link
        soc = values(lambda r: num(r.get("battSoc")), batteries, inverters)
        fails = sorted({str(r["failCode"]) for r in rows if r.get("failCode") not in (None, "", "0", 0)})
        out.append({
            "plantId": pid,
            "plant": rows[0].get("plantName") or "(no plant)",
            "devices": len(rows),
            "types": tally(r.get("deviceType") for r in rows),
            "status": tally(r.get("status") for r in rows),
            "mode": ", ".join(sorted({r["wkStateName"] for r in inverters if r.get("wkStateName")})) or None,
            "rated_kW": total([v for v in (num(r.get("ratedPower")) for r in inverters) if v is not None]),
            "pv_W": total([v for v in (watts(r, "pvTotalPower") for r in inverters) if v is not None]),
            "total_W": total([v for v in (watts(r, "totalPower") for r in inverters) if v is not None]),
            "grid_ct_W": total([v for v in (watts(r, "ctAcTtlInPower") for r in inverters) if v is not None]),
            "batt_W": total(batt),
            "soc_avg": round(sum(soc) / len(soc), 1) if soc else None,
            "soc_min": min(soc) if soc else None,
            "fail": ",".join(fails) or None,
            "live": any(has_live(r) for r in rows),
        })
    return sorted(out, key=lambda p: p["plant"].lower())


def print_plants(devices: list[dict], as_json: bool) -> None:
    plants = plant_summaries(devices)
    if as_json:
        print(json.dumps(plants, indent=2, ensure_ascii=False))
    else:
        cols = [("plant", 28), ("devices", 3), ("status", 12), ("mode", 14), ("rated_kW", 8),
                ("pv_W", 8), ("total_W", 8), ("grid_ct_W", 9), ("batt_W", 8),
                ("soc_avg", 7), ("soc_min", 7), ("fail", 8)]

        def cell(v, w):
            s = "-" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))
            return s[:w].ljust(w)

        print("  ".join(name[:w].ljust(w) for name, w in cols))
        for p in plants:
            print("  ".join(cell(p[name], w) for name, w in cols) + ("" if p["live"] else "  NO LIVE DATA"))

    # legends on stderr: the portal's codes are undocumented, so show what is in use
    def legend(key):
        groups: dict[str, list[dict]] = {}
        for d in devices:
            groups.setdefault(str(d.get(key) or "?"), []).append(d)
        for code, rows in sorted(groups.items()):
            models = sorted({str(r.get("deviceModel")) for r in rows})[:4]
            live = sum(has_live(r) for r in rows)
            print(f"  {key}={code}: {len(rows)} device(s), {live} with live values, e.g. {', '.join(models)}",
                  file=sys.stderr)

    pv = sum(p["pv_W"] or 0 for p in plants)
    print(f"\n{len(plants)} plant(s), {len(devices)} device(s), "
          f"{sum(p['live'] for p in plants)} plant(s) reporting, fleet PV {pv / 1000:.1f} kW", file=sys.stderr)
    legend("status")
    legend("deviceType")


# --- watchlist: selected plants only ------------------------------------------
# One list sweep resolves plant membership, status and units; live values then
# come from a snapshot of each device in the watched plants only.

SUB_ENTRY = re.compile(r"-\d+$")  # "<sn>-1" rows duplicate the parent's battery data


def plant_name(rows: list[dict]) -> str:
    return str(rows[0].get("plantName") or "(no plant)").strip()


def load_watchlist(path: str) -> list[str]:
    entries = (line.split("#", 1)[0].strip() for line in Path(path).read_text().splitlines())
    return [e for e in entries if e]


def match_plants(groups: dict[str, list[dict]], wanted: list[str]) -> tuple[list[str], list[str]]:
    """Resolve watchlist entries to plantIds, keeping watchlist order."""
    matched, unmatched = [], []
    for w in wanted:
        if w in groups:
            hits = [w]
        else:
            exact = [pid for pid, rows in groups.items() if plant_name(rows).lower() == w.lower()]
            hits = exact or [pid for pid, rows in groups.items() if w.lower() in plant_name(rows).lower()]
        if not hits:
            unmatched.append(w)
        matched += [pid for pid in hits if pid not in matched]
    return matched, unmatched


NOMINAL_PACK_V = 51.2                 # 16-cell LFP module
MODEL_AH = re.compile(r"48(\d{3})")   # FLA48500TG2 -> 500 Ah
STALE_AFTER_S = 12 * 3600             # see device_offline()


def reserve_pct() -> float:
    try:
        return min(max(float(os.environ.get("BATTERY_RESERVE_PCT", "20")), 0.0), 95.0)
    except ValueError:
        return 20.0


def positive(v) -> float | None:
    v = num(v)
    return v if v and v > 0 else None


MODULE_V_TYPICAL = 53.5  # a 16-cell LFP module sits near this over most of its working range
COUNT_BASIS = {
    "reported": "count reported by the stack",
    "sibling": "count taken from a sister stack at the same voltage",
    "limits": "count from the stack's voltage limits",
    "voltage": "count estimated from stack voltage, may be off by one",
}


def module_count(snap: dict) -> tuple[int, str]:
    """(modules in series inside one battery device, how that was established).

    1 for a 48 V pack; 10 or 12 for the high-voltage stacks seen so far. An
    explicit count from the snapshot is used when it agrees with stack voltage
    / module voltage to within 25 %. Some stacks report a count of 0; for those
    the midpoint of the BMS charge and discharge voltage limits is tried, then
    the stack voltage itself, which drifts with state of charge.
    watch_plant() later replaces such estimates with a sister stack's count.
    """
    volt = positive(snap.get("volt")) or NOMINAL_PACK_V
    stack_v = positive(first(snap, "battVolt", "emsVoltage"))
    listed = snap.get("bmsVoltageList")
    listed = sum(1 for v in map(num, listed) if v and 20 < v < 100) if isinstance(listed, list) else 0
    if stack_v is None:
        return (listed, "reported") if listed >= 2 and listed == num(snap.get("batCount")) else (1, "single")
    ratio = stack_v / volt
    if ratio < 1.5:
        return 1, "single"
    for count in (listed, num(snap.get("batCount")), num(snap.get("cellNumber"))):
        if count and 0.75 * ratio <= count <= 1.25 * ratio:
            return int(count), "reported"
    high, low = positive(snap.get("BMSLCVolt")), positive(snap.get("BMSLDVolt"))
    if high and low:
        count = round((high + low) / 2 / volt)
        if count >= 2 and 0.75 * ratio <= count <= 1.25 * ratio:
            return count, "limits"
    return max(round(stack_v / MODULE_V_TYPICAL), 1), "voltage"


def battery_energy(snap: dict, row: dict) -> dict:
    """Per-module energy, module count and state of charge of one battery device.

    Felicity's `ratedEnergy` is per module, so a high-voltage stack is
    `ratedEnergy` x modules in series. finish_battery() turns this into rated
    and stored energy once the module count is settled.
    """
    modules, basis = module_count(snap)
    per_module = positive(snap.get("ratedEnergy"))
    source = None
    if per_module and per_module > 1000:  # reported in Wh
        per_module /= 1000
    if per_module:
        source = f"{per_module:g} kWh reported"
    else:
        ah = next((a for a in (positive(snap.get(k)) for k in
                               ("capacity", "battCapacity", "emsCapacity", "totalEmsCapacity")) if a), None)
        ah = ah or positive(row.get("battCapacity"))
        from_model = False
        if ah is None:
            m = MODEL_AH.search(str(row.get("deviceModel") or ""))
            ah, from_model = (float(m.group(1)), True) if m else (None, False)
        if ah:
            volt = positive(snap.get("volt")) or NOMINAL_PACK_V
            per_module = ah * volt / 1000
            source = f"{ah:g} Ah {'from the model name' if from_model else 'reported'} x {volt:g} V"
    return {"per_module": per_module, "source": source, "modules": modules, "basis": basis,
            "stack_v": positive(first(snap, "battVolt", "emsVoltage")),
            "soc": num(first(snap, "emsSoc", "battSoc"))}


def finish_battery(e: dict) -> dict:
    """Rated and stored energy. Stored is rated x present state of charge; the
    portal's own `remainingBatteryEnergy` is often missing or stale."""
    rated = e["per_module"] * e["modules"] if e["per_module"] else None
    source = e["source"]
    if rated and e["modules"] > 1:
        source += f" per module x {e['modules']} modules in series ({COUNT_BASIS[e['basis']]})"
    return {"rated": rated, "rated_from": source,
            "remaining": rated * e["soc"] / 100 if rated is not None and e["soc"] is not None else None}


def device_offline(snap: dict, row: dict) -> tuple[bool, str | None]:
    """(offline?, time of the device's last data as the portal shows it).

    The portal keeps serving the last snapshot of a device that has stopped
    reporting, so offline devices must be excluded from live totals. Status
    "OL" is the portal's own offline flag. The age check is a backstop only:
    the snapshot's epoch can be 8 hours off (device time is stored as UTC+8),
    so anything under 12 hours is not judged by age.
    """
    stamp = first(snap, "dataTimeStr")
    stamp = str(stamp)[:16] if stamp else None
    status = str(first(snap, "status") or row.get("status") or "").upper()
    epoch_ms = num(snap.get("dataTime"))
    too_old = epoch_ms is not None and time.time() - epoch_ms / 1000 > STALE_AFTER_S
    return status == "OL" or too_old, stamp


# --- savings ------------------------------------------------------------------
# Money saved = solar energy used on site x the price of the electricity it
# replaced (grid tariff, or generator cost for an off-grid site), plus any
# payment for exported energy. Solar used on site = generation - export.

SAVINGS_FILE = Path(__file__).resolve().parent / "savings.json"
PERIODS = ("today", "month", "year", "total")
PV_ENERGY_KEYS = {
    "today": ("ePvToday", "epvToday", "eToday", "etoday"),
    "month": ("ePvMonth", "epvMonth"),
    "year": ("ePvYear", "epvYear"),
    "total": ("ePvTotal", "epvTotal"),
}
EXPORT_ENERGY_KEYS = {
    "today": ("eGridFeedToday", "egridFeedToday"),
    "month": ("eGridFeedMonth", "egridFeedMonth"),
    "year": ("eGridFeedYear", "egridFeedYear"),
    "total": ("eGridFeedTotal", "egridFeedTotal"),
}
_savings_cache: dict = {"mtime": None, "cfg": {}}


def load_savings_config() -> dict:
    """savings.json as a dict; {} if missing or invalid. Re-read when the file changes."""
    try:
        mtime = SAVINGS_FILE.stat().st_mtime
    except OSError:
        return {}
    if mtime != _savings_cache["mtime"]:
        try:
            cfg = json.loads(SAVINGS_FILE.read_text())
            if not isinstance(cfg, dict):
                raise ValueError("expected a JSON object")
        except (OSError, ValueError) as err:
            log(f"savings.json ignored: {err}")
            cfg = {}
        _savings_cache.update(mtime=mtime, cfg=cfg)
    return _savings_cache["cfg"]


def plant_tariff(cfg: dict, pid: str, name: str) -> float | None:
    """Price per kWh for one plant: its own entry (by plantId or name), else the default."""
    plants = cfg.get("plants") if isinstance(cfg.get("plants"), dict) else {}
    entry = plants.get(pid)
    if entry is None:
        entry = next((v for k, v in plants.items() if str(k).strip().lower() == name.strip().lower()), None)
    rate = entry.get("tariff_per_kwh") if isinstance(entry, dict) else entry
    return positive(rate) or positive(cfg.get("default_tariff_per_kwh"))


def plant_savings(pid: str, name: str, pv: dict, export: dict) -> dict | None:
    """Money saved per period, or None when no tariff is set for the plant."""
    cfg = load_savings_config()
    tariff = plant_tariff(cfg, pid, name)
    if tariff is None:
        return None
    export_rate = positive(cfg.get("export_rate_per_kwh")) or 0.0
    out = {"currency": str(cfg.get("currency") or "GHS"), "tariff_per_kwh": tariff,
           "export_rate_per_kwh": export_rate}
    for period in PERIODS:
        generated = pv.get(period)
        if generated is None:
            out[period] = out[f"{period}_used_kWh"] = None
            continue
        exported = min(max(export.get(period) or 0.0, 0.0), generated)
        used = generated - exported
        out[period] = round(used * tariff + exported * export_rate, 2)
        out[f"{period}_used_kWh"] = round(used, 1)
    return out


def unit_factor(row: dict) -> float:
    """Snapshots carry no unit, so use the unit the device list declares for
    this device (IVGM models report kW, IVEM and T-REX report W)."""
    return 1000.0 if str(row.get("pvTotalPowerUnit") or "W").strip().lower() == "kw" else 1.0


def watch_plant(portal: FelicityPortal, pid: str, rows: list[dict]) -> dict:
    devices = [r for r in rows if r.get("deviceSn") and not SUB_ENTRY.search(r["deviceSn"])]
    inverters, packs, warnings, errors, detail, offline = [], [], [], [], [], []
    pack_count = pack_errors = 0
    for r in devices:
        sn = r["deviceSn"]
        try:
            snap = portal.snapshot(sn)
        except (requests.RequestException, RuntimeError, ValueError) as err:
            errors.append(f"{sn}: {err}")
            pack_count += is_battery(r)
            pack_errors += is_battery(r)
            detail.append({"sn": sn, "model": r.get("deviceModel"), "error": str(err),
                           "kind": "battery" if is_battery(r) else "inverter"})
            continue
        finally:
            time.sleep(POLITE_DELAY_S)
        is_pack = is_battery(r) or first(snap, "productTypeEnum") == "LITHIUM_BATTERY_PACK"
        pack_count += is_pack
        model = first(snap, "deviceModel", "modelName") or r.get("deviceModel")
        entry = {"sn": sn, "model": model, "kind": "battery" if is_pack else "inverter"}
        detail.append(entry)

        gone, stamp = device_offline(snap, r)
        entry.update(offline=gone, data_time=stamp)
        if gone:  # last values of a device that stopped reporting: never mix into live totals
            offline.append({"sn": sn, "model": model, "kind": entry["kind"], "since": stamp})
            continue

        k = unit_factor(r)

        def w(*keys):
            v = num(first(snap, *keys))
            return None if v is None else v * k

        mode = first(snap, "workModeStr", "operMStr")
        energy = battery_energy(snap, r) if is_pack else None
        rec = {
            "pv": w("pvTotalPower", "pvPower"),
            "grid": w("acTtlInpower"),
            "load": w("totalConsumPower", "acTotalOutActPower"),
            "batt": w("emsPower", "bmsPower"),
            "soc": num(first(snap, "emsSoc", "battSoc")),
            "pv_today": num(first(snap, "ePvToday", "epvToday", "eToday", "etoday")),
            "mode": mode if mode not in (None, "-") else None,
            "rated": None,
            "remaining": None,
            "pv_energy": {p: num(first(snap, *PV_ENERGY_KEYS[p])) for p in PERIODS},
            "export_energy": {p: num(first(snap, *EXPORT_ENERGY_KEYS[p])) for p in PERIODS},
            "energy": energy,
            "entry": entry,
        }
        (packs if is_pack else inverters).append(rec)
        if first(snap, "warnMsg"):
            warnings.append(f"{sn}: {first(snap, 'warnMsg')}")
        # per-device breakdown, so every plant total can be traced to its inputs
        entry.update(soc=rec["soc"], batt_W=rec["batt"])
        if is_pack:
            entry["reported"] = {key: snap.get(key) for key in
                                 ("ratedEnergy", "capacity", "volt", "battVolt", "batCount", "cellNumber",
                                  "BMSLCVolt", "BMSLDVolt", "remainingBatteryEnergy", "battSoh")}
        else:
            entry.update(pv_W=rec["pv"], load_W=rec["load"], grid_in_W=rec["grid"], pv_today_kWh=rec["pv_today"])

    # Stacks in parallel on one DC bus have the same number of modules in series.
    # Where a stack did not report its count, take it from a sister stack whose
    # voltage is within 5 % rather than trust a voltage-based estimate.
    trusted = [x["energy"] for x in packs if x["energy"]["basis"] == "reported" and x["energy"]["stack_v"]]
    for x in packs:
        e = x["energy"]
        if e["basis"] in ("limits", "voltage") and e["stack_v"]:
            near = [t for t in trusted if abs(t["stack_v"] - e["stack_v"]) <= 0.05 * e["stack_v"]]
            if near:
                e["modules"] = min(near, key=lambda t: abs(t["stack_v"] - e["stack_v"]))["modules"]
                e["basis"] = "sibling"
        done = finish_battery(e)
        x["rated"], x["remaining"] = done["rated"], done["remaining"]
        x["entry"].update(rated_kWh=done["rated"], stored_kWh=done["remaining"],
                          modules=e["modules"], rated_from=done["rated_from"])

    def total(recs, key):
        vals = [x[key] for x in recs if x[key] is not None]
        return sum(vals) if vals else None

    batt = total(inverters, "batt")
    soc = [x["soc"] for x in packs if x["soc"] is not None]
    if not soc:
        soc = [x["soc"] for x in inverters if x["soc"] is not None]
        if pack_count == 0 and not any(soc):
            soc = []  # an inverter with no battery data link reports 0 %, which is not a reading
    load = total(inverters, "load")
    rated, remaining = total(packs, "rated"), total(packs, "remaining")
    # Backup time: energy above the reserve level divided by the present load,
    # i.e. how long the bank would last if solar and grid stopped now.
    reserve, backup = reserve_pct(), None
    if rated and remaining is not None and load and load >= 50:
        backup = round(max(remaining - rated * reserve / 100, 0) / (load / 1000), 1)
    def energy_sum(field: str, period: str):
        vals = [x[field][period] for x in inverters if x[field][period] is not None]
        return round(sum(vals), 2) if vals else None

    pv_energy = {p: energy_sum("pv_energy", p) for p in PERIODS}
    export_energy = {p: energy_sum("export_energy", p) for p in PERIODS}
    stamps = sorted(o["since"] for o in offline if o["since"])
    return {
        "plantId": pid,
        "plant": plant_name(rows),
        "inverters": len(devices) - pack_count,
        "batteries": pack_count,
        # True when no device in the plant is reporting
        "offline": bool(offline) and not inverters and not packs,
        "offline_devices": offline,
        "last_data": stamps[-1] if stamps else None,
        "batt_rated_kWh": None if rated is None else round(rated, 1),
        "batt_remaining_kWh": None if remaining is None else round(remaining, 1),
        # online packs with no usable capacity figure, plus packs that returned an error
        "batt_capacity_unknown": sum(x["rated"] is None for x in packs) + pack_errors,
        # stacks whose module count rests on voltage alone and may be off by one
        "batt_count_estimated": sum(x["energy"]["basis"] == "voltage" for x in packs),
        "backup_h": backup,
        "reserve_pct": reserve,
        "status": tally(r.get("status") for r in devices),
        "mode": ", ".join(sorted({x["mode"] for x in inverters if x["mode"]})) or None,
        "pv_W": total(inverters, "pv"),
        "load_W": load,
        "grid_in_W": total(inverters, "grid"),
        "batt_W": batt if batt is not None else total(packs, "batt"),
        "soc_avg": round(sum(soc) / len(soc), 1) if soc else None,
        "soc_min": min(soc) if soc else None,
        "pv_today_kWh": total(inverters, "pv_today"),
        "pv_energy_kWh": pv_energy,
        "export_energy_kWh": export_energy,
        "savings": plant_savings(pid, plant_name(rows), pv_energy, export_energy),
        "warnings": warnings,
        "errors": errors,
        "devices": detail,
    }


def format_plant(p: dict) -> str:
    def kw(v):
        return "-" if v is None else f"{v / 1000:.2f} kW"

    lines = [f"{p['plant']}  [{p['inverters']} inv, {p['batteries']} batt, status {p['status']}]"]
    if p.get("offline"):
        lines.append(f"  OFFLINE, last data {p.get('last_data') or 'unknown'}")
    else:
        batt = p["batt_W"]
        # positive battery power = charging (inferred from live data, not documented)
        flow = "" if not batt else (" charging" if batt > 0 else " discharging")
        soc = "-" if p["soc_avg"] is None else f"{p['soc_avg']:.0f}% (min {p['soc_min']:.0f}%)"
        today = "-" if p["pv_today_kWh"] is None else f"{p['pv_today_kWh']:.1f} kWh"
        lines += [
            f"  PV {kw(p['pv_W'])} | Load {kw(p['load_W'])} | Grid in {kw(p['grid_in_W'])} | "
            f"Battery {kw(abs(batt) if batt is not None else None)}{flow}",
            f"  SOC {soc} | PV today {today} | Mode {p['mode'] or '-'}",
        ]
        rated, remaining = p.get("batt_rated_kWh"), p.get("batt_remaining_kWh")
        if rated:
            line = f"  Stored {'-' if remaining is None else format(remaining, '.1f')} of {rated:.1f} kWh"
            if p.get("backup_h") is not None:
                line += f" | Backup ~{p['backup_h']:.1f} h at this load (to {p['reserve_pct']:.0f}%)"
            if p.get("batt_capacity_unknown"):
                line += f" | capacity unknown for {p['batt_capacity_unknown']} pack(s)"
            if p.get("batt_count_estimated"):
                line += f" | module count estimated for {p['batt_count_estimated']} stack(s)"
            lines.append(line)
        saved = p.get("savings")
        if saved and (saved["today"] is not None or saved["month"] is not None):
            def money(v):
                return "-" if v is None else f"{saved['currency']} {v:,.0f}"
            lines.append(f"  Saved today {money(saved['today'])} | this month {money(saved['month'])} "
                         f"(at {saved['currency']} {saved['tariff_per_kwh']:g}/kWh)")
        lines += [f"  OFFLINE {o['kind']} {o['model'] or ''} {o['sn']}, last data {o['since'] or 'unknown'}"
                  for o in p.get("offline_devices") or []]
    lines += [f"  WARNING {w}" for w in p["warnings"]]
    lines += [f"  NO DATA {e}" for e in p["errors"]]
    return "\n".join(lines)


# --- Telegram -----------------------------------------------------------------

def tg_call(method: str, payload: dict | None = None) -> dict:
    """Call the Telegram Bot API. Errors never include the request URL,
    because the URL contains the bot token and would end up in logs."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                          json=payload or {}, timeout=TIMEOUT)
        body = r.json()
    except requests.RequestException as err:
        raise RuntimeError(f"Telegram {method}: {type(err).__name__} (network or timeout)") from None
    except ValueError:
        raise RuntimeError(f"Telegram {method}: HTTP {r.status_code}, non-JSON reply") from None
    if not body.get("ok"):
        hint = ""
        moved = (body.get("parameters") or {}).get("migrate_to_chat_id")
        if moved:
            hint = f" (group became a supergroup; set TELEGRAM_CHAT_ID={moved})"
        raise RuntimeError(
            f"Telegram {method}: {body.get('error_code')} {body.get('description')}{hint}"
        )
    return body


def send_telegram(text: str) -> None:
    """Send a plain-text report. Blocks are separated by blank lines; the first
    line of each block is shown in bold, and blocks are never split."""
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat:
        raise RuntimeError("TELEGRAM_CHAT_ID is not set")
    chunks, current = [], ""
    for block in text.split("\n\n"):
        head, _, rest = html.escape(block, quote=False).partition("\n")
        block = f"<b>{head}</b>" + (f"\n{rest}" if rest else "")
        if current and len(current) + len(block) + 2 > 3900:  # Telegram limit is 4096
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{block}" if current else block
    chunks.append(current)
    for chunk in chunks:
        tg_call("sendMessage", {"chat_id": chat, "text": chunk, "parse_mode": "HTML",
                                "disable_web_page_preview": True})


def telegram_chats() -> int:
    """Print the chats that have recently messaged the bot, to find the chat ID."""
    me = tg_call("getMe")["result"]
    print(f"bot: @{me.get('username')}")
    chats: dict = {}
    for upd in tg_call("getUpdates", {"timeout": 0}).get("result", []):
        for key in ("message", "channel_post", "my_chat_member", "edited_message"):
            chat = (upd.get(key) or {}).get("chat")
            if chat:
                chats[chat["id"]] = chat
    if not chats:
        print("No chats yet. Send /start to the bot (or add it to the group and send "
              "/start there), then run this again.")
        return 1
    for cid, chat in chats.items():
        name = chat.get("title") or " ".join(
            x for x in (chat.get("first_name"), chat.get("last_name")) if x
        ) or chat.get("username") or ""
        print(f"{cid}\t{chat.get('type')}\t{name}")
    return 0


def telegram_test() -> int:
    send_telegram(f"Felicity monitor test\nSent {time.strftime('%Y-%m-%d %H:%M')} from {os.uname().nodename}")
    print("sent")
    return 0


def run_watch(portal: FelicityPortal, devices: list[dict], path: str, as_json: bool, telegram: bool) -> int:
    groups = group_by_plant(devices)
    matched, unmatched = match_plants(groups, load_watchlist(path))
    plants = [watch_plant(portal, pid, groups[pid]) for pid in matched]
    for w in unmatched:
        print(f"watchlist entry not found: {w!r}", file=sys.stderr)
    if as_json:
        report = json.dumps(plants, indent=2, ensure_ascii=False)
    else:
        header = f"Felicity watchlist, {time.strftime('%Y-%m-%d %H:%M')} ({len(plants)} plant(s))"
        blocks = [header] + [format_plant(p) for p in plants]
        if unmatched:
            blocks.append("Not found in portal\n" + "\n".join(f"  {w}" for w in unmatched))
        report = "\n\n".join(blocks)
    print(report)
    if telegram and not as_json:
        send_telegram(report)
    return 1 if unmatched or any(p["errors"] for p in plants) else 0


def portal_api_paths(s: requests.Session) -> list[str]:
    """Scan the portal's JS bundles for path strings that look plant-related.

    The result mixes backend API paths with front-end page routes.
    """
    site = urljoin(PORTAL_LOGIN, "/")
    host = site.split("/")[2]
    html = s.get(PORTAL_LOGIN, timeout=TIMEOUT).text
    queue = [urljoin(PORTAL_LOGIN, m) for m in re.findall(r'(?:src|href)=["\']([^"\']+\.js)["\']', html)]
    seen, paths = set(), set()
    while queue and len(seen) < MAX_BUNDLES:
        url = queue.pop(0)
        if url in seen or url.split("/")[2] != host:
            continue
        seen.add(url)
        try:
            r = s.get(url, timeout=TIMEOUT)
        except requests.RequestException:
            continue
        # the SPA answers unknown paths with index.html, so check the type
        if not r.ok or "javascript" not in r.headers.get("content-type", "javascript"):
            continue
        for ref in ASSET_RE.findall(r.text):
            if ref.startswith("."):
                queue.append(urljoin(url, ref))
            else:
                queue += [urljoin(site, ref.lstrip("/")), urljoin(url, ref)]
        for p in PATH_RE.findall(r.text):
            if KEYWORDS.search(p) and not p.lower().endswith(STATIC_EXT):
                paths.add(p)
    print(f"scanned {len(seen)} bundle URL(s)", file=sys.stderr)
    return sorted(paths)


def discover(portal: FelicityPortal) -> None:
    data = portal.device_page(1, size=1)
    print("# device list paging fields")
    print(json.dumps({k: v for k, v in data.items() if k != "dataList"}, indent=2, ensure_ascii=False))
    print("# first device-list row (redact customer details before sharing)")
    print(json.dumps((data.get("dataList") or [{}])[0], indent=2, ensure_ascii=False))
    print("# plant-related paths found in the portal bundles")
    for p in portal_api_paths(portal.s):
        print(p)


def main() -> int:
    ap = argparse.ArgumentParser(description="Felicity portal probe")
    ap.add_argument("--raw", action="store_true", help="dump full JSON")
    ap.add_argument("--sn", help="only this device serial number")
    ap.add_argument("--limit", type=int, help="only the first N devices")
    ap.add_argument("--plants", action="store_true",
                    help="one line per plant, built from the device list only")
    ap.add_argument("--json", action="store_true", help="with --plants or --watch: JSON output")
    ap.add_argument("--list-plants", action="store_true", help="print plantId, name and device count")
    ap.add_argument("--watch", metavar="FILE", help="report only the plants listed in FILE")
    ap.add_argument("--telegram", action="store_true", help="with --watch: send the report to Telegram")
    ap.add_argument("--discover", action="store_true",
                    help="show one device-list row and plant-related portal endpoints")
    ap.add_argument("--telegram-chats", action="store_true",
                    help="list the chat IDs that have messaged the bot")
    ap.add_argument("--telegram-test", action="store_true", help="send a test message")
    args = ap.parse_args()

    if args.telegram_chats or args.telegram_test:
        try:
            return telegram_chats() if args.telegram_chats else telegram_test()
        except RuntimeError as err:
            log(f"FAILED: {err}")
            return 1
    try:
        return run(args)
    except (requests.RequestException, RuntimeError) as err:
        # one notice per failed run, so a scheduled report never fails silently
        log(f"FAILED: {type(err).__name__}: {err}")
        if args.telegram:
            try:
                send_telegram(f"Felicity watchlist report failed\n{type(err).__name__}: {err}")
            except RuntimeError as tg_err:
                log(f"could not send the failure notice: {tg_err}")
        return 1


def run(args: argparse.Namespace) -> int:
    user, password = os.environ.get("FELICITY_USER"), os.environ.get("FELICITY_PASS")
    if not user or not password:
        print("Set FELICITY_USER and FELICITY_PASS", file=sys.stderr)
        return 2

    portal = FelicityPortal(user, password)
    if args.discover:
        discover(portal)
        return 0
    single = args.sn and not (args.list_plants or args.watch or args.plants)
    devices = [] if single else portal.devices()  # one device needs no list sweep
    if args.list_plants:
        for pid, rows in sorted(group_by_plant(devices).items(), key=lambda kv: plant_name(kv[1]).lower()):
            print(f"{pid}\t{plant_name(rows)}\t{len(rows)} device(s)")
        return 0
    if args.watch:
        return run_watch(portal, devices, args.watch, args.json, args.telegram)
    if args.plants:
        print_plants(devices, args.json)
        return 0
    if args.raw and not single:
        print(json.dumps({"devices": devices}, indent=2, ensure_ascii=False))
    sns = [d["deviceSn"] for d in devices if d.get("deviceSn")]
    if args.sn:
        sns = [args.sn]
    if args.limit:
        sns = sns[: args.limit]
    print(f"{len(sns)} device(s)", file=sys.stderr)

    failures = 0
    for sn in sns:
        try:
            snap = portal.snapshot(sn)
            print(json.dumps({sn: snap}, indent=2, ensure_ascii=False) if args.raw else summarise(sn, snap))
        except (requests.RequestException, RuntimeError, ValueError) as err:
            failures += 1
            print(f"{sn}  ERROR {err}", file=sys.stderr)
        time.sleep(POLITE_DELAY_S)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())