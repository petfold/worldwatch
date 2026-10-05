"""Database connection and schema migrations."""

import sqlite3
from pathlib import Path

MIGRATIONS: list[str] = [
    # v1 — initial schema
    """
    CREATE TABLE IF NOT EXISTS sources (
        stream_id   TEXT PRIMARY KEY,
        class       TEXT NOT NULL,
        modality    TEXT NOT NULL CHECK (modality IN ('physical','economic','infrastructural','informational')),
        topic_tags  TEXT NOT NULL DEFAULT '[]',
        flavor      TEXT NOT NULL CHECK (flavor IN ('continuous','count','rate','categorical')),
        config      TEXT NOT NULL DEFAULT '{}',
        status      TEXT NOT NULL DEFAULT 'nursery'
                        CHECK (status IN ('nursery','active','quarantined','retired')),
        created_at  INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS raw_ring (
        stream_id   TEXT NOT NULL,
        cell        TEXT NOT NULL,
        ts          INTEGER NOT NULL,
        value       REAL,
        meta        TEXT,
        PRIMARY KEY (stream_id, cell, ts)
    );

    CREATE TABLE IF NOT EXISTS bins (
        stream_id   TEXT NOT NULL,
        cell        TEXT NOT NULL,
        scale       INTEGER NOT NULL,
        bin_start   INTEGER NOT NULL,
        n           INTEGER NOT NULL,
        vmin        REAL,
        vmax        REAL,
        vmean       REAL,
        m2          REAL,
        sketch      BLOB,
        PRIMARY KEY (stream_id, cell, scale, bin_start)
    );

    CREATE TABLE IF NOT EXISTS surprise (
        stream_id       TEXT NOT NULL,
        cell            TEXT NOT NULL,
        scale           INTEGER NOT NULL,
        bin_start       INTEGER NOT NULL,
        q_value         REAL,
        presence_q      REAL NOT NULL,
        precision       REAL NOT NULL,
        n_obs           INTEGER NOT NULL,
        tail_index      REAL,
        model_version   INTEGER NOT NULL,
        PRIMARY KEY (stream_id, cell, scale, bin_start)
    );

    CREATE TABLE IF NOT EXISTS model_state (
        stream_id   TEXT NOT NULL,
        version     INTEGER NOT NULL,
        state       BLOB NOT NULL,
        updated_at  INTEGER NOT NULL,
        pit_stat    REAL,
        PRIMARY KEY (stream_id, version)
    );

    CREATE TABLE IF NOT EXISTS alerts (
        alert_id    INTEGER PRIMARY KEY,
        opened_at   INTEGER NOT NULL,
        status      TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','acknowledged','resolved','false_positive')),
        severity    REAL NOT NULL,
        cell        TEXT NOT NULL,
        scale       INTEGER NOT NULL,
        evidence    TEXT NOT NULL DEFAULT '[]',
        label       TEXT
    );

    CREATE TABLE IF NOT EXISTS health (
        component   TEXT NOT NULL,
        ts          INTEGER NOT NULL,
        event       TEXT NOT NULL,
        detail      TEXT,
        PRIMARY KEY (component, ts, event)
    );

    CREATE TABLE IF NOT EXISTS schema_version (
        version     INTEGER PRIMARY KEY,
        applied_at  INTEGER NOT NULL
    );
    """,
    # v2 — per-(stream, cell, scale) Layer-0 models.
    # The v1 model_state sketch keyed only (stream_id, version); per-(stream,
    # cell, scale) modeling needs cell + scale in the key. No production data
    # yet, so recreate rather than ALTER.
    """
    DROP TABLE IF EXISTS model_state;
    CREATE TABLE model_state (
        stream_id   TEXT NOT NULL,
        cell        TEXT NOT NULL,
        scale       INTEGER NOT NULL,
        version     INTEGER NOT NULL,
        state       BLOB NOT NULL,
        updated_at  INTEGER NOT NULL,
        pit_stat    REAL,
        PRIMARY KEY (stream_id, cell, scale, version)
    );
    """,
    # v3 — presence channel per-source state (expected-report model + cursor).
    """
    CREATE TABLE IF NOT EXISTS presence_state (
        stream_id   TEXT PRIMARY KEY,
        state       BLOB NOT NULL,
        last_slot   INTEGER,
        updated_at  INTEGER NOT NULL,
        version     INTEGER NOT NULL
    );
    """,
    # v4 — observation keys already ingested, kept past consolidation.
    # raw_ring's PK only dedups inside the fine window; pollers re-fetch
    # overlapping history (USGS last hour, Wikipedia 3 d, Cloudflare 7 d, NWS
    # active alerts), which was re-counted into bins once its raw rows had
    # been folded and deleted. Pruned by first_seen, not ts, so a feed that
    # keeps returning old-dated rows is still recognised.
    """
    CREATE TABLE IF NOT EXISTS seen (
        stream_id   TEXT NOT NULL,
        cell        TEXT NOT NULL,
        ts          INTEGER NOT NULL,
        first_seen  INTEGER NOT NULL,
        PRIMARY KEY (stream_id, cell, ts)
    ) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS seen_first_seen ON seen (first_seen);
    INSERT OR IGNORE INTO seen (stream_id, cell, ts, first_seen)
        SELECT stream_id, cell, ts, CAST(strftime('%s', 'now') AS INTEGER) FROM raw_ring;
    """,
    # v5 — evidence store: what an observation was, for people (push text,
    # dashboard). Never read by detection. A fixed byte budget, oldest first
    # out (rowid order = arrival order); see worldwatch.evidence.
    """
    CREATE TABLE IF NOT EXISTS context (
        stream_id   TEXT NOT NULL,
        cell        TEXT NOT NULL,
        ts          INTEGER NOT NULL,
        rank        REAL,
        size        INTEGER NOT NULL,
        data        TEXT NOT NULL,
        UNIQUE (stream_id, cell, ts)
    );
    CREATE INDEX IF NOT EXISTS context_lookup ON context (stream_id, cell, ts);
    CREATE INDEX IF NOT EXISTS context_ts ON context (ts);
    """,
    # v6 — real-time path (ADR 0002). raw_ring.scored marks observations the
    # live scorer has consumed (the consolidator folds only those, so a crash
    # never loses one); live_windows holds open count windows (bucketed by
    # arrival); live_cells lists the cells a count stream scores, zeros included.
    """
    ALTER TABLE raw_ring ADD COLUMN scored INTEGER NOT NULL DEFAULT 0;
    UPDATE raw_ring SET scored = 1;
    CREATE TABLE IF NOT EXISTS live_windows (
        stream_id   TEXT NOT NULL,
        cell        TEXT NOT NULL,
        win_start   INTEGER NOT NULL,
        n           INTEGER NOT NULL,
        PRIMARY KEY (stream_id, cell, win_start)
    ) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS live_cells (
        stream_id     TEXT NOT NULL,
        cell          TEXT NOT NULL,
        last_event_ts INTEGER NOT NULL,
        PRIMARY KEY (stream_id, cell)
    ) WITHOUT ROWID;
    """,
    # v7 — active prober targets (ADR 0002 §E). Operational only: target IPs
    # never reach the evidence store, the API or pushes.
    """
    CREATE TABLE IF NOT EXISTS probe_targets (
        ip            TEXT PRIMARY KEY,
        cc            TEXT NOT NULL,
        kind          TEXT NOT NULL CHECK (kind IN ('ntp', 'anchor')),
        asn           INTEGER,
        discovered_at INTEGER NOT NULL,
        rejected      TEXT
    );
    """,
    # v8 — surprise.q_detect: for a discrete observation, the least extreme
    # PIT it is consistent with; NULL = q_value. q_value stays the randomized
    # PIT (uniform under a correct model: calibration, Layer 1); every tail
    # decision and display reads COALESCE(q_detect, q_value).
    """
    ALTER TABLE surprise ADD COLUMN q_detect REAL;
    """,
    # v9 — the pushes sent, for the notifier's budget (kind: alert, or extreme:
    # the ones that may wake the operator; stage: the alert's stage when pushed), and
    # an alert's stage (0 unconfirmed, 1 confirmed, 2 extreme), raised by escalation.
    """
    CREATE TABLE IF NOT EXISTS push_log (
        ts          INTEGER NOT NULL,
        alert_id    INTEGER,
        kind        TEXT NOT NULL CHECK (kind IN ('alert', 'extreme')),
        stage       INTEGER NOT NULL DEFAULT 0
    );
    ALTER TABLE alerts ADD COLUMN stage INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE alerts ADD COLUMN escalated_at INTEGER;
    CREATE INDEX IF NOT EXISTS push_log_ts ON push_log (ts);
    """,
    # v10 — the weekly report (worldwatch.api.digest): the facts an LLM read, and its
    # analysis; a few tens of KB a week.
    """
    CREATE TABLE IF NOT EXISTS digests (
        week_end    INTEGER PRIMARY KEY,
        created_at  INTEGER NOT NULL,
        digest      TEXT NOT NULL,
        analysis    TEXT,
        model       TEXT
    );
    """,
    # v11 — resource use as data (worldwatch.usage): bytes per source per UTC day,
    # and a sample of the slice's memory/CPU/network, disk and DB size every 10 min.
    """
    CREATE TABLE IF NOT EXISTS usage (
        component   TEXT NOT NULL,
        day         INTEGER NOT NULL,
        bytes_in    INTEGER NOT NULL DEFAULT 0,
        bytes_out   INTEGER NOT NULL DEFAULT 0,
        requests    INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (component, day)
    );
    CREATE TABLE IF NOT EXISTS resources (
        ts          INTEGER PRIMARY KEY,
        mem_current INTEGER,
        mem_peak    INTEGER,
        mem_max     INTEGER,
        cpu_usec    INTEGER,
        net_in      INTEGER,
        net_out     INTEGER,
        host_in     INTEGER,
        host_out    INTEGER,
        disk_free   INTEGER,
        disk_total  INTEGER,
        db_bytes    INTEGER
    );
    """,
    # v12 — the nursery's verdicts (worldwatch.layer0.nursery), and each poller's
    # conditional-request memo, so a restart doesn't re-download what it has.
    """
    CREATE TABLE IF NOT EXISTS calibration (
        stream_id   TEXT NOT NULL,
        ts          INTEGER NOT NULL,
        n           INTEGER NOT NULL,
        span_days   REAL NOT NULL,
        tv          REAL,
        lo_ratio    REAL,
        hi_ratio    REAL,
        status      TEXT NOT NULL,
        verdict     TEXT NOT NULL,
        PRIMARY KEY (stream_id, ts)
    );
    CREATE TABLE IF NOT EXISTS poll_state (
        stream_id       TEXT PRIMARY KEY,
        etag            TEXT,
        last_modified   TEXT,
        updated_at      INTEGER NOT NULL
    );
    """,
]


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    return conn


def connect(path: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Lightweight connection (row factory, WAL) without running migrations —
    for processes that attach to an already-initialized database (e.g. the API)."""
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
    )
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row[0] is not None else 0

    import time

    for i, sql in enumerate(MIGRATIONS, start=1):
        if i > current:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_version VALUES (?, ?)", (i, int(time.time())))
            conn.commit()
