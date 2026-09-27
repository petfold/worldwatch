"""Parsers for feeds with a shape of their own: official warnings (CAP), cyclone
warning texts (JTWC), tsunami bulletins, emergency squawks, Wikipedia views per
language, status-page components. Everything a stanza doesn't name is dropped
here (guardrail 8); aircraft identities never leave the parser.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from typing import Any

from worldwatch import evidence
from worldwatch.config.loader import SourceConfig
from worldwatch.ingest.generic import parse_time
from worldwatch.ingest.geocode import h3_cell
from worldwatch.ingest.models import Observation
from worldwatch.ingest.registry import register

CAP_NS = "{urn:oasis:names:tc:emergency:cap:1.2}"
CAP_SEVERITY = {"Unknown": 0, "Minor": 1, "Moderate": 2, "Severe": 3, "Extreme": 4}


def _offset(key: str, spread: int = 600) -> int:
    return int(hashlib.sha1(key.encode()).hexdigest()[:8], 16) % spread


def _area_points(area: ET.Element) -> list[tuple[float, float]]:
    """(lat, lon) centres of a CAP area's polygons and circles."""
    pts = []
    for poly in area.findall(f"{CAP_NS}polygon"):
        pairs = [p.split(",") for p in (poly.text or "").split()]
        coords = [(float(a), float(b)) for a, b, *_ in pairs if a and b]
        if coords:
            pts.append((sum(c[0] for c in coords) / len(coords), sum(c[1] for c in coords) / len(coords)))
    for circle in area.findall(f"{CAP_NS}circle"):
        head = (circle.text or "").split()[0] if (circle.text or "").split() else ""
        if "," in head:
            a, b = head.split(",")[:2]
            pts.append((float(a), float(b)))
    return pts


@register("cap_alerts")
def parse_cap_alerts(payload: Any, cfg: SourceConfig) -> list[Observation]:
    """CAP 1.2 alerts (fetch kind linked_get: the documents an RSS/Atom listing
    links to) → one pure-event observation per alert and H3 cell it covers, at
    the time it was sent, its value the CAP severity (Minor 1 … Extreme 4). Areas without coordinates fall
    back to the stanza's country. Cancellations and tests are dropped.

    [parse] severities = the CAP severities kept (default all); language =
    preferred <info> language prefix (default "en")."""
    from worldwatch.config.countries import country_cell

    res = int(cfg.geocode.get("h3_resolution", 3))
    keep = set(cfg.parse.get("severities") or CAP_SEVERITY)
    lang = str(cfg.parse.get("language", "en"))
    fallback = cfg.geocode.get("country")
    obs: list[Observation] = []
    for doc in payload or []:
        try:
            root = ET.fromstring(doc["text"].encode())
        except (ET.ParseError, KeyError):
            continue
        if root.tag != f"{CAP_NS}alert":
            inner = root.find(f".//{CAP_NS}alert")
            root = inner if inner is not None else root
        if (root.findtext(f"{CAP_NS}status") or "Actual") != "Actual" or \
                (root.findtext(f"{CAP_NS}msgType") or "Alert") == "Cancel":
            continue
        ident = root.findtext(f"{CAP_NS}identifier") or doc.get("url", "")
        infos = root.findall(f"{CAP_NS}info")
        info = next((i for i in infos if (i.findtext(f"{CAP_NS}language") or "").startswith(lang)),
                    infos[0] if infos else None)
        if info is None:
            continue
        sev = (info.findtext(f"{CAP_NS}severity") or "Unknown").strip()
        if sev not in keep:
            continue
        # when it was issued: what we observe (onset can be tomorrow)
        when = root.findtext(f"{CAP_NS}sent") or info.findtext(f"{CAP_NS}effective") or info.findtext(f"{CAP_NS}onset")
        ts = parse_time(when)
        if ts is None:
            continue
        cells = []
        for area in info.findall(f"{CAP_NS}area"):
            cells += [h3_cell(lat, lon, res) for lat, lon in _area_points(area)
                      if -90 <= lat <= 90 and -180 <= lon <= 180]
        if not cells and fallback:
            c = country_cell(str(fallback), res)
            cells = [c] if c else []
        areas = "; ".join(a.findtext(f"{CAP_NS}areaDesc") or "" for a in info.findall(f"{CAP_NS}area"))
        source = {
            "event": (info.findtext(f"{CAP_NS}event") or "").strip(),
            "headline": (info.findtext(f"{CAP_NS}headline") or "").strip(),
            "severity": sev, "urgency": info.findtext(f"{CAP_NS}urgency"),
            "certainty": info.findtext(f"{CAP_NS}certainty"),
            "sender": info.findtext(f"{CAP_NS}senderName") or root.findtext(f"{CAP_NS}sender"),
            "area": areas[:300], "url": info.findtext(f"{CAP_NS}web") or doc.get("url"),
        }
        for cell in dict.fromkeys(cells):
            obs.append(Observation(cfg.stream_id, cell, ts + _offset(f"{ident}|{cell}"), float(CAP_SEVERITY.get(sev, 0)),
                                   context=evidence.with_rank(cfg, evidence.pick(cfg, source))))
    return obs


