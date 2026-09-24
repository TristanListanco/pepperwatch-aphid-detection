-- Pepper Watch schema — the three normalized entities of manuscript §3.3.2.1.
--
-- detection_event  one leaf scan that was worth keeping: timestamp + image path
-- bounding_box     the spatial aphid-cluster coordinates and probability scores
-- system_log       hardware health metrics and diagnostic events
--
-- Two deliberate departures from a textbook normalization, both earning their keep:
--
-- 1. Bounding-box coordinates are stored NORMALIZED to 0..1 rather than in pixels.
--    The dashboard draws them on a canvas whose size depends on the phone holding
--    it, and the preview resolution is a config value that will change during
--    tuning. Normalized coordinates survive both; pixel coordinates would silently
--    misalign every historical overlay the first time preview_width moved.
--
-- 2. detection_event carries denormalized detection_count and max_confidence.
--    The Historical Detection Log lists hundreds of events with a per-event count,
--    and the KPI tiles aggregate a week of them. Recomputing those from
--    bounding_box on every page load is the one query on this device that would
--    actually be slow. The worker writes both inside the same transaction as the
--    boxes, and tests/test_db_concurrency.py asserts they never disagree.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS detection_event (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,

    -- ISO-8601 with an explicit UTC offset. Stored as TEXT because SQLite has no
    -- native date type and lexical order equals chronological order for this
    -- format, so the DESC index below is a plain b-tree scan.
    captured_at     TEXT    NOT NULL,
    site            TEXT    NOT NULL,

    -- Relative to config [media].root, never absolute: the database is copied off
    -- the device for analysis and absolute Pi paths would not resolve.
    image_path      TEXT,
    thumb_path      TEXT,

    -- Ties the event back to the exact frame the boxes were computed on.
    frame_seq       INTEGER NOT NULL,

    -- Provenance. A results table that cannot say which model and thresholds
    -- produced a row is not defensible, and these change constantly while tuning.
    model_version   TEXT    NOT NULL,
    conf_threshold  REAL    NOT NULL,
    iou_threshold   REAL    NOT NULL,

    inference_ms    REAL,
    preview_fps     REAL,
    inference_fps   REAL,

    detection_count INTEGER NOT NULL DEFAULT 0,
    max_confidence  REAL,
    damaged_area    REAL,       -- union fraction of frame covered by aphid_damage boxes
    severity        TEXT    NOT NULL DEFAULT 'none'
                    CHECK (severity IN ('none', 'low', 'moderate', 'high')),

    trigger         TEXT    NOT NULL DEFAULT 'auto'
                    CHECK (trigger IN ('auto', 'manual', 'state_change', 'heartbeat')),

    -- True when telemetry came from modelled values rather than real sensors, so
    -- development data can never be mistaken for field measurements.
    synthetic       INTEGER NOT NULL DEFAULT 0,
    archived        INTEGER NOT NULL DEFAULT 0,
    note            TEXT,

    CHECK (detection_count >= 0),
    CHECK (max_confidence IS NULL OR (max_confidence >= 0.0 AND max_confidence <= 1.0))
);

-- The gallery and the KPI tiles both read newest-first over a date range.
CREATE INDEX IF NOT EXISTS idx_event_captured_at ON detection_event (captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_archived    ON detection_event (archived, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_severity    ON detection_event (severity, captured_at DESC);
-- Makes the worker's idempotent upsert a lookup rather than a scan.
CREATE UNIQUE INDEX IF NOT EXISTS idx_event_frame ON detection_event (site, frame_seq, captured_at);


CREATE TABLE IF NOT EXISTS bounding_box (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL REFERENCES detection_event (id) ON DELETE CASCADE,

    class_id    INTEGER NOT NULL,
    class_name  TEXT    NOT NULL,
    confidence  REAL    NOT NULL,

    -- Top-left origin, normalized 0..1 against the frame that produced them.
    x           REAL    NOT NULL,
    y           REAL    NOT NULL,
    w           REAL    NOT NULL,
    h           REAL    NOT NULL,

    CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CHECK (w > 0.0 AND h > 0.0),
    -- Allow a small overhang: a box clipped by the frame edge is legitimate, a
    -- box entirely outside it is a un-letterboxing bug and should fail loudly.
    CHECK (x >= -0.01 AND y >= -0.01 AND x + w <= 1.01 AND y + h <= 1.01)
);

CREATE INDEX IF NOT EXISTS idx_box_event ON bounding_box (event_id);
CREATE INDEX IF NOT EXISTS idx_box_class ON bounding_box (class_name, confidence DESC);


CREATE TABLE IF NOT EXISTS system_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at     TEXT    NOT NULL,
    level         TEXT    NOT NULL CHECK (level IN ('debug', 'info', 'warn', 'error')),
    source        TEXT    NOT NULL,   -- worker | web | ap | power | maintenance
    event         TEXT    NOT NULL,   -- startup | telemetry | thermal_backoff | camera_lost | ...

    cpu_temp_c    REAL,
    cpu_percent   REAL,
    mem_used_mb   REAL,
    mem_total_mb  REAL,
    disk_free_mb  REAL,
    voltage_v     REAL,
    current_ma    REAL,
    power_w       REAL,
    battery_pct   REAL,
    throttled     INTEGER NOT NULL DEFAULT 0,

    synthetic     INTEGER NOT NULL DEFAULT 0,
    detail        TEXT
);

CREATE INDEX IF NOT EXISTS idx_log_logged_at ON system_log (logged_at DESC);
CREATE INDEX IF NOT EXISTS idx_log_event     ON system_log (event, logged_at DESC);
CREATE INDEX IF NOT EXISTS idx_log_level     ON system_log (level, logged_at DESC);


-- Schema version plus a single-writer advisory lock. The manuscript states the
-- writer twice, differently (§3.3 says the inference layer, §3.4.3 says the
-- backend); the code supports either, and this row records which one is live so
-- a misconfiguration is caught at startup instead of producing duplicate rows.
CREATE TABLE IF NOT EXISTS schema_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
