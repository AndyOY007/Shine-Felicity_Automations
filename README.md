# Shine-Felicity automations

Scheduled Telegram reports and a live web dashboard for a chosen set of Felicity Solar plants, read from the Felicity (Shine / FSolar) portal with an ordinary portal account.

For each plant it shows solar, load, grid and battery power, state of charge, battery energy stored and rated, estimated backup time, solar energy today and money saved. It is read-only: nothing here changes an inverter setting.

![Dashboard with simulated plants](dashboard-preview.png)

How it works, and what is still unverified, is in [architecture.md](architecture.md).

## Requirements

Python 3.10 or newer, a Felicity portal login that can see the plants, and a Telegram bot for the reports. The Raspberry Pi section assumes a Linux machine with systemd.

## Files

| File | Purpose |
|---|---|
| `probe.py` | Reads Felicity, builds the per-plant figures, sends Telegram messages. Also the command-line tool |
| `scheduler.py` | Sends the watchlist report to Telegram on a schedule |
| `dashboard.py`, `dashboard.html` | The live dashboard |
| `watchlist.txt` | The plants to report (you create this) |
| `schedule.json` | Report times for weekdays and weekends |
| `savings.json` | Electricity price per plant, for the savings figures |
| `.env` | Logins and settings (you create this; never commit it) |
| `.env.example` | Template for `.env` |
| `requirements.txt` | Python packages |
| `felicity-watch.service`, `felicity-dashboard.service` | systemd units for the Pi |

## 1. Install

```
git clone https://github.com/AndyOY007/Shine-Felicity_Automations.git
cd Shine-Felicity_Automations
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If `venv` is missing on Debian or Raspberry Pi OS, run `sudo apt install python3-venv` first.

## 2. Add your logins

```
cp .env.example .env
chmod 600 .env
```

Edit `.env` and fill in `FELICITY_USER` and `FELICITY_PASS`. Leave the Telegram lines for step 4. Keep each comment on its own line; a comment after a value on the same line becomes part of the value.

Check the login works:

```
python probe.py --list-plants
```

This pages through the device list, printing progress, then lists every plant as `plantId`, name and device count. It takes about 30 seconds for a fleet of 500 devices.

## 3. Choose the plants

Create `watchlist.txt` with one plant per line, by `plantId` or by name. IDs are safer; a name matches exactly first, then as a case-insensitive substring. `#` starts a comment.

```
# high-profile plants
11684007037303265      # WAPCo
A2 old installation
```

Print a report for those plants:

```
python probe.py --watch watchlist.txt
```

## 4. Set up Telegram

1. In Telegram, talk to `@BotFather`, send `/newbot` and follow the prompts. Copy the token it gives you into `.env` as `TELEGRAM_BOT_TOKEN`. Never paste the token anywhere else.
2. Send `/start` to your new bot. For a group, add the bot to the group and send `/start` there; it must be a `/` command, because bots in groups do not see ordinary messages.
3. Run `python probe.py --telegram-chats` and copy the number for the chat you want into `.env` as `TELEGRAM_CHAT_ID`. Group IDs are negative; keep the minus sign.
4. Run `python probe.py --telegram-test`. A test message should arrive within seconds.
5. Run `python probe.py --watch watchlist.txt --telegram` to send a real report.

## 5. Schedule the reports

Edit `schedule.json`:

```json
{
  "timezone": "Africa/Accra",
  "watchlist": "watchlist.txt",
  "weekdays": {"start": "07:00", "end": "19:00", "interval_minutes": 60},
  "weekends": {"start": "08:00", "end": "18:00", "interval_minutes": 120}
}
```

Reports go out at the start time and every interval after it, up to and including the end time. Weekdays are Monday to Friday. The minimum interval is 10 minutes. A window cannot cross midnight; use `"00:00"` to `"23:59"` for round-the-clock reports. Set `interval_minutes` to 0 to switch a day type off.

