# CLAUDE.md — Milma Malappuram AIME 8U Dashboard (Lightweight Edition)

This file is read by Claude Code at the start of every session. It contains
the persistent context for this project. Update it as the project evolves.

---

## What this project is

A web-based monitoring dashboard for one or more **PPI AIME 8U** 8-channel
Universal Analog Input Modules over **Modbus TCP/IP**. Polls every
configured RTU continuously, stores readings in a local SQLite database,
and serves a live view + historical trends + tabular history + Excel
export to operators in their browser.

**Deployment target: Radxa ROCK 5C SBC, 2 GB RAM, 128 GB NVMe SSD,
Linux + Docker.** Every architectural choice in this document is made
to keep the runtime memory footprint under ~200 MB and to avoid any
moving parts that aren't strictly necessary on a constrained device.

Users: plant operators and engineers at the Milma dairy facility,
Malappuram, viewing the dashboard on Chrome/Edge over the LAN.

---

## Hardware constraints — read before adding anything heavy

- **2 GB RAM total.** OS + Docker daemon take ~450 MB. Stack budget: ~200 MB.
- **No Node.js on the device.** Frontend is HTML + small JS libraries
  loaded from CDN-style local static files. No build step.
- **One container if possible, two at most** (backend + optional Caddy).
- **No PostgreSQL, no Redis, no message broker, no Celery.** Anything
  that runs as a separate process is forbidden unless there is no other way.
- **ARM64 architecture** — every Docker image must have an arm64 manifest.
  Stick to official images (python, caddy) or well-known multi-arch ones.

---

## Hardware context — AIME 8U RTU

The full user manual is at `docs/AIME-8U-Manual.pdf`. Refer to it before
changing register addresses or scaling.

- 8 universal analog input channels per device (TC, RTD, mV, V, mA).
- Modbus TCP port: **502**, default unit ID: **1**.
- Maximum **2 simultaneous Modbus TCP clients** per device — the poller
  must hold exactly **one persistent connection per device**.
- ADC sample time: 45 / 79 / 146 ms per channel (configurable on device).
  At 146 ms default, a full 8-channel scan inside the module takes ~1.17 s.
  **Do not poll faster than 1 Hz** unless you've also reduced the device
  sample time — otherwise you get the same value twice.

### Register map (manual Section 10 — verified working)

All live data is on **Input Registers (Function Code 0x04)**. Holding
registers are configuration-only and should not be touched by the poller.

| Address | Type     | Count | What |
|---------|----------|-------|------|
| 82      | INT16    | 1     | Ambient (terminal block / CJC) temp, 0.1 °C res |
| 1561    | INT16    | 8     | PV CH1..CH8 (scaled by per-channel resolution) |
| 1577    | BITMAP16 | 4     | Alarm-1..Alarm-4 status (each reg packs 8 channels) |
| 2001    | FLOAT32  | 16    | PV CH1..CH8 as IEEE-754 floats (2 regs/channel) |

**Float decoding:** big-endian word order (high reg first), big-endian
byte order within each register (ABCD). Use:
```python
struct.unpack(">f", struct.pack(">HH", hi, lo))[0]
```

**PV error sentinels** (treat as NULL in the DB, not as values):
- `-32768` → UNDER-RANGE
- `+32752` → OVER-RANGE
- `+32767` → SENSOR-OPEN

A reference reader script lives at `reference/aime8u_modbus_reader.py`.
The production poller must reuse its decoding logic and version
compatibility shim verbatim.

### Pymodbus version compatibility

Wrap every Modbus read in this shim — the unit-id keyword changed:

```python
try:
    rsp = client.read_input_registers(address=a, count=c, device_id=uid)
except TypeError:
    rsp = client.read_input_registers(address=a, count=c, slave=uid)
```

---

## Architecture

**One Python process. Inside it, three asyncio cooperating tasks.**