_JTWC_POS = re.compile(r"(\d{6})Z\s+---\s+NEAR\s+(\d+(?:\.\d+)?)([NS])\s+(\d+(?:\.\d+)?)([EW])")
_JTWC_WIND = re.compile(r"MAX SUSTAINED WINDS\s+-\s+(\d+)\s+KT")
_JTWC_SUBJ = re.compile(r"SUBJ/(.+?)//")


@register("jtwc_warnings")
def parse_jtwc_warnings(payload: Any, cfg: SourceConfig) -> list[Observation]:
    """JTWC warning texts (fetch kind linked_get, the *web.txt links of the RSS)
    → one observation per warning: at the warning position, valued by its
    maximum sustained wind (kt), timed at the warning time (DDHHMMZ, in the
    month of the text's header or the current one)."""
    import time as _time

    res = int(cfg.geocode.get("h3_resolution", 3))
    obs: list[Observation] = []
    for doc in payload or []:
        text = doc.get("text", "")
        m = _JTWC_POS.search(text)
        if not m:
            continue
        stamp, lat, ns, lon, ew = m.groups()
        lat_f = float(lat) * (1 if ns == "N" else -1)
        lon_f = float(lon) * (1 if ew == "E" else -1)
        now = _time.gmtime()
        day, hour, minute = int(stamp[:2]), int(stamp[2:4]), int(stamp[4:6])
        year, month = now.tm_year, now.tm_mon
        if day > now.tm_mday + 1:  # a warning from late last month
            month, year = (12, year - 1) if month == 1 else (month - 1, year)
        ts = parse_time(f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:00+00:00")
        if ts is None:
            continue
        w = _JTWC_WIND.search(text)
        subj = _JTWC_SUBJ.search(text)
        name = subj.group(1).strip().title() if subj else "Tropical cyclone"
        source = {"name": name, "wind_kt": int(w.group(1)) if w else None,
                  "position": f"{abs(lat_f):.1f}{ns} {abs(lon_f):.1f}{ew}", "url": doc.get("url")}
        obs.append(Observation(cfg.stream_id, h3_cell(lat_f, lon_f, res), ts + _offset(name, 60),
                               float(w.group(1)) if w else None,
                               context=evidence.with_rank(cfg, evidence.pick(cfg, source))))
    return obs


TSUNAMI_LEVEL = {"information": 0, "threat": 1, "advisory": 1, "watch": 2, "warning": 3}


@register("tsunami_atom")
def parse_tsunami_atom(payload: Any, cfg: SourceConfig) -> list[Observation]:
    """tsunami.gov Atom (PTWC/NTWC) → one observation per bulletin entry, at its
    epicentre, valued by its category (Information 0, Threat/Advisory 1, Watch 2,
    Warning 3); [parse] min_level drops the lower ones."""
    atom, geo = "{http://www.w3.org/2005/Atom}", "{http://www.w3.org/2003/01/geo/wgs84_pos#}"
    res = int(cfg.geocode.get("h3_resolution", 3))
    min_level = int(cfg.parse.get("min_level", 0))
    try:
        root = ET.fromstring((payload or {}).get("text", "").encode())
    except ET.ParseError:
        return []
    obs: list[Observation] = []
    for e in root.findall(f"{atom}entry"):
        try:
            lat, lon = float(e.findtext(f"{geo}lat") or ""), float(e.findtext(f"{geo}long") or "")
        except ValueError:
            continue
        summary = ET.tostring(e.find(f"{atom}summary"), encoding="unicode", method="text") \
            if e.find(f"{atom}summary") is not None else ""
        cat = re.search(r"Category:\s*([A-Za-z]+)", summary)
        category = cat.group(1) if cat else "Information"
        level = TSUNAMI_LEVEL.get(category.lower(), 0)
        ts = parse_time(e.findtext(f"{atom}updated"))
        if ts is None or level < min_level:
            continue
        mag = re.search(r"Magnitude:\s*([\d.]+)", summary)
        link = next((lk.get("href") for lk in e.findall(f"{atom}link") if lk.get("title") == "Bulletin"), None)
        source = {"category": category, "region": (e.findtext(f"{atom}title") or "").strip().title(),
                  "magnitude": float(mag.group(1)) if mag else None, "url": link}
        obs.append(Observation(cfg.stream_id, h3_cell(lat, lon, res),
                               ts + _offset(e.findtext(f"{atom}id") or "", 60), float(level),
                               context=evidence.with_rank(cfg, evidence.pick(cfg, source))))
    return obs


SQUAWKS = {"7500": "hijack", "7600": "radio failure", "7700": "general emergency"}


@register("adsb_squawks")
def parse_adsb_squawks(payload: Any, cfg: SourceConfig) -> list[Observation]:
    """adsb.lol /v2/sqk/<code> feeds (multi_get) → one event per aircraft and
    emergency code per hour, at its position. The aircraft's identity only makes
    the key stable within the hour and never leaves the parser."""
    import json

    res = int(cfg.geocode.get("h3_resolution", 3))
    obs: list[Observation] = []
    for feed in payload or []:
        try:
            doc = json.loads(feed.get("text") or "{}")
        except ValueError:
            continue
        now = int(doc.get("now", 0)) // 1000
        hour = now - now % 3600
        for ac in doc.get("ac") or []:
            sq, lat, lon = str(ac.get("squawk", "")), ac.get("lat"), ac.get("lon")
            if sq not in SQUAWKS or lat is None or lon is None or not now:
                continue
            key = f"{ac.get('hex')}|{sq}"
            source = {"squawk": sq, "meaning": SQUAWKS[sq]}
            obs.append(Observation(cfg.stream_id, h3_cell(float(lat), float(lon), res), hour + _offset(key, 3600),
                                   float(sq), context=evidence.with_rank(cfg, evidence.pick(cfg, source))))
    return obs


@register("wiki_projectviews")
def parse_wiki_projectviews(payload: Any, cfg: SourceConfig) -> list[Observation]:
    """Wikimedia hourly projectviews (text_get; the hour is in the file name)
    → one observation per language of the stanza's `languages`: desktop plus
    mobile Wikipedia views, as a named cell "wikipedia:<lang>"."""
    import math

    payload = payload or {}
    m = re.search(r"projectviews-(\d{8})-(\d{2})0000", payload.get("url", ""))
    if not m:
        return []
    ts = parse_time(f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}T{m.group(2)}:00:00+00:00")
    langs = [str(x) for x in cfg.parse.get("languages", [])]
    views: dict[str, int] = {}
    for line in payload.get("text", "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2].isdigit():
            code = parts[0]
            lang = code[:-2] if code.endswith(".m") else code
            if lang in langs and (code == lang or code == lang + ".m"):
                views[lang] = views.get(lang, 0) + int(parts[2])
    log = str(cfg.parse.get("transform", "log1p")) == "log1p"
    return [Observation(cfg.stream_id, f"wikipedia:{lang}", ts, math.log1p(v) if log else float(v))
            for lang, v in sorted(views.items()) if ts is not None]


@register("statuspage_components")
def parse_statuspage_components(payload: Any, cfg: SourceConfig) -> list[Observation]:
    """Atlassian Statuspage components.json → one event per component not
    operational, at its country (component names "City, Country - (IATA)"),
    keyed by when it last changed: a count of degraded points of presence per
    country. [parse] name_pattern overrides how the country is read."""
    from worldwatch.config.countries import countries, country_cell

    res = int(cfg.geocode.get("h3_resolution", 3))
    by_name = {name.lower(): cc for cc, (name, _, _) in countries().items()}
    aliases = {str(k).lower(): str(v) for k, v in (cfg.parse.get("country_aliases") or {}).items()}
    pat = re.compile(str(cfg.parse.get("name_pattern", r"^(?P<city>[^,]+),\s*(?P<country>[^-(]+?)\s*-\s*\(")))
    obs: list[Observation] = []
    for comp in (payload or {}).get("components") or []:
        status = str(comp.get("status", "operational"))
        if status == "operational" or comp.get("group"):
            continue
        m = pat.match(str(comp.get("name", "")))
        if not m:
            continue
        country = m.group("country").strip().lower()
        cc = aliases.get(country) or by_name.get(country)
        cell = country_cell(cc, res) if cc else None
        ts = parse_time(comp.get("updated_at"))
        if cell is None or ts is None:
            continue
        source = {"component": comp.get("name"), "status": status.replace("_", " ")}
        obs.append(Observation(cfg.stream_id, cell, ts + _offset(str(comp.get("id")), 60), None,
                               context=evidence.with_rank(cfg, evidence.pick(cfg, source))))
    return obs