```
python scheduler.py --next     # show the next 10 run times, send nothing
python scheduler.py --once     # send one report now
python scheduler.py            # run until stopped
```

Edits to `schedule.json` are picked up within 30 seconds without a restart. Run only one scheduler per bot, or every report arrives twice.

## 6. Run the dashboard

```
python dashboard.py --demo     # simulated plants, no login needed
python dashboard.py            # live plants from watchlist.txt
```

Open `http://localhost:8080`. Each plant has a status: Reporting, Check (a warning, an offline or silent device, a low battery, or an uncertain battery capacity), Offline, or No data.

The dashboard reads Felicity only while the page is open in a visible tab, every 60 seconds by default. Devices upload every five minutes, so setting `DASHBOARD_REFRESH_S=300` in `.env` loses little and cuts the load on Felicity.

By default anyone on the same network can open the page. Set `DASHBOARD_PASSWORD` in `.env` to require a password (any username). The page is plain HTTP: use it on a private network such as Tailscale and do not expose the port to the internet.

## 7. Set tariffs for savings

Savings stay hidden until a price is set. Edit `savings.json`:

```json
{
  "currency": "GHS",
  "default_tariff_per_kwh": 1.85,
  "export_rate_per_kwh": 0,
  "plants": {
    "WAPCo": {"tariff_per_kwh": 2.10},
    "A2 old installation": {"tariff_per_kwh": null}
  }
}
```

A plant uses its own tariff if set, otherwise the default. Plants are matched by `plantId` or by exact name. Use the energy charge from the client's bill, or the generator cost per kWh for an off-grid site; the numbers above are placeholders, not real tariffs. Changes apply on the next refresh.

Savings are solar energy used on site (generation minus export) times the tariff, for today and for the month.

## Deploy on the Raspberry Pi

These steps assume user `translight-iot` and the project in `~/Shine-Felicity_Automations`, which is what the two service files contain. If either differs, edit the `User`, `WorkingDirectory` and `ExecStart` lines in both files first.

```
ssh translight-iot@tsl-server
cd ~
git clone https://github.com/AndyOY007/Shine-Felicity_Automations.git
cd Shine-Felicity_Automations
python3 --version                 # must be 3.10 or newer
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env && nano .env
```

Copy `watchlist.txt` across if it is not in the repository. Test before installing the services:

```
.venv/bin/python probe.py --telegram-test
.venv/bin/python scheduler.py --next
.venv/bin/python scheduler.py --once
```

Install and start both services:

```
sudo cp felicity-watch.service felicity-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now felicity-watch felicity-dashboard
systemctl status felicity-watch felicity-dashboard --no-pager
```

Both start at boot and restart themselves after a crash. The dashboard is then at `http://tsl-server:8080` from any device on the Tailscale network. Stop any scheduler still running on another machine.

Logs:

```
journalctl -u felicity-watch -f
journalctl -u felicity-dashboard -f
```

To deploy an update:

```
cd ~/Shine-Felicity_Automations && git pull
sudo systemctl restart felicity-watch felicity-dashboard
```

Edits to `schedule.json`, `savings.json`, `watchlist.txt` and `dashboard.html` need no restart.

## Settings in `.env`

| Variable | Required | Meaning |
|---|---|---|
| `FELICITY_USER`, `FELICITY_PASS` | Yes | Felicity portal login |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | For reports | Bot token and destination chat |
| `BATTERY_RESERVE_PCT` | No | State of charge treated as empty for backup time. Default 20. Match the inverter's discharge cut-off |
| `DASHBOARD_PASSWORD` | No | If set, the dashboard asks for it |
| `DASHBOARD_HOST`, `DASHBOARD_PORT` | No | Listen address and port. Default `0.0.0.0` and `8080` |
| `DASHBOARD_REFRESH_S` | No | Seconds between dashboard refreshes. Default 60, minimum 30 |
| `FELICITY_CA_BUNDLE` | No | Path to a PEM bundle if certificate verification fails |
| `FELICITY_PUBKEY` | No | Felicity's RSA login key, to skip reading it from the portal |
| `FELICITY_TOKEN_FILE` | No | Login token cache. Default `~/.felicity_token.json` |

