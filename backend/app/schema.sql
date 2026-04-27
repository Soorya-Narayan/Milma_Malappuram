-- AIME 8U Dashboard — schema DDL (idempotent; safe to re-run).
-- Applied on startup by db.init_db().
-- Source of truth: CLAUDE.md § "Database schema".

-- ---------------------------------------------------------------------------
-- Static metadata — synced from devices.yaml on boot.
-- ---------------------------------------------------------------------------
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
    enabled   INTEGER NOT NULL DEFAULT 1,  -- 0 = operator-disabled
    PRIMARY KEY (device_id, channel)
);

-- ---------------------------------------------------------------------------
-- Raw 1 Hz readings — kept for the retention window only (default 7 days).
-- ts is unix epoch milliseconds (INTEGER); PV errors stored as NULL pv with
-- a non-zero pv_error code.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS readings (
    ts         INTEGER NOT NULL,
    device_id  INTEGER NOT NULL,
    channel    INTEGER NOT NULL,
    pv         REAL,
    pv_error   INTEGER NOT NULL DEFAULT 0,   -- 0=ok 1=under 2=over 3=open 99=read_fail
    alarms     INTEGER NOT NULL DEFAULT 0,   -- 4-bit field AL1|AL2|AL3|AL4
    PRIMARY KEY (ts, device_id, channel)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_readings_dev_ch_ts
    ON readings (device_id, channel, ts DESC);

-- Ambient / CJC temperature — one row per device per poll.
CREATE TABLE IF NOT EXISTS ambient_readings (
    ts         INTEGER NOT NULL,
    device_id  INTEGER NOT NULL,
    ambient_c  REAL,
    PRIMARY KEY (ts, device_id)
) WITHOUT ROWID;

-- ---------------------------------------------------------------------------
-- 1-minute aggregates — kept for 90 days.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS readings_1min (
    bucket_ts    INTEGER NOT NULL,
    device_id    INTEGER NOT NULL,
    channel      INTEGER NOT NULL,
    pv_avg       REAL,
    pv_min       REAL,
    pv_max       REAL,
    sample_count INTEGER NOT NULL,
    PRIMARY KEY (bucket_ts, device_id, channel)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_readings_1min_dev_ch_ts
    ON readings_1min (device_id, channel, bucket_ts DESC);

-- ---------------------------------------------------------------------------
-- 1-hour aggregates — kept for 2 years.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS readings_1h (
    bucket_ts    INTEGER NOT NULL,
    device_id    INTEGER NOT NULL,
    channel      INTEGER NOT NULL,
    pv_avg       REAL,
    pv_min       REAL,
    pv_max       REAL,
    sample_count INTEGER NOT NULL,
    PRIMARY KEY (bucket_ts, device_id, channel)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_readings_1h_dev_ch_ts
    ON readings_1h (device_id, channel, bucket_ts DESC);

-- ---------------------------------------------------------------------------
-- Alarm acknowledgements (Phase 7). Dashboard-side only — acking here does
-- NOT silence the device. One row per (ack event, device, channel).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alarm_acks (
    ts           INTEGER NOT NULL,   -- unix ms at time of ack
    device_id    INTEGER NOT NULL,
    channel      INTEGER NOT NULL,
    alarms_mask  INTEGER NOT NULL,   -- 4-bit mask acknowledged
    operator     TEXT
);

CREATE INDEX IF NOT EXISTS idx_alarm_acks_dev_ts
    ON alarm_acks (device_id, ts DESC);

-- ---------------------------------------------------------------------------
-- Runtime-editable settings (key/value). Operators change these from the
-- Settings page; they persist across restarts. Keys currently used:
--   "log_interval_s"   — poller cycle time in seconds (float stored as TEXT)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
