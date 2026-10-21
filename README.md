# XpengMate

**A self-hosted data logger and history browser for an XPENG — a rebranded,
XPENG-native fork of [TeslaMate](https://github.com/teslamate-org/teslamate).**

XpengMate keeps everything that makes TeslaMate useful — its database schema,
web app, MQTT/Home Assistant integration and Grafana dashboards — and replaces
the Tesla data path with an XPENG one:

* a **live engine** (Python) either authenticates to XPENG's cloud itself or
  relays the Tesla-compatible provider published by the
  [xpengtoolkit](https://github.com/xpengtoolkit/xpengtoolkit) Home Assistant
  integration (0.6.16+), then serves TeslaMate over Tesla's own "fleet provider"
  API;
* an optional **history import** pulls drives, charging sessions, states and GPS
  positions from [EVConduit](https://evconduit.com) — by API key for a normal
  account, or from its database for an operator;
* **XPENG GDPR export files** (rolling 30-day zips) can be uploaded in the app,
  merged, deduplicated and enriched into the history, so an owner with no
  EVConduit account still gets historical drives and charging sessions.

Nothing about the car is invented: a field XPENG does not report is stored as
`NULL`, not as zero, and the UI says so.

![XpengMate Home dashboard](docs/screenshots/grafana-home.png)

## What you get

* **Live logging** from the car: positions, state (online/asleep/offline),
  battery/range, charging sessions and drive summaries, at the cadence the
  XPENG platform exposes (MQTT telemetry through the provider).
* **Historical import** of drives, charges, GPS traces and states (EVConduit),
  or of drives/charges/tyres/states from XPENG's export zips.
* **TeslaMate's dashboards**, rebranded and 24 provisioned: Overview, Drives,
  Drive Stats, Charges, Charging Stats, Battery Health, Efficiency, Locations,
  Mileage, States, Timeline, Database Information and the reports folders.
* **A settings page** at `/xpengmate` in the app for the live source, XPENG
  credentials, outside-temperature sensor, history credentials/sync and export
  uploads — no SSH required for day-to-day changes.
* **Home Assistant integration** via MQTT (same topics as TeslaMate) plus a
  branded app UI, logo and Grafana theme.
* **EVConduit-optional**: the stack works with live data + export files alone;
  EVConduit just adds historical GPS/backfill.

## How it fits together

```
XPENG cloud / xpengtoolkit integration          EVConduit (API key or DB)
              |                                          |
              v                                          v
   engine :8201  (Tesla provider API)        history :8100 (import + freshness)
   - provider relay (recommended)             - EVConduit API/Postgres import
   - or native XPENG cloud engine              - XPENG export-zip import
   - or HA entity websocket                   - derived-column backfill
              |                                          |
              +------------------+-----------------------+
                                 v
                 xpengmate app :4100  (TeslaMate Phoenix app)
                                 |
                 +---------------+----------------+
                 v                                v
          Grafana :3003                   MQTT -> Home Assistant
```

Deeper detail — provider contract, the one schema concession, the live-source
alternatives — is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quick start

```bash
git clone https://github.com/xpengtoolkit/xpengmate
cd xpengmate
cp .env.example .env
chmod 600 .env
# fill in ENCRYPTION_KEY (openssl rand -hex 32), DATABASE_PASS,
# GRAFANA_ADMIN_PASSWORD, XPENG_PROVIDER_TOKEN (openssl rand -hex 24),
# RELEASE_COOKIE (openssl rand -hex 24)
mkdir -p data/engine/{certs,secrets,state}

DOCKER_BUILDKIT=1 docker-compose build     # BuildKit is required (COPY --chmod)
docker-compose up -d
docker-compose ps
```

Open `http://<host>:4100`, sign in, and the car appears once a live source or an
import has written data.

| Port | Service |
|---|---|
| 4100 | XpengMate web app (sign-in, `/xpengmate` settings, dashboards in Grafana) |
| 3003 | Grafana (admin password from `.env`) |
| 8201 | Engine's Tesla-compatible provider API (loopback; the app talks to it) |
| 8100 | History service (loopback; import, sync, freshness, export uploads) |

```bash
curl http://localhost:8201/healthz     # engine: {"ok": true, ...}
curl -I http://localhost:4100/         # app
./scripts/smoke-test.sh                # assertions for engine, app, history, Grafana
```

## Live data: three ways

XPENG allows **one live session per account**, so these are alternatives, not
layers. Use a dedicated account (share the car to it) for XpengMate.

| Source | How | When to use |
|---|---|---|
| `provider` *(recommended)* | The xpengtoolkit 0.6.16+ integration's own Tesla-compatible provider is enabled in Home Assistant (port 8200); XpengMate relays it, swapping in the provider token. | You already run the xpengtoolkit integration and want one owner of the XPENG session. |
| `xpeng` | The engine authenticates to XPENG's cloud itself with the app-tier mTLS certificates, tenant key pool and account tokens. | You want XpengMate to own the session with no Home Assistant in the path. |
| `ha` | The engine subscribes to `state_changed` over Home Assistant's websocket and reconstructs the telemetry tree from the integration's entities. | Fallback when the provider option is not enabled. |

```yaml
# provider relay (recommended)
XPENG_SOURCE=provider
XPENG_PROVIDER_UPSTREAM_URL=http://<ha-host>:8200
XPENG_PROVIDER_UPSTREAM_TOKEN=<provider token>

# or native XPENG cloud
XPENG_SOURCE=xpeng

# or HA entity websocket
XPENG_SOURCE=ha
XPENG_HA_URL=http://<ha-host>:8123
XPENG_HA_TOKEN=<long-lived read-only token>
XPENG_HA_ENTITY_PREFIX=xpeng_800783
```

The relay forwards TeslaMate's read routes and the streaming websocket to the
integration's provider, answers the token endpoint locally, and adds three
pieces of XPENG-specific enrichment:

* the **outside temperature** comes from a Home Assistant sensor when configured;
* the `XOS` firmware prefix is stripped so version changes are recorded once
  (not as fake updates);
* a **parked car is reported as asleep**: the provider keeps saying "online" and
  its own threshold is two hours, so the relay watches the integration's
  `sensor.<prefix>_polling_status` (`Active` = awake, `Silent` = parked and
  quiet) and returns the Tesla "vehicle unavailable" response while asleep,
  which is what makes TeslaMate enter its normal sleep/suspend behaviour.
  Configure with `XPENG_SLEEP_STATUS_URL`, `XPENG_SLEEP_STATUS_TOKEN` and
  `XPENG_SLEEP_STATUS_ENTITY`; without it the fallback is
  `XPENG_ASLEEP_AFTER` seconds of telemetry silence (default 900). The status
  URL/token must point at the **same Home Assistant as the provider** — if it
  points at another host, that host's "Silent" can put the logger to sleep while
  the real provider still has fresh data (the engine logs a warning when the
  hosts differ).

## Settings in the app

`/xpengmate` is the configuration page; changes are applied by restarting the
engine (a few seconds).

* **Live source** — pick provider/native/HA, enter the provider URL and token,
  "Test source" before saving, and set the **outside-temperature entity**
  (e.g. `sensor.xpeng_800783_weather_at_vehicle`).
* **XPENG credentials (native engine)** — upload the three app-tier PEMs, the
  tenant key pool and the token pair. Files are written mode `0600` on the
  engine's volume and never shown back.
* **History from EVConduit** — configure the source, "Sync now", and read data
  age: per-table row counts, newest row per table and the lag to the EVConduit
  source. See the credential table below.
* **XPENG data export files** — upload up to 12 zips at once (normal multipart
  form, with feedback listing the selected files); the result reports new
  files, duplicates, added/extended/covered drives and charges, and explains
  when nothing new was needed.

## XPENG credentials (native engine)

| Path | What it is |
|---|---|
| `data/engine/certs/{ca-chain.crt,client-cert-0.pem,client-key.pem}` | app-tier mTLS identity extracted from an XPENG APK (3.7.0–3.12.0) |
| `data/engine/secrets/xpeng_tenant.json` | tenant key pool, sealed with this install's device id |
| `data/engine/secrets/xpeng_tokens.json` | the account's access/refresh token pair |

These come from [xpengtoolkit](https://github.com/xpengtoolkit/xpengtoolkit);
its README and `docs/UNATTENDED_RENEWAL.md` are the authority. The engine's
device identity is fixed by the tenant file and never regenerated. Keep
`XPENG_ALLOW_OFFLINE=false` so a missing credential is a startup failure rather
than a car that quietly never logs.

## History from EVConduit

The history service imports drives, charging sessions, GPS positions, tyre
pressures and states, and reports how stale the source is. Two credential sets:

| Who you are | What to enter | What you get |
|---|---|---|
| **Normal EVConduit account** | API base URL + your **xpengcardata API key** (EVConduit → History → API) | newest 200 trips/charging sessions with each trip's GPS track — a rolling recent window, not a full backfill |
| **Self-hosting EVConduit** | the EVConduit **Supabase URL + service-role key** | the complete history; operator credential that bypasses RLS — treat as a password, rotate/scope it |
| **No EVConduit** | leave unconfigured | live logging plus XPENG export-file imports; no historical GPS before live logging began |

* Every imported row is tracked in `xpengmate_import`, so re-runs replace their
  own rows and never touch what the live logger wrote.
* Positions are only real GPS points from the owner's logger; elevation is not
  available from XPENG and stays `NULL`.
* Daily sync: `ops/xpengmate-history.cron` (a systemd timer sits beside it) POSTs
  to the history service; `scripts/sync-history.sh` does the same from a shell.
* `docker-compose run --rm history` still works as a one-shot run.

## Importing XPENG data exports

![XPENG export import](docs/screenshots/export-import.png)

XPENG's GDPR exports are rolling 30-day windows, so "upload a handful" is the
normal workflow. The app parses the three 1 Hz datasets and merges windows:

* drives (speed-integrated distance, odometer, SoC bounds, peak power), charging
  sessions (energy at the wall and into the pack, SoC/range bounds), hourly tyre
  pressures and online/asleep spans;
* two records that overlap → the longer wins; two halves cut at a window edge →
  stitched with distance/energy summed; nothing is double counted;
* records already covered by richer sources (live logger/EVConduit) are skipped;
* export SoC/odometer/range is joined onto existing GPS positions;
* files are remembered by content hash, so re-uploading is a no-op.

Deliberately **not** imported: positions. The export has no coordinates, and the
importer will not invent them. A zero-row result is normal when the windows are
already covered — the card says so.

## Filling TeslaMate's derived columns

`history/backfill_teslamate.py` (run inside the history container) completes the
columns TeslaMate normally derives live: drive/charge rated ranges (from the
exports, positions or SoC), `charger_phases` so AC/DC is not "everything DC",
rated range on charge samples (battery capacity/cycles), `cars.efficiency`,
`positions.drive_id`, and start/end/charge addresses via Nominatim (cached, 1
req/s, `--no-geocode` to skip). It reads every uploaded zip and is idempotent:

```bash
docker exec xpengmate_history_1 python backfill_teslamate.py
```

It also clamps ranges to the pack's physical full range at each SoC, so the
energy, battery-health and capacity panels stay consistent.

## Outside temperature

The provider's own weather value is replaced by a Home Assistant sensor when
configured (*Outside temperature entity* in settings, or
`XPENG_OUTSIDE_TEMP_ENTITY`). TeslaMate stores it per position, so drive/charge
temperature averages and the *Temperature – Driving Efficiency* panel follow
that sensor instead. No sensor configured → the provider's value is kept.

## Dashboards

TeslaMate's dashboards, rebranded and extended:

* **24 provisioned dashboards** in folders `XpengMate`, `XpengMate Internal` and
  `XpengMate Reports`; the datasource display name is `XpengMate` while its uid
  deliberately stays `TeslaMate` so every panel reference keeps working.
* **Home** dashboard: the supplied XPENG line-up photo with the XpengMate
  wordmark, `XPENG · drives, charges, battery and GPS history` tagline and a
  *"A fork of TeslaMate"* credit, beside the dashboard list.
* **Energy** in kWh/100km: net = drive range delta × efficiency; gross = net ×
  the measured charging-loss factor (so gross is at or above net).
* **Battery Health**: pack is *91 kWh gross / 87.5 kWh usable* (3.5 kWh buffer).
  "Usable (new)" is pinned to 87.5; "Usable (now)" is
  `Σ charge energy into pack ÷ Σ SOC increase` over recent sessions with a ≥5%
  rise — independent of the car's optimistic range estimate.
* **Charges** shows the AC/DC split from `charger_phases` (86 AC / 19 DC on the
  reference car). The EVConduit import writes the phase itself (AC = 1, DC =
  NULL) and every sync runs the derived-column backfill afterwards, so the split
  survives re-imports instead of degrading to "everything DC" and the **Locations** page groups drives by geocoded address
  (2 states, 12 cities, Sydney/Taree/Forster/Gold Coast…).
* The **Firmware** stat reads the latest `updates` version with the `XOS `
  prefix stripped (currently `5.9.5.8909`), so a source change cannot look like
  two updates for one firmware.

## Verification status

Measured on the reference car (XPENG G6 RWD, VIN `…800783`):

| Check | Result |
|---|---|
| Engine provider contract (token, products, vehicle_data, websocket) | offline test suite passes |
| Live logging through the xpengtoolkit 0.6.16 provider relay | upstream healthy, stream connected, positions written |
| Parked-car sleep detection | relay reports `asleep` from `polling_status: silent`; TeslaMate logs `Start / :asleep`, state row written; wakes on `Active` |
| Sign-in through the engine | `Starting logger for 'XPENG 800783'`, `Real online detected` |
| History imported (EVConduit, 2026-08-13 → 2026-10-17) | 343 drives, 25,995 positions, 104 charges, 22,957 charge samples, 645 states |
| Export enrichment (positions with SoC/odometer/range) | ~93% of imported positions |
| Derived columns backfill | 328 drives with rated ranges, 85 AC / 19 DC sessions, 1.65 MWh net energy |
| Locations | 2 states (NSW 95 + QLD 4), 12 cities, top destinations populated |
| Battery health | new 87.5 kWh, now ≈85.7 kWh (~98%), AC/DC pie |
| Efficiency | net 19.3, gross 20.4, rated 16.6 kWh/100km |
| Outside temperature | relay answers 19.0 °C from `sensor.xpeng_800783_weather_at_vehicle` |
| Firmware | 5.9.5.8909 (single update row) |
| Live path end to end (`XPENG_SIMULATE` replay) | 292 km drive opened/closed, charge opened, 1,279 positions |
| Native XPENG cloud source | built and unit-tested; needs a dedicated account's credentials |

## What is approximate

These follow from what XPENG exposes, not from how the fork is wired:

* **Live charge energy** is estimated from battery percentage × usable capacity;
  XPENG does not publish a session's kWh.
* **Outside temperature** without a configured sensor is the weather at the car,
  not an ambient sensor reading.
* **Drive power** is `0` while driving (XPENG does not expose motor power to the
  integration); charging power is real.
* **Elevation, ascent, descent** are unavailable live and in exports.
* **AC vs DC** is inferred from charger phases/peak power.
* **Battery capacity** is a computed estimate (± few kWh), not a BMS reading.

## Repository layout

```
elixir/            TeslaMate (Phoenix app), rebranded + XPENG settings/controller
grafana/           TeslaMate's dashboards, rebranded, plus the XPENG hero image
engine/            XPENG engine: provider relay, native cloud client, HA source,
                   settings API; Tesla-compatible provider on :8201
history/           EVConduit/export import, derived-column backfill, freshness API
docs/              architecture, screenshots
scripts/           deploy + smoke-test helpers
docker-compose.yml the whole stack
```

## Upstream, licence and attribution

XpengMate is a **fork of [TeslaMate](https://github.com/teslamate-org/teslamate)**,
© the TeslaMate contributors, distributed under the **AGPL-3.0-or-later** (see
[LICENSE](LICENSE) and [NOTICE](NOTICE)); modified files are marked as modified
by this fork. TeslaMate's schema, Phoenix app, MQTT topics and Grafana dashboards
remain the work of that project — the XPENG data engine, history import and
rebranding are this fork's additions. "TeslaMate" and "XPENG" are used only to
describe the origin/compatibility; XpengMate is not affiliated with Tesla or
XPENG.

The engine vendors code from [xpengtoolkit](https://github.com/xpengtoolkit/xpengtoolkit)
(AGPL-3.0-or-later); the exact revision is pinned in
[engine/VENDOR.md](engine/VENDOR.md). Historical data may come from
[EVConduit](https://evconduit.com) under that service's terms.
