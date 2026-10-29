# XpengMate architecture

XpengMate is [TeslaMate](https://github.com/teslamate-org/teslamate) wired to log
an XPENG instead of a Tesla, plus a history importer that backfills the months the
car was never logged live.

```
              XPENG cloud                        historical feed
        (mTLS MQTT + REST)                 (EVConduit / export files)
                 |                                    |
                 v                                    v
    +---------------------+               +--------------------------+
    |  engine (Python)    |               |  history importer        |
    |  auth + telemetry   |               |  trips/charges/positions |
    |  Tesla provider API |               |  -> xpengmate Postgres   |
    +----------+----------+               +------------+-------------+
               |  HTTP (TESLA_API_HOST, TOKEN)         | SQL
               v                                       v
    +--------------------------------------------------------------+
    |  xpengmate (Elixir fork of TeslaMate)                         |
    |  car state machine -> positions, drives, charges, updates     |
    +----------+---------------------------------------------------+
               | MQTT                        | Postgres
               v                             v
         Home Assistant                 Grafana dashboards
```

## Why two processes

TeslaMate's car logger is a state machine that turns a stream of vehicle state
into drives, charging sessions, positions and state changes. The hard XPENG work
is elsewhere: app-tier mTLS, request signing, the tenant key pool, the login
chain, the MQTT JSON decode with partial-frame merging, and short-lived MQTT
credentials. That code already exists, in Python, in the XPENG Home Assistant
integration - and has been measured against the live car.

Porting it to Elixir would be a rewrite of the riskiest part of the system for no
functional gain. So the engine keeps it, and speaks TeslaMate's own "third-party
API provider" protocol to the fork. The fork therefore stays close to upstream:
its diff is branding, the history importer's schema concession, and configuration.

## Where live telemetry comes from

The engine has two interchangeable live sources, because XPENG allows **one
session per account**:

* **`XPENG_SOURCE=xpeng`** - the engine is the XPENG client: app-cert mTLS, the
  tenant key pool, account tokens, the MQTT subscription, token refresh and
  short-lived credential minting. This is the self-contained path; it needs the
  car's own credentials.
* **`XPENG_SOURCE=ha`** - the engine subscribes to the `state_changed` events of
  an xpengtoolkit integration over Home Assistant's websocket and rebuilds the
  same telemetry tree the native path produces. The integration owns the
  session (and its renewal); the engine only reads. This is the deployment in
  use while XpengMate does not hold an XPENG account of its own.

There is a third, preferred source when the Home Assistant integration is the
one holding the session: **`XPENG_SOURCE=provider`** relays the integration's own
provider (xpengtoolkit 0.6.16+). The engine forwards `/api/1/...` and the
streaming websocket with the integration's token, so no translation happens and
the integration keeps ownership of auth and renewal. `XPENG_SOURCE=ha` (entity
websocket) remains as the fallback when the provider option is not enabled.

All three feed the same local endpoint, so TeslaMate cannot tell them apart. The HA source deliberately takes its data timestamp from the
integration's `last_data_update`, so a sleeping car is reported asleep rather
than "freshly heard".

## The provider contract

TeslaMate 4.x can be pointed at any server that answers, in Tesla's shapes:

| Route | Purpose |
|---|---|
| `POST {TESLA_AUTH_PATH}/token` | the sign-in click; returns `access_token` + integer `expires_in` |
| `GET /api/1/products` | vehicle list |
| `GET /api/1/vehicles/:id` | one vehicle |
| `GET /api/1/vehicles/:id/vehicle_data` | all five state blocks |
| `GET /streaming/` | websocket telemetry (`data:subscribe_oauth` in, `data:update` out) |
| `GET /healthz` | diagnostics, no token |

Units are Tesla's, not XPENG's: **miles**, **mph**, **°C**, **bar**. TeslaMate
converts them again when it writes the database, so sending km would be a silent
1.609x error in every dashboard. `teslamate_mapping.py` owns those rules and the
tests pin them.

## Authentication and updates

* **Auth** is the integration's chain: the three app-tier PEM files, the tenant
  key pool sealed with this install's 32-hex device id, then account tokens.
  The engine loads an existing token/tenant pair, refreshes the refresh token on
  a timer, and mints short-lived MQTT credentials on demand.
* **Token renewal** rewrites the token file atomically (mode 0600), preserving
  the `json` + `jsonText` wrapper other XPENG tools expect.
* **Car software updates**: TeslaMate writes an `updates` row when
  `vehicle_state.car_version` changes. The engine polls XPENG's OTA status and
  feeds that version in, so the Updates dashboard and the Grafana updates
  dashboard show XPENG XOS transitions.
* **XpengMate's own update check** points at `xpengtoolkit/xpengmate` instead of
  `teslamate-org/teslamate`, so the footer does not advertise upstream releases.

## What the history importer writes

The importer reads EVConduit's parsed XPENG tables
(`xpeng_trips`, `xpeng_charging_sessions`, `vehicle_positions`,
`xpeng_tyre_pressures`) and writes rows into xpengmate's Postgres in TeslaMate's
own schema. It is honest about what it does not know:

* **Drives** carry distance, duration, max speed and the odometer pair. Elevation
  is `NULL`, never 0 - an XPENG export has no altitude and 0 would claim a flat
  drive.
* **Charging sessions** carry the two energies EVConduit computes (into the pack,
  and drawn at the wall), start/end SoC and duration. Cost is `NULL`.
* **Positions** come only from a real GPS logger (`vehicle_positions`); XPENG's
  data export contains no coordinates at all. A drive without a position has no
  map, which is the truth.
* **States** are derived from observed coverage: online while the logger or an
  event says the car was up, asleep across the gaps. `offline` is never invented.

### The one schema concession

`charging_processes.position_id` is `NOT NULL` in upstream TeslaMate because its
live logger always has the position a charge started at. A historical charge from
an export has no coordinates, so the importer drops that `NOT NULL` rather than
fabricating a latitude and longitude. Everything else keeps upstream's shape.

## The settings API and the history service

`/xpengmate` in the app is backed by two local services:

* the **engine settings API** (port 8202, compose network only, provider token):
  reads status and the (secret-free) configuration, writes the runtime source
  configuration, installs XPENG credentials as files, probes a provider or HA
  URL, and restarts the engine to apply a change;
* the **history service** (port 8100): stores the EVConduit source
  configuration, runs the importer as a subprocess on request, and answers
  freshness questions from the database (`counts`, newest row per table, lag to
  the source). It keeps its configuration and log on its own volume, mode 0600.

## Vehicle setups

Tyre choice, towing a trailer, a roof box and a fully loaded car all change
energy consumption, and an owner who records them can compare the drives made
under each. A setup is **one aspect** of the car - `tyres`, `towing`,
`roof_rack`, `loaded` or `other` - over an **inclusive** date range, with a free
text label and optional notes (`car_setups`; `TeslaMate.Setups.Setup` closes a
missing end date with `COALESCE(ended_at, 'infinity'::date)`).

The rows are per aspect rather than one row describing the whole car on
purpose. Tyres change twice a year, a roof box goes on for a holiday and a
trailer is towed for a day; a single "configuration" row would force those
changes to happen together. Independent ranges also let them overlap, which is
the truth - winter tyres and a roof box are on at the same time - and when two
rows of the same aspect overlap the most recently started one wins.

Nothing is written onto a drive. `TeslaMate.Setups.comparison/0` attributes each
drive to the newest setup of each aspect that covers it **at read time**, so one
drive can appear under tyres and under towing, and fixing a mistyped date range
re-tags the whole history instead of leaving stale tags on drive rows that
nothing would remember to clean up. A drive that no setup of an aspect covers
appears with a `NULL` label: the *Not recorded* baseline that new tyres, a
trailer or a roof box are measured against.

A drive belongs to the deployment's own calendar day, not the UTC day. The
`start_date` column holds UTC, so the day is
`(timezone(TZ, timezone('UTC', start_date)))::date` with `TZ` from the
environment (falling back to UTC when it is unset or unknown). A 07:00 drive in
Sydney falls on the previous UTC day, and it is the local day the owner fitted
the tyres on that decides which setup it belongs to.

There are two read paths over that one rule:

* the **Setups page** (`/setups`) lists the periods grouped by car and aspect,
  flags overlapping ranges, and renders `comparison/0`'s consumption table;
* the **Setups dashboard** in Grafana is the same comparison as a provisioned
  dashboard: a table per car, aspect and label, a bar chart of consumption per
  setup label coloured by aspect, a consumption-over-time chart split by tyre
  label, and a current-setup stat.

Distance, energy and consumption are converted to the owner's units in both, and
the energy is the same range-drop-times-efficiency estimate the Efficiency
dashboard uses.

## Ask assistant

`/ask` answers plain-English questions about the car's own data with an AI model.
It is off until an owner enables it and supplies a provider key; the key is
stored encrypted in `agent_settings` (a single row, `TeslaMate.Vault`-encrypted,
redacted from `inspect/1`) and can also come from `DEEPSEEK_API_KEY`, which
wins over the stored value. `agent_usage` holds one row per day with the
question count, because this stack has no Redis and the cap must survive a
restart; the increment and the cap are one statement, and the question is
counted **before** the provider runs.

The model never touches the database. `TeslaMate.Agent.Snapshot.load/1` reads
one bounded snapshot per question — at most 730 days, 2000 drives, 1000
charging sessions, the car rows, the recorded setups, parked gaps computed
between consecutive drives, and tyre pressures — and every tool in
`TeslaMate.Agent.Tools` is a pure function of that map. Two consequences matter:
a figure the model quotes is a figure a tool computed, and a tool can be tested
against a hand-written snapshot with no database and no model. The setups are
attributed with the same rule `TeslaMate.Setups.comparison/0` uses (the newest
setup of each aspect covering the drive's local day), so the assistant and the
Setups page agree.

`TeslaMate.Agent.Runner` is a DeepSeek/OpenAI-compatible chat-completions loop
with function calling: at most four model turns and nine tool calls, which is
the cost ceiling rather than a tuning knob. When the fourth turn is still tool
calls, one more call goes out with the tool list removed (and an instruction to
answer from the results already gathered), so the reader gets the answer the
model owes instead of an apology; tool calls the budget refuses still get a
`tool` message, because an assistant turn with an unanswered `tool_call` is an
invalid request. Tool results carry two private keys
that never reach the model — `_summary` (the "checked: …" line the page shows)
and `_chart` (the rendered SVG). Charts are not model output: the model names
one of four charts from a fixed catalogue and `TeslaMate.Agent.Chart` computes
the series from the same snapshot and renders the SVG from its own strings and
numbers, escaping every label.

The page is a LiveView. `TeslaMate.Agent.ask/2` starts the loop in a supervised
task (`TeslaMate.TaskSupervisor`) and sends `{:agent_event, event}` messages to
the page (`:progress`, `:tool`, `:chart`, `:answer`, `:meta`, `:error`, `:done`)
instead of streaming tokens over SSE; failures are mapped to sentences a reader
can act on. The assistant is read-only by construction: no tool writes, and the
system prompt tells the model so.

## Scheduled sync, export enrichment and the simulated live test

* **Daily sync**: `ops/xpengmate-history.cron` (or the systemd timer beside it)
  POSTs to the history service at 03:30 and appends to `history-sync.log`. The import is idempotent, so a failed or repeated run
  converges.
* **Export enrichment**: EVConduit's GPS logger records where the car was but
  not how full it was; XPENG's data export is the mirror image. When
  `XPENG_EXPORT_ZIP` names a zip (or a directory, newest zip wins), the importer
  joins them by exact second and fills `positions.battery_level`, `odometer`,
  `ideal/est_battery_range_km`, then re-derives each drive's start/end range.
  On the reference car that covered 24,150 of 25,249 positions (95.6%).
* **EVConduit data API (`mode=api`)**: `history/xpeng_api.py` consumes the
  versioned, cursor-paged `/api/v1/xpeng` surface (trips, charges, positions,
  tyre pressures, charge curves, state spans) with a per-key rate limit; the
  importer pages every collection to the end, so an API key now gets the full
  history without database access or manual export downloads.
* **Derived-column backfill**: `backfill_teslamate.py` fills the TeslaMate
  columns the historical import leaves empty (rated ranges, charger phases,
  efficiency, positions.drive_id, geocoded addresses) from the exports,
  positions and Nominatim, so the energy/capacity/location panels have data.
* **Outside temperature**: the engine's relay replaces
  `climate_state.outside_temp` with a configurable Home Assistant sensor, which
  TeslaMate then stores per position.
* **Sleep detection**: the relay maps the integration's
  `sensor.<prefix>_polling_status` (`Active`/`Silent`) onto Tesla's asleep
  semantics - `state: asleep` on the vehicle object and the Tesla
  `408 vehicle unavailable` response for `vehicle_data` - so TeslaMate enters its
  normal asleep/suspended cycle instead of staying online while parked. A drive
  can hold XPENG's single telemetry session and leave the polling status
  `Silent`/`Parked` for its whole length, so the sibling odometer and battery
  entities are a second arousal signal: a step in either keeps the car online
  for `awake_hold` (45 min) instead of losing the drive. Falls back to
  `XPENG_ASLEEP_AFTER` seconds of telemetry silence.
* **XPENG data exports**: `/xpengmate` uploads zips to the history service,
  `export_parser.py` streams the three 1 Hz CSVs into drives/charges/tyres/states
  and `merge_exports/1` reconciles overlapping 30-day windows; `export_import.py`
  inserts only what no other source already covers, records each file by sha256,
  and joins the export's SoC/odometer/range onto existing GPS positions.
* **Proof of the live path without XPENG**: `history/make_sim_frames.py` turns a
  real drive and a real charge from EVConduit into a replay file, and the
  engine's `XPENG_SIMULATE` mode pushes it through the same provider API a live
  connection uses. A throwaway stack (`scripts/sim/docker-compose.yml`) then logs
  it like a live car - on the reference run TeslaMate opened a drive, closed it
  at 292.31 km, and opened a charging process, all from the replay.

## Live vs historical overlap

The importer stops at the earliest live-logged row (or at a configured cutoff) so
the two writers never describe the same minute. Re-running it is idempotent: each
imported object is recorded by its natural key in `xpengmate_import`, and the
TeslaMate rows are re-created only when that key is new.