```
                ┌─────────────────────────────────────────────┐
                │  Single Python container                    │
                │                                             │
   AIME 8U ──┐  │  ┌──────────────────────────────────────┐   │
   AIME 8U ──┤  │  │ asyncio loop (uvloop)                │   │
   AIME 8U ──┤──┼─►│ ┌──────────┐ ┌─────────┐ ┌────────┐  │   │
       ...   │  │  │ │ Poller   │ │FastAPI  │ │Down-   │  │   │
   AIME 8U ──┘  │  │ │ tasks    │ │REST + WS│ │sampler │  │   │
                │  │ └────┬─────┘ └────┬────┘ └───┬────┘  │   │
                │  │      │            │           │       │   │
                │  │      └─pubsub─────┘           │       │   │
                │  │      │                        │       │   │
                │  │      └────► aiosqlite ◄───────┘       │   │
                │  └──────────────────┬───────────────────┘    │
                │                     │                         │
                │              ┌──────▼───────┐                 │
                │              │  /data/      │                 │
                │              │  aime.db     │  ← Docker vol   │
                │              │  aime.db-wal │                 │
                │              └──────────────┘                 │
                └─────────────────────────────────────────────┘
                                     ▲
                                     │ HTTP + WebSocket :8000
                                     │ (or via Caddy :443 with TLS)
                                ┌────┴────┐
                                │ Browser │
                                │  HTMX   │
                                └─────────┘
```

**Three concurrent jobs in one event loop:**

1. **Poller tasks** — one per RTU. Each holds a persistent Modbus TCP
   connection, polls every `poll_interval_s`, writes a snapshot row to
   SQLite, publishes the snapshot to the in-process pubsub.
2. **FastAPI server** — serves REST endpoints, WebSocket, and static
   HTML/JS/CSS files for the dashboard.
3. **Downsampler** — runs once a minute, computes 1-minute averages
   from raw data and writes to `readings_1min`. Once an hour, computes
   1-hour averages → `readings_1h`. Once a day, deletes raw rows older
   than the retention window.

No separate database container. SQLite lives in a Docker volume mounted
at `/data` inside the container.

---

## Tech stack — pin these versions

**Backend (single Docker container)**
- Python 3.12-slim base image (arm64)
- FastAPI ≥ 0.110
- uvicorn[standard] (pulls in uvloop and httptools — both critical for perf)
- pymodbus ≥ 3.6 (port from the reference script)
- aiosqlite (async SQLite driver)
- SQLAlchemy 2.x async (optional but kept for typed models)
- openpyxl (Excel export)
- pydantic v2 (FastAPI dependency anyway)
- Jinja2 (server-side HTML rendering for HTMX)
- PyYAML (read devices.yaml)

**Frontend (no build step, served as static files by FastAPI)**
- HTMX 2.x (loaded as a single .js file, ~14 KB)
- Alpine.js 3.x (~15 KB) for small client-side reactive bits
- uPlot (~45 KB) for time-series charts — fastest chart library available
- Pico.css or Simple.css (~10 KB) for clean default styling
- All bundled as local static files, no CDN at runtime

**Optional reverse proxy**
- Caddy 2 (arm64, ~25 MB) — only if you want TLS / basic auth.
  Otherwise expose FastAPI directly on port 80.

**Database**
- SQLite (built into Python, no install)
- WAL mode + `synchronous=NORMAL` + 4 MB page cache + `mmap_size=64 MB`

---

## Directory layout

```
milma-dashboard/
├── CLAUDE.md
├── README.md
├── docker-compose.yml
├── .env.example
├── .gitignore
├── docs/
│   └── AIME-8U-Manual.pdf
├── reference/
│   └── aime8u_modbus_reader.py
└── backend/
    ├── Dockerfile
    ├── pyproject.toml
    ├── devices.yaml                  # source of truth for RTU list
    └── app/
        ├── __init__.py
        ├── main.py                   # FastAPI app + lifespan
        ├── config.py                 # pydantic-settings
        ├── db.py                     # aiosqlite + SQLAlchemy engine
        ├── schema.sql                # full DDL run on first startup
        ├── pubsub.py                 # in-process broadcaster
        ├── modbus/
        │   ├── client.py             # AIME8UClient
        │   ├── decoders.py           # float / int / alarm decoding
        │   └── poller.py             # one async task per device
        ├── tasks/
        │   ├── downsampler.py        # 1m + 1h aggregates, retention
        │   └── scheduler.py          # registers periodic tasks
        ├── api/
        │   ├── devices.py
        │   ├── live.py
        │   ├── history.py
        │   └── export.py
        ├── ws/
        │   └── live.py               # /ws/live WebSocket
        ├── pages/                    # HTMX page handlers
        │   ├── live.py
        │   ├── trends.py
        │   └── history.py
        ├── templates/                # Jinja2 templates
        │   ├── base.html
        │   ├── live.html
        │   ├── trends.html
        │   ├── history.html
        │   └── partials/             # HTMX response fragments
        │       ├── channel_tile.html
        │       ├── device_card.html
        │       └── history_rows.html
        ├── static/
        │   ├── css/
        │   │   └── app.css
        │   ├── js/
        │   │   ├── htmx.min.js
        │   │   ├── alpine.min.js
        │   │   ├── uplot.iife.min.js
        │   │   └── app.js
        │   └── img/
        └── tests/
            ├── test_decoders.py
            ├── test_history_query.py
            └── test_downsampler.py
```

