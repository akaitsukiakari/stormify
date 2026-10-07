# Rules reference

Every alert is archived to the dashboard no matter what. Rules decide which alerts **also push** to your phone.

A rule matches an alert when its **products** and **scope** match. Then:

- if the alert passes the rule's **filters**, the rule's `action` applies (`push` or `log`)
- if it fails the filters, the alert is just logged, with the reason shown in the feed

If several push rules match, the highest `priority` wins, along with that rule's templates and update behavior.

## Fields

| Field | Meaning |
|---|---|
| `name` | Label shown in the feed ("matched …") |
| `enabled` | `true`/`false` |
| `events` | Exact event names, case-insensitive. Empty = any. e.g. `["Tornado Warning", "Tornado Watch"]`. NHC products are listed [below](#national-hurricane-center-products). |
| `kinds` | Tiers: `Emergency`, `Warning`, `Watch`, `Advisory`, `Statement`, `Outlook`, `Message`, `Product`, `Other`. An emergency also counts as a Warning. `Product` is an NWS text product (AFD, HWO, RER, ...); scope those by `offices`, since they carry no zones. |
| `tags_any` | Derived tags, at least one required: `emergency`, `pds`, `considerable`, `destructive`, `observed`, `tornado-possible`, plus the NHC tags below |
| `scope` | `{"nationwide": true}`, or any mix of `offices` (`"BOU"` or `"KBOU"`), `zones` (UGC like `COZ039`, `COC005`), `states` (`"CO"`), `same` (FIPS like `"008005"`) |
| `filters.include_any` | At least one of these phrases must appear (OR) |
| `filters.include_all` | All of these phrases must appear (AND) |
| `filters.exclude_any` | None of these may appear |
| `filters.include_regex` | At least one regex must match |
| `filters.exclude_regex` | No regex may match |
| `filters.case_sensitive` | Default `false` |
| `min_hail_in` / `min_wind_mph` | Thresholds from the warning's tagged hail size / wind gust |
| `action` | `push` or `log` |
| `priority` | 1–5 (ntfy priority). Emergencies are always sent at 5. |
| `on_update` | `new_only`, `significant` (severity up, new PDS/emergency/observed tag, bigger hail/wind, upgrade; for NHC, stronger winds, a new watch/warning type, or becoming a major hurricane), or `any` |
| `on_cancel` | Push a "CANCELLED" note when something you were pushed is cancelled |
| `title_template` / `body_template` | Custom notification text (see below) |

Text filters search the event name, headline, NWS headline, description, instructions, area, and sender.

## Examples

PDS severe thunderstorm warnings anywhere:

```json
{"name": "PDS SVR", "events": ["Severe Thunderstorm Warning"], "scope": {"nationwide": true},
 "filters": {"include_any": ["PDS", "particularly dangerous situation"]}, "priority": 4}
```

Every special weather statement from Boulder except small hail:

```json
{"name": "BOU SPS", "events": ["Special Weather Statement"], "scope": {"offices": ["BOU"]},
 "filters": {"exclude_regex": ["\\b(pea|penny|dime|nickel)\\s+size"]}}
```

Boulder's Hazardous Weather Outlooks and Record Event Reports (needs `[nws_products]` in the config; event names are as shown in the dashboard's Event box):

```json
{"name": "BOU HWO + RER", "kinds": ["Product"], "events": ["Hazardous Weather Outlook", "Record Event Report"],
 "scope": {"offices": ["BOU"]}, "priority": 2}
```

## National Hurricane Center products

NHC products have office `NHC` (Central Pacific: `CPHC`). Each storm product threads by storm and product type, so `on_update` applies across advisories: `significant` pushes the first advisory and then only when winds go up or a new watch/warning type appears; `any` pushes every one.

| Event | Kind | What it is |
|---|---|---|
| `Tropical Cyclone Public Advisory` | Advisory | The main advisory, every 6 h, plus intermediates (tag `intermediate`) when watches/warnings are up. Drawn on the map with the forecast track. |
| `Tropical Cyclone Update` | Other | Short special updates, e.g. landfall or rapid strengthening |
| `Tropical Cyclone Forecast Advisory` | Advisory | Technical positions and wind radii |
| `Tropical Cyclone Discussion` | Other | Forecaster reasoning and key messages |
| `Tropical Cyclone Wind Speed Probabilities` | Other | Wind probability tables by location |
| `Tropical Weather Outlook` | Outlook | Basin outlook; the headline summarizes formation chances, e.g. `48h low 10% · 7d medium 40%` |

Tags:

- basin: `atlantic`, `east-pacific`, `central-pacific`
- storm class: `tropical-depression`, `tropical-storm`, `hurricane`, `major-hurricane` (111 mph+), `subtropical-storm`, `subtropical-depression`, `potential-tropical-cyclone`, `post-tropical`, `remnants`
- watches/warnings in effect (public advisories and updates): `hurricane-warning`, `hurricane-watch`, `ts-warning`, `ts-watch`, `surge-warning`, `surge-watch`, and `ww-changes` when the advisory changes any of them
- `intermediate` for intermediate public advisories

`min_wind_mph` works on storm products too (maximum sustained winds), e.g. only hurricanes: `"min_wind_mph": 74`.

The coastal watches and warnings themselves (`Hurricane Warning`, `Tropical Storm Watch`, `Storm Surge Warning`, ...) come through the NWS feed as usual.

To add the bundled tropical rules (Atlantic advisories on significant changes, every Atlantic update, and tropical watches nationwide) without touching your other rules:

```bash
stormify rules add --user scott --example tropical
```

`rules add` also takes a file of your own rules. Rules whose name you already have are skipped. Turn NHC polling off with `nhc_enabled = false`, or limit basins with `nhc_basins = ["at"]`, under `[general]` in config.toml.

## Templates

Templates use `{variable}` placeholders. Unknown variables come out blank. Put the important stuff first, because Android cuts off long notifications.

Defaults:

- title: `{status_prefix}{tags_prefix}{event} · {office}`, e.g. `UPDATE PDS Tornado Warning · BOU`
- body: `{threat}{area_short}` / `Until {expires}` / `{nws_headline}`
- body for text products: `{headline}` / `Issued {sent}`

Variables: `event kind office sender area area_short headline nws_headline severity certainty urgency tags tags_prefix hail hail_short wind wind_short threat status_prefix first_line sent expires sent_local sent_event sent_z expires_local expires_event expires_z`

NHC products default to title `{storm} · {product}` and body `{storm_stats}` / `{watches}` / `{nws_headline}`, e.g.

```
Tropical Storm Isaias · Advisory 4
45 mph · 1000 mb · ENE at 8 mph
Hurricane Watch, Storm Surge Watch, TS Watch
...HURRICANE AND STORM SURGE WATCHES ISSUED FOR PORTIONS OF THE NORTHERN GULF COAST...
```

Extra NHC variables: `storm product adv basin pressure movement location watches storm_stats`. For NHC, `expires` is the next scheduled advisory.

`sent` and `expires` follow your time display setting (your local / event local / Zulu, default all three).

## Sharing

`stormify rules export --user scott rules.json` and `stormify rules import --user friend rules.json`, or use **Export rules** in the dashboard. Importing replaces that user's rules.

## Testing without waiting for storms

```bash
stormify replay saved-alerts.json --user scott        # dry run: shows what would push
stormify replay saved-alerts.json --user scott --send # really pushes, marked TEST
```

To save a busy day for later replays: `curl -A "(stormify, you@example.com)" https://api.weather.gov/alerts/active > outbreak.json`
