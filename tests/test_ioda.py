"""IODA (ADR 0002 §F): country-keyed outage events and alerts."""

import h3

from worldwatch.config.countries import countries, country_cell
from worldwatch.ingest import parsers
from worldwatch.poll.url import build_url

EVENTS = {"data": [
    {"location": "country/PY", "start": 1789497000, "duration": 939867, "datasource": "bgp",
     "score": 21339.4, "location_name": "Paraguay"},
    {"location": "country/PY", "start": 1789497000, "duration": 3600, "datasource": "ping-slash24",
     "score": 800.0, "location_name": "Paraguay"},
    {"location": "asn/12345", "start": 1789497000, "datasource": "bgp", "score": 5.0},
    {"location": "country/ZZ", "start": 1789497000, "datasource": "bgp", "score": 5.0},
]}
ALERTS = {"data": [
    {"datasource": "bgp", "entity": {"code": "NE", "name": "Niger", "type": "country"},
     "time": 1790418300, "level": "critical", "value": 191, "historyValue": 199},
    {"datasource": "bgp", "entity": {"code": "12345", "type": "asn"}, "time": 1790418300},
]}


def test_country_points_are_inside_and_cover_the_world():
    c = countries()
    assert len(c) > 230
    assert c["DE"][0] == "Germany" and 47 < c["DE"][1] < 55
    assert h3.get_resolution(country_cell("py", 2)) == 2
    assert country_cell("ZZ", 2) is None


def test_ioda_events_one_per_country_signal_with_dashboard_link(sources):
    obs = parsers.parse(EVENTS, sources["ioda_outage_events"])
    assert len(obs) == 2  # the ASN and the unknown country are skipped
    assert len({o.ts for o in obs}) == 2  # two signals starting together are both kept
    assert {o.cell for o in obs} == {country_cell("PY", 3)}
    ctx = obs[0].context
    assert ctx["country"] == "Paraguay" and ctx["url"].startswith("https://ioda.inetintel.cc.gatech.edu/country/PY")


def test_ioda_alerts_count_signals_below_history(sources):
    (o,) = parsers.parse(ALERTS, sources["ioda_alerts"])
    assert o.value is None and o.cell == country_cell("NE", 3)
    assert o.context["ratio"] == round(191 / 199, 3) and o.context["level"] == "critical"


def test_epoch_placeholders(sources):
    url = build_url(sources["ioda_alerts"], 1_790_000_000)
    assert "from=1789992800&until=1790000000" in url  # 2-h look-back


def test_ioda_events_policy_is_authoritative(sources):
    pol = sources["ioda_outage_events"].extra["alerts"]
    assert pol["every_event"] and pol["fresh_seconds"] == 3600