---

## Database schema (plain SQLite)

```sql
-- Static metadata
CREATE TABLE IF NOT EXISTS devices (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    host      TEXT NOT NULL,
    port      INTEGER NOT NULL DEFAULT 502,
    unit_id   INTEGER NOT NULL DEFAULT 1,
    location  TEXT,
    enabled   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS channels (
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    channel   INTEGER NOT NULL CHECK (channel BETWEEN 1 AND 8),
    name      TEXT NOT NULL,
    unit      TEXT,
    PRIMARY KEY (device_id, channel)
);

-- Raw 1 Hz readings — kept for the retention window only (default 7 days)
CREATE TABLE IF NOT EXISTS readings (
    ts         INTEGER NOT NULL,         -- unix epoch milliseconds
    device_id  INTEGER NOT NULL,
    channel    INTEGER NOT NULL,
    pv         REAL,                     -- NULL on PV error
    pv_error   INTEGER NOT NULL DEFAULT 0, -- 0=ok 1=under 2=over 3=open 99=read_fail
    alarms     INTEGER NOT NULL DEFAULT 0, -- 4-bit field AL1|AL2|AL3|AL4
    PRIMARY KEY (ts, device_id, channel)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_readings_dev_ch_ts
    ON readings (device_id, channel, ts DESC);

-- Ambient temperature (one per device per poll)
CREATE TABLE IF NOT EXISTS ambient_readings (
    ts         INTEGER NOT NULL,
    device_id  INTEGER NOT NULL,
    ambient_c  REAL,
    PRIMARY KEY (ts, device_id)
) WITHOUT ROWID;

-- 1-minute aggregates — kept for 90 days
CREATE TABLE IF NOT EXISTS readings_1min (
    bucket_ts  INTEGER NOT NULL,
    device_id  INTEGER NOT NULL,
    channel    INTEGER NOT NULL,
    pv_avg     REAL,
    pv_min     REAL,
    pv_max     REAL,
    sample_count INTEGER NOT NULL,
    PRIMARY KEY (bucket_ts, device_id, channel)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_readings_1min_dev_ch_ts
    ON readings_1min (device_id, channel, bucket_ts DESC);

-- 1-hour aggregates — kept for 2 years
CREATE TABLE IF NOT EXISTS readings_1h (
    bucket_ts  INTEGER NOT NULL,
    device_id  INTEGER NOT NULL,
    channel    INTEGER NOT NULL,
    pv_avg     REAL,
    pv_min     REAL,
    pv_max     REAL,
    sample_count INTEGER NOT NULL,
    PRIMARY KEY (bucket_ts, device_id, channel)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_readings_1h_dev_ch_ts
    ON readings_1h (device_id, channel, bucket_ts DESC);
```

**Why `WITHOUT ROWID`:** for tables where the primary key *is* the
natural row identifier and rows are small, this halves storage and
speeds up lookups significantly. It's a perfect fit for time-series
PKs of (ts, device_id, channel).

**Why epoch milliseconds, not TIMESTAMPTZ:** SQLite has no native
timestamp type. Storing as INTEGER is faster, smaller, and trivially
sortable. Convert to ISO strings only at the API boundary.

---

## SQLite tuning — apply on every connection open

These pragmas matter. Set them in `db.py` immediately after opening
each connection:

