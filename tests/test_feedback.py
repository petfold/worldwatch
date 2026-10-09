"""A push's Useful / Not useful buttons: signed URLs, the endpoint, the ntfy actions."""

import h3
import httpx
from fastapi.testclient import TestClient

from worldwatch.api import feedback
from worldwatch.api.app import create_app
from worldwatch.api.notify import NtfyConfig, send_ntfy
from worldwatch.db import open_db

NOW = 2_000_000_000


def _alert(conn, alert_id=7):
    cell = h3.latlng_to_cell(35.7, 139.7, 2)
    conn.execute(
        "INSERT INTO alerts (alert_id, opened_at, status, severity, cell, scale, evidence) "
        "VALUES (?, ?, 'open', 0.8, ?, 0, ?)",
        (alert_id, NOW, cell, '[{"stream_id": "q", "modality": "physical", "q_value": 0.999, "presence_q": 1.0}]'))
    conn.commit()
    return conn.execute("SELECT * FROM alerts WHERE alert_id = ?", (alert_id,)).fetchone()


def test_a_signature_is_for_one_alert_and_one_verdict(db):
    sig = feedback.sign(db, 7, "useful")
    assert len(sig) == 32 and feedback.verify(db, 7, "useful", sig)
    assert not feedback.verify(db, 8, "useful", sig)        # another alert
    assert not feedback.verify(db, 7, "not_useful", sig)    # the other answer
    assert not feedback.verify(db, 7, "delete_all", sig)    # not a verdict
    assert feedback.sign(db, 7, "useful") == sig            # the secret is kept


def test_the_endpoint_records_the_last_answer(tmp_path):
    path = tmp_path / "f.db"
    conn = open_db(path)
    _alert(conn)
    client = TestClient(create_app(path, now_fn=lambda: NOW))
    useful = feedback.url(conn, "", 7, "useful")
    assert client.post(useful).json() == {"alert_id": 7, "feedback": "useful"}
    assert client.post(feedback.url(conn, "", 7, "not_useful")).status_code == 200  # changed my mind
    row = conn.execute("SELECT feedback, feedback_at FROM alerts WHERE alert_id = 7").fetchone()
    assert row["feedback"] == "not_useful" and row["feedback_at"] is not None
    assert client.post(useful.replace(useful[-32:], "0" * 32)).status_code == 403
    assert client.post(feedback.url(conn, "", 99, "useful")).status_code == 404
    assert client.get(useful).status_code == 405  # POST only


async def test_pushes_carry_the_two_buttons_and_one_source_link(db):
    row = _alert(db)
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200)

    cfg = NtfyConfig(server="http://n", topic="t", dashboard_url="https://example.org:8001")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await send_ntfy(client, cfg, row, conn=db)
    actions = seen["actions"].split("; ")
    assert len(actions) == 3  # ntfy's maximum
    assert actions[0].startswith("view, Map, ")  # the first source link stays first
    assert actions[1] == ("http, Useful, https://example.org:8001/api/feedback/7/useful/"
                          f"{feedback.sign(db, 7, 'useful')}, method=POST, clear=true")
    assert actions[2].startswith("http, Not useful, https://example.org:8001/api/feedback/7/not_useful/")


async def test_no_buttons_without_a_public_dashboard(db):
    row = _alert(db)
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await send_ntfy(client, NtfyConfig(server="http://n", topic="t"), row, conn=db)
    assert "http," not in seen.get("actions", "")
