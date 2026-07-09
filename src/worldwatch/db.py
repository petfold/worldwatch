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
]


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    _migrate(conn)
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