```python
await conn.execute("PRAGMA journal_mode=WAL;")        # critical: enables concurrent reads while writing
await conn.execute("PRAGMA synchronous=NORMAL;")      # safe with WAL, ~5x faster than FULL
await conn.execute("PRAGMA cache_size=-4000;")        # 4 MB page cache (negative = KB)
await conn.execute("PRAGMA mmap_size=67108864;")      # 64 MB memory-mapped I/O
await conn.execute("PRAGMA temp_store=MEMORY;")       # temp tables in RAM, not disk
await conn.execute("PRAGMA busy_timeout=5000;")       # wait up to 5s on lock contention
await conn.execute("PRAGMA foreign_keys=ON;")
```

Run `PRAGMA wal_checkpoint(TRUNCATE);` once an hour from the
downsampler task to keep the WAL file from growing unbounded.

Run `PRAGMA optimize;` once a day (also from the downsampler).

---

## Retention policy (downsampler logic)

| Table          | Resolution | Kept for | Approx rows / 8 RTUs |
|----------------|------------|----------|----------------------|
| readings       | 1 Hz raw   | 7 days   | 38 M                 |
| readings_1min  | 1 minute   | 90 days  | 8.3 M                |
| readings_1h    | 1 hour     | 2 years  | 1.1 M                |

Total disk: ~3 GB on full deployment. Plenty of room on 128 GB SSD.

**Downsampler runs four jobs:**
1. Every **1 minute**: aggregate the previous minute's raw rows into
   `readings_1min`.
2. Every **1 hour**: aggregate the previous hour's `readings_1min` rows
   into `readings_1h`. Then `PRAGMA wal_checkpoint(TRUNCATE);`.
3. Every **1 day at 03:00**: delete from `readings` where ts < now-7d,
   from `readings_1min` where ts < now-90d, from `readings_1h` where
   ts < now-2y. Then `PRAGMA optimize;` and `VACUUM;` (only if
   significant deletes happened — VACUUM rewrites the whole DB).
4. **Boot**: catch up any missed aggregation windows since last run.

---

## API surface

```
GET  /                                    # HTMX live page
GET  /trends                              # HTMX trends page
GET  /history                             # HTMX history page

GET  /api/devices                         # list devices + their channels
GET  /api/devices/{id}/diagnostics        # last_seen, success_rate_1h
GET  /api/live                            # most recent snapshot per device
GET  /api/history?device_id=&channel=&from=&to=&interval=raw|1m|1h
GET  /api/export.xlsx?device_id=&from=&to=  # streamed openpyxl workbook

WS   /ws/live                             # broadcasts every poll snapshot

# HTMX partial-fragment endpoints (return HTML, not JSON)
GET  /partials/live-tiles                 # full grid for first paint
GET  /partials/history-rows?...           # paginated table rows
```

**Interval auto-selection logic for /api/history:**
- range ≤ 2 hours        → query `readings` directly (raw)
- range ≤ 7 days         → query `readings_1min`
- range > 7 days         → query `readings_1h`
- explicit interval param overrides the auto choice

---

## Configuration

`backend/devices.yaml` — single source of truth for RTU configuration:

```yaml
poll_interval_s: 1.0
retention:
  raw_days: 7
  one_min_days: 90
  one_hour_years: 2

devices:
  - id: 1
    name: "Cold Storage 1"
    host: "192.168.1.20"
    port: 502
    unit_id: 1
    location: "CS-1 Panel"
    channels:
      - { channel: 1, name: "Top Probe",    unit: "°C" }
      - { channel: 2, name: "Mid Probe",    unit: "°C" }
      - { channel: 3, name: "Bottom Probe", unit: "°C" }
      # ... up to 8
  # add more devices here as they come online
```

On startup the backend syncs this file into the `devices` and
`channels` tables (upsert by `id`). Edit YAML, restart container, done.

`.env`:
```
DATA_DIR=/data
LOG_LEVEL=INFO
TZ=Asia/Kolkata
DASHBOARD_PASSWORD=changeme   # if Caddy basic auth is used
```

---

## Coding conventions

- **Python:** ruff for lint+format (line length 100), strict typing on
  public functions, docstrings on every module and public class.
