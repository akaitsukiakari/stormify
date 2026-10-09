# ⚡ Stormify

Alerts-forward weather notifications. Stormify watches the National Weather Service's alert feed plus the National Hurricane Center, Storm Prediction Center and Space Weather Prediction Center, runs every alert through your own rules, and pushes the ones you care about to your phone through [ntfy](https://ntfy.sh). Everything else lands in a searchable dashboard with a polygon map, so nothing is lost when Android's notification bundle overflows during an outbreak.

It's built to run on a Raspberry Pi Zero W.

## What works today (v0.1)

- **NWS alerts, nationwide.** Polls `api.weather.gov/alerts/active` every 60 s (politely, with `If-Modified-Since`). That feed includes local-office warnings and watches, SPC watches as issued by offices, NHC tropical warnings, and tsunami products.
- **National Hurricane Center products.** Polls NHC's Atlantic, Eastern Pacific and Central Pacific feeds every 60 s (conditional requests): public advisories (including intermediates), tropical cyclone updates, forecast advisories, discussions, wind speed probabilities, and tropical weather outlooks. Each storm shows on the map at its current center with NHC's forecast track. Advisories are tagged with the watches/warnings in effect, so a rule can push on "significant" changes: stronger winds, or a new watch/warning type. Add the bundled tropical rules with `stormify rules add --user NAME --example tropical`.
- **Storm Prediction Center.** Day 1–3 convective outlooks (risk areas drawn in SPC's colors, with the narrative text) and mesoscale discussions (with their polygon, watch probability, and the offices they concern). Push on a Moderate/High risk day or a "watch likely" MD: `stormify rules add --user NAME --example spc`.
- **Space weather.** SWPC geomagnetic storm, solar radiation storm and radio blackout watches, warnings and alerts, tagged with their G/S/R level. Push G3 and up: `stormify rules add --user NAME --example space`.
- **NWS text products** (optional). Area Forecast Discussions, Hazardous Weather Outlooks, Record Event Reports, Public Information Statements, Local Storm Reports, or any AWIPS product code, for the offices you list under `[nws_products]` in the config. They show on the dashboard under the Product chip and push only if a rule asks for `"kinds": ["Product"]`.
- **Rules per user.** Match by event names, tiers, derived tags (emergency, PDS, considerable, destructive, observed), and scope (nationwide / offices / saved office groups / zones / states / FIPS). Filters support include-any (OR), include-all (AND), excludes, regex, and case sensitivity, plus hail and wind thresholds. Each rule pushes or just logs. See [docs/rules.md](docs/rules.md).
- **Rule editor** in the dashboard: office search by code or city, bulk set/add/remove offices across selected rules, copy a scope from one rule to others, saved office groups, a per-office view of which rules cover it, drag to reorder, JSON import/export, and a check of the draft against the last 48 hours.
- **Smart updates.** NWS updates are grouped by VTEC event. Each rule chooses `new_only`, `significant`, or `any`, and can opt into cancellation notices. If an original and its update arrive in the same poll, you get one notification.
- **Outbreak safety valve.** A per-poll push cap; anything over it is logged, with one summary push. The first run pushes nothing.
- **Notification templates** that front-load what matters (`PDS Tornado Warning · BOU`, then `2" hail · 70 mph · Arapahoe, Douglas`) and show your local time, the event's local time, and Zulu.
- **Map pictures on notifications.** With `public_url` set, pushes carry a small map of the alert's shape (SPC outlooks in SPC's colors), drawn by the Pi and cached.
- **Dashboard.** Login, alert feed with filters (time range, tier chips, pushed/logged, office and event autocomplete, text search, active only), Leaflet polygon map, tap-through links from notifications, test-push button. Watches and advisories that only list zones are drawn from the zones' outlines (dashed), looked up once and cached.
- **Heartbeat** at `/api/health` for Home Assistant. See [docs/home-assistant.md](docs/home-assistant.md).
- **Replay** saved alert files through your rules, as a dry run or with real test pushes.
- Pluggable sources and delivery channels, so new sources and APNs/email delivery can be added without touching the engine.

## Not built yet

Saved dashboard views, per-type scopes for NWS text products, zip code scopes, quiet hours, hail alerting from radar/mPING, GPS location, test alerts of any type from the dashboard, share links, nightly config backup to DreamHost.

## Quick start (development)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev,images]'
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
  sources/nhc.py     NHC RSS feeds → Alert objects (storm vitals, watches, track)
  sources/nws_products.py  NWS text products (AFD, HWO, RER, ...) → Alert objects
  sources/spc.py     SPC outlooks (GeoJSON + text) and mesoscale discussions → Alert objects
  sources/swpc.py    SWPC watches/warnings/alerts → Alert objects (G/S/R scale tags)
  zones.py           zone outlines for alerts without a polygon (looked up once, cached)
  images.py          map pictures for notifications (Pillow, optional)
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

Data comes from the National Weather Service, National Hurricane Center, Storm Prediction Center and Space Weather Prediction Center, which are public domain. Notification maps use OpenStreetMap tiles (© OpenStreetMap contributors), or CARTO's with `map_key`. Stormify isn't affiliated with or endorsed by NOAA/NWS.
