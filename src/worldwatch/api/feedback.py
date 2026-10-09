"""The operator's verdict on a push: was it worth the attention?

Each pushed alert carries two ntfy buttons, Useful and Not useful, that POST
from the phone to the public dashboard. A button's URL is signed for its one
alert and its one verdict (HMAC-SHA256 with a secret kept in the database and
created on first use): a copied link can only repeat that answer, and no token
travels in the notification. The last answer counts (one can change one's mind).

This is a different question from `alerts.label` (was it a real event?): these
verdicts are the ground truth for tuning what gets pushed (score, modalities,
harm, reach), by a small local model once enough have been given.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time

VERDICTS = ("useful", "not_useful")
LABELS = {"useful": "Useful", "not_useful": "Not useful"}


def _secret(conn: sqlite3.Connection) -> bytes:
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('feedback_secret', ?)",
                 (secrets.token_hex(32),))
    conn.commit()
    return bytes.fromhex(conn.execute(
        "SELECT value FROM settings WHERE key = 'feedback_secret'").fetchone()[0])


def sign(conn: sqlite3.Connection, alert_id: int, verdict: str) -> str:
    msg = f"{int(alert_id)}:{verdict}".encode()
    return hmac.new(_secret(conn), msg, hashlib.sha256).hexdigest()[:32]


def url(conn: sqlite3.Connection, base: str, alert_id: int, verdict: str) -> str:
    return f"{base.rstrip('/')}/api/feedback/{int(alert_id)}/{verdict}/{sign(conn, alert_id, verdict)}"


def actions(conn: sqlite3.Connection, base: str, alert_id: int) -> list[str]:
    """The two ntfy http actions: POST in the background, then clear the notification."""
    return [f"http, {LABELS[v]}, {url(conn, base, alert_id, v)}, method=POST, clear=true"
            for v in VERDICTS]


def verify(conn: sqlite3.Connection, alert_id: int, verdict: str, sig: str) -> bool:
    return verdict in VERDICTS and hmac.compare_digest(sign(conn, alert_id, verdict), sig)


def record(conn: sqlite3.Connection, alert_id: int, verdict: str, now: int | None = None) -> bool:
    """Store the verdict; False when there is no such alert."""
    cur = conn.execute("UPDATE alerts SET feedback = ?, feedback_at = ? WHERE alert_id = ?",
                       (verdict, int(time.time()) if now is None else now, alert_id))
    conn.commit()
    return cur.rowcount > 0