- **Async everywhere.** No sync DB calls, no blocking IO in handlers.
  pymodbus is sync — wrap reads with `asyncio.to_thread`.
- **No silent failures.** A failed poll writes a row with `pv=NULL,
  pv_error=99` so gaps are explicit in the data.
- **Timestamps in DB are UTC milliseconds (INTEGER).** Convert to local
  (Asia/Kolkata) only at the API boundary.
- **HTMX-first for pages.** Server renders HTML; HTMX swaps fragments
  on user interaction. Use Alpine.js only for small purely-client-side
  things (toggling a panel, highlighting a row).
- **Charts only on the trends page.** Live view shows tiles + values,
  not sparklines (sparklines on every tile = unnecessary render cost
  on the SBC and on cheap operator browsers).

---

## Common commands

```bash
# Bring up the stack
docker compose up -d --build

# Tail logs
docker compose logs -f backend

# Shell into the backend
docker compose exec backend sh

# Open SQLite shell against the live DB
docker compose exec backend sqlite3 /data/aime.db

# Run tests
docker compose exec backend pytest -q

# Force a manual checkpoint / vacuum (during maintenance)
docker compose exec backend sqlite3 /data/aime.db "PRAGMA wal_checkpoint(TRUNCATE); VACUUM;"

# Backup (atomic, while running)
docker compose exec backend sqlite3 /data/aime.db ".backup /data/backups/aime-$(date +%Y%m%d).db"
```

---

## Performance budget — keep an eye on these

| Metric | Target on Radxa 5C 2GB | Watch out at |
|---|---|---|
| Backend RSS memory | < 200 MB | > 350 MB |
| Idle CPU | < 5 % | > 20 % |
| /api/live response time | < 50 ms | > 200 ms |
| /api/history (24h, 1 channel) | < 200 ms | > 1 s |
| /api/export.xlsx (24h, 8 ch) | < 2 s | > 10 s |
| WebSocket broadcast latency | < 100 ms | > 500 ms |
| SQLite db file size growth | ~50 MB/day per RTU | uncontrolled growth → downsampler is broken |

---

## Things to NOT do

- **No PostgreSQL, no TimescaleDB, no MariaDB, no Redis.** This is a
  2 GB device. SQLite is the answer.
- **No Node.js, no npm, no React, no Vite.** Frontend is HTMX +
  Alpine + uPlot, all served as static files.
- **No Celery, no APScheduler, no Dramatiq.** Periodic tasks are
  asyncio coroutines started in the FastAPI lifespan.
- **No multiple uvicorn workers.** One process. Multiple workers
  would each spawn pollers — chaos.
- **No new Modbus connection per poll.** One persistent connection
  per device. Reconnect only on failure.
- **No raw row dumps to the browser.** /api/history must downsample
  before responding. A typical chart needs ~500 points, not 86,400.
- **No PV error sentinels stored as numbers.** -32768 / 32752 / 32767
  → store as `pv=NULL` with `pv_error` code.
- **No floating point for timestamps.** INTEGER milliseconds only.
- **No sparklines on every live tile.** Save the chart rendering for
  the trends page.
- **No credentials in compose files.** Use `.env`, gitignore it.

---

## Deployment

- **Host:** Radxa ROCK 5C at `192.168.1.21`, SSH user `radxa`
  (ed25519 key auth). Sudo password: `radxa`, wrapped as
  `echo radxa | sudo -S sg docker -c '<docker cmd>'` because the
  user needs the `docker` group applied without a fresh login.
- **Project dir on device:** `/home/radxa/milma-dashboard`
- **Port mapping:** host `8080` → container `8000`. Dashboard at
  `http://192.168.1.21:8080/`.
- **Standard deploy loop:**
  ```
  scp <changed files> radxa@192.168.1.21:/home/radxa/milma-dashboard/...
  ssh radxa@192.168.1.21 "cd /home/radxa/milma-dashboard && \
    echo radxa | sudo -S sg docker -c 'docker compose up -d --build'"
  ```
- **Cache-busting:** bump `?v=N` on `<link>`/`<img>` URLs in
  `base.html` whenever a static asset's bytes change but its path
  doesn't — browsers aggressively cache the favicon and logo.

---

