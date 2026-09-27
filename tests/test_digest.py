"""The weekly report: every significant deviation, alerted or not, read by an LLM."""

import json

import h3
import httpx

from worldwatch.api.digest import build_digest, markdown, render_digest, run_digest

WEEK_END = 2_000_000_000 - 2_000_000_000 % 86400


def _surprise(db, stream, cell, t, q):
    db.execute("INSERT INTO surprise (stream_id, cell, scale, bin_start, q_value, presence_q, precision, "
               "n_obs, tail_index, model_version) VALUES (?, ?, -1, ?, ?, 1, 1, 1, NULL, 1)",
               (stream, cell, t, q))


def _week(db):
    cz = h3.latlng_to_cell(50.1, 14.4, 3)
    for i in range(500):  # a quiet week of radiation, then two low readings (the harmless way)
        _surprise(db, "eurdep_gamma", cz, WEEK_END - 6 * 86400 + i * 600, 0.5)
    _surprise(db, "eurdep_gamma", cz, WEEK_END - 3600, 0.0001)
    _surprise(db, "eurdep_gamma", cz, WEEK_END - 7200, 0.0002)
    _surprise(db, "usgs_m45", h3.latlng_to_cell(35.7, 139.7, 3), WEEK_END - 86400, 0.99999)
    db.commit()


def test_the_digest_lists_deviations_both_ways_with_chance(db, sources):
    _week(db)
    d = build_digest(db, sources, WEEK_END)
    assert "### Radiation, Europe (EURDEP)" in d
    assert "Readings 502; beyond 1 in 1,000: 2 (chance: 0.5)" in d
    assert "- Czechia (50.4N 14.9E): low" in d and "[harmless way]" in d  # kept, never alerted
    assert "- Japan (35.8N 140.1E): high" in d
    assert "## Alerts: 0 opened" in d


def test_the_weekly_report_is_analysed_stored_and_announced_once(db, sources, monkeypatch):
    _week(db)
    monkeypatch.setenv("WW_ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("WW_NTFY_TOPIC", "t")
    monkeypatch.setenv("WW_NTFY_SERVER", "http://n")
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "api.anthropic.com":
            body = json.loads(request.content)
            assert body["messages"][0]["content"].startswith("# Worldwatch weekly digest")
            assert request.headers["x-api-key"] == "test-key"
            return httpx.Response(200, json={"content": [{"type": "text", "text": "## Standing out\n- Two low readings in Czechia: chance."}]})
        assert request.headers["priority"] == "2"  # silent
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert run_digest(db, sources, now=WEEK_END + 6 * 3600, client=client) == WEEK_END
    assert run_digest(db, sources, now=WEEK_END + 7 * 3600, client=client) is None  # once a week
    assert seen == ["api.anthropic.com", "n"]
    page = render_digest(db)
    assert "Two low readings in Czechia" in page and "Czechia (50.4N 14.9E)" in page


def test_without_a_key_the_digest_alone_is_kept(db, sources, monkeypatch):
    monkeypatch.delenv("WW_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("WW_NTFY_TOPIC", raising=False)
    _week(db)
    run_digest(db, sources, now=WEEK_END + 60)
    row = db.execute("SELECT digest, analysis FROM digests").fetchone()
    assert row["digest"] and row["analysis"] is None
    assert "No analysis" in render_digest(db)


def test_markdown_escapes_what_the_llm_writes():
    h = markdown('# Head\n- a **b** <script>x</script>\n- [ok](https://a.b) [no](javascript:1)\n1. one')
    assert "<script>" not in h and "&lt;script&gt;" in h
    assert '<a href="https://a.b"' in h and 'href="javascript' not in h
    assert "<h2>Head</h2>" in h and "<ol>" in h and "<b>b</b>" in h
