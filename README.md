# ⚡ Stormify

Alerts-forward weather notifications. Stormify watches the National Weather Service's alert feed, runs every alert through your own rules, and pushes the ones you care about to your phone through [ntfy](https://ntfy.sh). Everything else lands in a searchable dashboard with a polygon map, so nothing is lost when Android's notification bundle overflows during an outbreak.

It's built to run on a Raspberry Pi Zero W.

## What works today (v0.1)

- **NWS alerts, nationwide.** Polls `api.weather.gov/alerts/active` every 60 s (politely, with `If-Modified-Since`). That feed includes local-office warnings and watches, SPC watches as issued by offices, NHC tropical warnings, and tsunami products.
- **NWS text products** (optional). Area Forecast Discussions, Hazardous Weather Outlooks, Record Event Reports, Public Information Statements, Local Storm Reports, or any AWIPS product code, for the offices you list under `[nws_products]` in the config. They show on the dashboard under the Product chip and push only if a rule asks for `"kinds": ["Product"]`.
- **Rules per user.** Match by event names, tiers, derived tags (emergency, PDS, considerable, destructive, observed), and scope (nationwide / offices / zones / states / FIPS). Filters support include-any (OR), include-all (AND), excludes, regex, and case sensitivity, plus hail and wind thresholds. Each rule pushes or just logs. See [docs/rules.md](docs/rules.md).
- **Smart updates.** NWS updates are grouped by VTEC event. Each rule chooses `new_only`, `significant`, or `any`, and can opt into cancellation notices. If an original and its update arrive in the same poll, you get one notification.
- **Outbreak safety valve.** A per-poll push cap; anything over it is logged, with one summary push. The first run pushes nothing.
- **Notification templates** that front-load what matters (`PDS Tornado Warning · BOU`, then `2" hail · 70 mph · Arapahoe, Douglas`) and show your local time, the event's local time, and Zulu.
- **Dashboard.** Login, alert feed with filters (time range, tier chips, pushed/logged, office and event autocomplete, text search, active only), Leaflet polygon map, tap-through links from notifications, test-push button, rule export.
- **Heartbeat** at `/api/health` for Home Assistant. See [docs/home-assistant.md](docs/home-assistant.md).
- **Replay** saved alert files through your rules, as a dry run or with real test pushes.
- Pluggable sources and delivery channels, so SPC/SWPC sources and APNs/email delivery can be added without touching the engine.

## Not built yet

Rule editor UI (bulk assign, copy scopes, presets), saved dashboard views, zone polygons for alerts without their own geometry, polygon images in notifications, SPC outlooks/MDs, SWPC space weather, hail alerting from radar/mPING, GPS location, nightly config backup to DreamHost.

## Quick start (development)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
stormify init --user scott --password changeme
stormify ntfy --user scott --topic my-test-topic --server https://ntfy.sh
stormify poll --once
stormify web            # http://127.0.0.1:8080
```

## On the Pi

See [docs/setup-pi.md](docs/setup-pi.md) and [docs/ntfy.md](docs/ntfy.md).

## Layout

```
stormify/
  sources/nws.py     NWS feed → Alert objects (office, VTEC thread, tags, hail/wind)
  sources/nws_products.py  NWS text products (AFD, HWO, RER, ...) → Alert objects
  rules.py           rule model + matching
  engine.py          archive, decide, handle updates/cancels, cap, deliver
  delivery/          Channel interface + ntfy adapter
  templates.py       notification text
  db.py              SQLite (WAL) storage, everything keyed by user
  poller.py          the always-on loop + heartbeat
  web/               Flask dashboard (served by waitress)
deploy/              install.sh, systemd units, example config
docs/                setup, ntfy, Home Assistant, rules reference
```

Data comes from the National Weather Service, which is public domain. Stormify isn't affiliated with or endorsed by NOAA/NWS.
