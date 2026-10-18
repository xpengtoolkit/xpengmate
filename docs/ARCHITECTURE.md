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

## Live vs historical overlap

The importer stops at the earliest live-logged row (or at a configured cutoff) so
the two writers never describe the same minute. Re-running it is idempotent: each
imported object is recorded by its natural key in `xpengmate_import`, and the
TeslaMate rows are re-created only when that key is new.