Variables already set in the shell take precedence over `.env`.

## Commands

| Command | What it does |
|---|---|
| `python probe.py --list-plants` | Every plant: ID, name, device count |
| `python probe.py --watch watchlist.txt` | Report for the watchlist plants |
| `python probe.py --watch watchlist.txt --telegram` | The same, sent to Telegram |
| `python probe.py --watch watchlist.txt --json` | Full detail, including a per-device breakdown behind every total |
| `python probe.py --plants` | One line per plant for the whole fleet, from the device list only |
| `python probe.py --sn <serial>` | Summary of one device |
| `python probe.py --raw --sn <serial>` | Everything Felicity returns for one device |
| `python probe.py --telegram-chats` | Chats that have messaged the bot, with their IDs |
| `python probe.py --telegram-test` | Send a test message |
| `python scheduler.py --next [N]` | The next N scheduled run times |
| `python scheduler.py --once` | Send one report now |
| `python dashboard.py --demo` | Dashboard with simulated plants |

When a figure looks wrong, `--watch watchlist.txt --json` shows which devices fed it and where each battery's rated energy came from, and `--raw --sn <serial>` shows the source data.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Service shows `status=203/EXEC` | The path in the service file does not exist. Correct `WorkingDirectory` and `ExecStart`, copy the file again, then `sudo systemctl daemon-reload` and restart |
| `Felicity login failed: code=1002006` | Wrong password in `.env`, or Felicity changed its login key |
| `Felicity login failed: code=1002001` | The account is not activated; contact Felicity |
| Long pause with `retry 1/3` lines | Felicity is slow or the network is failing. The run gives up after three retries and reports why |
| `device list page N failed: code=...` | Felicity rejected the request. Wait and retry; if it persists, the account may be throttled |
| SSL certificate error | Do not disable verification. Point `FELICITY_CA_BUNDLE` at a bundle containing Felicity's intermediate certificate |
| `--telegram-chats` says "No chats yet" | Send `/start` to the bot (in a group, `/start@your_bot_name`) and run it again |
| Telegram `401 Unauthorized` | The bot token in `.env` is wrong or has a stray space or quote |
| Telegram `chat not found` | Wrong `TELEGRAM_CHAT_ID`, or the bot has not been started in that chat |
| Every report arrives twice | Two schedulers are running, or a cron job is still installed |
| `cannot listen on 0.0.0.0:8080` | Another program uses the port. Set `DASHBOARD_PORT` |
| "Not found in the portal" | A `watchlist.txt` entry matches no plant. Check it against `--list-plants` |
| "Savings are hidden" | No tariff is set in `savings.json` |
| A plant shows Offline | Felicity has had no data from any of its devices; the last data time is shown. Check the site's datalogger and internet |

## Security

Keep `.env` out of git and readable only by you (`chmod 600 .env`); add `.env`, `*.log` and `.venv/` to `.gitignore`. If a token or password is ever committed, revoke the bot token in BotFather (`/revoke`), change the Felicity password, and remove the file from the repository history.

The portal account used here can change inverter settings. This code never does, and any change to it must keep to the three read-only calls listed in `architecture.md`.

## Limitations

This uses the Felicity portal's internal interface, not an official API. It can change without notice, in which case reports fail with a Telegram notice and the dashboard shows an error. Rate limits are unknown, so intervals are deliberately conservative.

Load is probably under-read on IVGM hybrid inverters, which makes backup time optimistic on those plants. Month and year savings rely on energy counters that have not yet been checked against the portal. See section 10 of `architecture.md` for the full list.