## UI / frontend conventions (as implemented)

The initial mock-driven buildout is complete. The rules below lock
in what has been shipped so future edits stay consistent.

### Layout target

- **Primary display: 1024 × 600.** Everything sizes off `vh`/`vw`
  and `clamp()` so the 4×2 tile grid fills the viewport without
  scrolling. Grid height: `calc(100vh - 13rem)` on the Live page.
- Tile grid: `repeat(4, minmax(0, 1fr))` × `grid-auto-rows: 1fr`,
  so CH1–CH4 occupy the top row and CH5–CH8 the bottom row.
- Live page currently **does not** show a per-device status strip
  (CONNECTED pill, last-seen, success rate, Acknowledge button).
  Diagnostics are still exposed via `/api/devices/{id}/diagnostics`
  for other consumers — do not delete the endpoint.

### Branding

- Product brand in nav: `<span class="brand-accent">Milma</span>
  Malappuram`, where `--accent: #3dd06b` paints "Milma" green.
- Company branding: `goose-icon.png` is the favicon/apple-touch
  icon; `goose-logo.png` sits at the right edge of the nav bar
  with class `.company-logo` (height pinned to `2.25rem` via
  `!important` to beat Pico's `img { height: auto }`).
- Icons in the nav tabs are inline SVG with `stroke: currentColor`
  so they inherit the tab's text color on hover/active. No emoji,
  no icon fonts, no external icon packs.

### Color & state language

CSS variables live at the top of `app.css`:

| Token | Value | Meaning |
|---|---|---|
| `--bg` | `#0d1117` | Page background |
| `--panel` | `#141a22` | Card / nav background |
| `--panel-2` | `#1a2230` | Active tab, header cells |
| `--accent` | `#3dd06b` | Milma green, live-dot "on" |
| `--tile-ok-bg` | `#0a0f15` | **OK state is black**, not green |
| `--tile-alarm-*` | amber `#e0a040` | Any alarm bit set |
| `--tile-err-*` | red `#e05a5a` | `pv_error != 0` (sensor fault) |
| `--tile-stale-*` | grey | No snapshot yet |

State is signalled with a 4 px left border + background tint, not
a full-card fill. The `value` text itself also recolors on
`.err` / `.alarm`.

### Alarm bit labels

Modbus alarm bits map to these labels across the whole UI:

```
bit 0 (AL1) → HiHi
bit 1 (AL2) → Hi
bit 2 (AL3) → Lo
bit 3 (AL4) → LoLo
```

Four pills are always rendered in that order; only the active
bits get `.al.on`. `window.alarmLabel()` in `app.js` uses the
same labels for history/export contexts.

### History page

- Table uses `.history-table` with centered headings and centered
  values (`text-align: center` on both `th` and `td`).
- `thead th` is `position: sticky; top: 0` inside a scroll
  container `#history-rows` with `max-height: calc(100vh - 12rem)`
  so the header stays frozen while the body scrolls.
- Column set is **pivoted** per device — one column per channel.
  The page handler passes `headers_json` to the template and the
  row-fragment endpoint produces matching cells.
- **Pagination (Next/Prev)**: the row fragment's `query_str` is
  built with `urllib.parse.urlencode({"from": iso, "to": iso,
  "device_id": id})`. Do **not** interpolate `datetime.isoformat()`
  directly into a query string — the `+00:00` tz offset gets
  decoded as a literal space by the server and the next-page
  request 422s. This bug was observed and fixed.
- **HTMX swap + frozen header**: after the initial `fetch()`
  renders the rows, subsequent Prev/Next clicks are HTMX swaps
  into `#history-rows`. The thead is rebuilt by an
  `htmx:afterSwap` listener in `history.html` that calls
  `injectHistoryThead()` — otherwise the column headers disappear
  on page 2+.

### Excel export (`/api/export.xlsx`)

The export must stay visually identical to the History page.
Current shape in `backend/app/api/export.py`:

- **Single sheet** named `History`. No multi-sheet "Export info"
  header any more.
- **Columns:** `Timestamp | <CH1 name (unit)> | ... | <CH8 name
  (unit)>`. Channel names + units pulled from the `channels`
  table (so operator renames from Settings carry through).
- **Header row style:** bold, white text (`FFFFFFFF`), solid
  fill `FF104861` (`#104861`), centered, row height `30` pt
  (~2× default). Implemented via `WriteOnlyCell` + `Font` +
  `PatternFill` + `Alignment`.
- **Body cells:** centered; PV values use number format `0.00`.
  Timestamps are rendered as `YYYY-MM-DD HH:MM:SS` in the
  configured local tz (`Settings.tz`, default Asia/Kolkata) —
  same format as the on-screen table.
- **Frozen first row:** `ws.freeze_panes = "A2"`.
- **AutoFilter:** `ws.auto_filter.ref = f"A1:I{last_row}"` over
  the full data range (not just the header) so the dropdowns
  actually filter.
- **Column widths:** A = 22 (timestamp), B–I = 14.
- **Row cap:** `ROW_CAP = 500_000`; beyond that the export stops
  reading and `X-Truncated: true` is returned in the headers.
- **Streaming:** the workbook is built in a `BytesIO`, then sent
  via `StreamingResponse(_chunked(buf), ...)` with a 64 KiB
  chunk generator. Do **not** pass the `BytesIO` directly —
  `StreamingResponse` would iterate it by newlines and split the
  binary .xlsx container on `0x0A` bytes.
- **Download filename:**
  `Temperature From YYYY-MM-DD HH-MM to YYYY-MM-DD HH-MM.xlsx`
  — local time, hyphens instead of `:` so it's valid on Windows
  filesystems.
- **SQL:** same pivoted `MAX(CASE WHEN channel=N THEN pv END)`
  as `/partials/history-rows`, just `ORDER BY ts ASC` (oldest
  first) and streamed via `session.stream()` so rows don't all
  land in memory before the workbook is written.

### Settings page

- Route: `GET /settings`, template `settings.html`.
- Renames go to `POST /api/channels/rename` with
  `{device_id, channel, name, unit?}`.
- **Persistence rule:** `startup.py`'s channel upsert uses
  `ON CONFLICT(device_id, channel) DO NOTHING` so operator
  renames survive container restarts. Do not change this back
  to `DO UPDATE` — it would clobber user edits with whatever is
  in `devices.yaml`.

### Files added/changed beyond the original layout

```
backend/app/api/channels.py        # POST /api/channels/rename
backend/app/api/export.py          # rewritten — pivoted single-sheet
backend/app/pages/settings.py      # settings page handler
backend/app/pages/history.py       # urlencode query_str for pagination
backend/app/templates/settings.html
backend/app/templates/history.html # htmx:afterSwap thead re-injection
backend/app/templates/partials/device_card.html  # dev-status strip removed
backend/app/static/css/app.css     # dark theme, 4×2 tile grid, logo sizing
backend/app/static/js/app.js       # window.alarmLabel() helper
backend/app/static/img/goose-icon.png
backend/app/static/img/goose-logo.png
```

Both router and page handler are wired up in `main.py`
(`channels_api.router`, `settings_page.router`).

### Bugs fixed in this phase (keep these fixed)

1. **History pagination 422'd on Next/Prev** — caused by raw
   `isoformat()` in the query string. Fix: `urllib.parse.urlencode`.
2. **Column headers vanished after HTMX swap** — caused by the
   initial `fetch()`-based thead injection not re-running on
   subsequent swaps. Fix: `htmx:afterSwap` listener.
3. **Logo rendered at native size** — Pico's `img { height: auto }`
   overrode our `height` rule. Fix: `!important` on height/max-height/
   width/max-width + `flex: 0 0 auto`.
4. **Bundled PNG changes didn't appear in browser** — aggressive
   favicon/logo caching. Fix: `?v=N` cache-bust on static asset URLs
   in `base.html`.
5. **`StreamingResponse(BytesIO)` for .xlsx** — the default
   iteration splits on newlines, which is wrong for zipped binary.
   Fix: explicit `_chunked(buf)` generator yielding 64 KiB chunks.
6. **Channel renames got wiped on restart** — old startup upsert
   was `ON CONFLICT ... DO UPDATE SET name = excluded.name`. Fix:
   `ON CONFLICT(device_id, channel) DO NOTHING` in `startup.py`.