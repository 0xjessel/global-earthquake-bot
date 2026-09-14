"""Notability scoring for the global earthquake bot.

An event posts if its score is >= POST_THRESHOLD (60), or >= 80 when a
post already came from the same 300km region within the last 2 hours
(region suppression). No CA bias, no swarm logic, no rate cap
(owner decision 2026-09-13).

Score components:
  magnitude base:  M6.5+ = 100 (auto), 6.0-6.4 = 90, 5.5-5.9 = 70,
                   5.0-5.4 = 40, 4.5-4.9 = 20, 4.0-4.4 = 0 (pre-gate M4.0)
  depth modifier:  <10km +25, <25km +15, <50km +5, >70km -10, unknown 0
  city proximity:  <100km from a 1M+ city +25; 100-300km from a 1M+ city
                   or <100km from a 500k-1M city +10

Depth is NOT in the USGS feed; fetch_depth_km() does a per-event QuakeML
lookup (only called when the magnitude base is < 90, since deeper/unknown
depth can never pull a base-90 event below threshold).

City data: major_cities.json next to this file (GeoNames populated places,
population >= 500k). Static file, refreshed manually by Alfred, no cron.
"""

import json
import math
import os
import re

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CITIES_FILE = os.path.join(BASE_DIR, 'major_cities.json')

POST_THRESHOLD = 60
PRE_GATE_MAG = 4.0
SUPPRESS_RADIUS_KM = 300.0
SUPPRESS_WINDOW_S = 2 * 3600
SUPPRESS_BONUS = 20
REVISION_MIN_DELTA = 0.3
REVISION_MAX_AGE_S = 6 * 3600

DEPTH_QUERY = ('https://earthquake.usgs.gov/fdsnws/event/1/query'
               '?eventid={fid}&format=quakeml')
_USER_AGENT = {'User-Agent': 'Mozilla/5.0'}

_cities_cache = None


def _load_cities():
    global _cities_cache
    if _cities_cache is None:
        try:
            with open(CITIES_FILE) as f:
                cities = json.load(f)
            _cities_cache = (
                [c for c in cities if c['pop'] >= 1000000],
                [c for c in cities if c['pop'] < 1000000],
            )
        except (OSError, ValueError):
            _cities_cache = ([], [])
    return _cities_cache


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def magnitude_base(mag):
    if mag >= 6.5:
        return 100
    if mag >= 6.0:
        return 90
    if mag >= 5.5:
        return 70
    if mag >= 5.0:
        return 40
    if mag >= 4.5:
        return 20
    if mag >= PRE_GATE_MAG:
        return 0
    return None  # below pre-gate


def depth_mod(depth_km):
    if depth_km is None:
        return 0
    if depth_km < 10:
        return 25
    if depth_km < 25:
        return 15
    if depth_km < 50:
        return 5
    if depth_km > 70:
        return -10
    return 0


def city_mod(lat, lon):
    million, half = _load_cities()
    if not million and not half:
        return 0
    best_m = min((haversine_km(lat, lon, c['lat'], c['lon']) for c in million),
                 default=None)
    if best_m is not None and best_m < 100:
        return 25
    if best_m is not None and best_m <= 300:
        return 10
    best_h = min((haversine_km(lat, lon, c['lat'], c['lon']) for c in half),
                 default=None)
    if best_h is not None and best_h < 100:
        return 10
    return 0


def score_event(lat, lon, mag, depth_km=None):
    """Return (score, breakdown) or None if below the pre-gate."""
    base = magnitude_base(mag)
    if base is None:
        return None
    dmod = depth_mod(depth_km)
    cmod = city_mod(lat, lon)
    total = base + dmod + cmod
    return total, {'mag_base': base, 'depth': dmod, 'city': cmod,
                   'depth_km': depth_km}


def fetch_depth_km(fid, timeout=5):
    """Per-event USGS QuakeML lookup; depth is <origin><depth><value> in meters."""
    try:
        resp = requests.get(DEPTH_QUERY.format(fid=fid), headers=_USER_AGENT,
                            timeout=timeout)
        resp.raise_for_status()
        m = re.search(r'<origin[\s>].*?<depth>\s*<value>\s*([-\d.]+)\s*</value>',
                      resp.text, re.S)
        if not m:
            return None
        return float(m.group(1)) / 1000.0
    except Exception:
        return None


def suppression_required(lat, lon, now_ts, posted):
    """True if a post came from within SUPPRESS_RADIUS_KM in the window."""
    for rec in posted.values():
        if now_ts - rec['ts'] > SUPPRESS_WINDOW_S:
            continue
        if haversine_km(lat, lon, rec['lat'], rec['lon']) <= SUPPRESS_RADIUS_KM:
            return True
    return False


def effective_threshold(lat, lon, now_ts, posted):
    if suppression_required(lat, lon, now_ts, posted):
        return POST_THRESHOLD + SUPPRESS_BONUS
    return POST_THRESHOLD


def evaluate(feature, posted, now_ts, depth_km=None):
    """Score one USGS feature.

    Returns (would_post, score, breakdown, threshold, reason) or None
    when the event is below the pre-gate. depth_km=None skips the
    depth component (caller decides whether to fetch).
    """
    props = feature['properties']
    mag = props.get('mag')
    if mag is None:
        return None
    coords = feature['geometry']['coordinates']
    lon, lat = coords[0], coords[1]
    res = score_event(lat, lon, mag, depth_km)
    if res is None:
        return False, None, None, POST_THRESHOLD, 'below pre-gate M4.0'
    score, breakdown = res
    threshold = effective_threshold(lat, lon, now_ts, posted)
    if score >= threshold:
        reason = 'notable'
        if suppression_required(lat, lon, now_ts, posted):
            reason = 'notable (region-suppressed bar met)'
        would_post = True
    else:
        would_post = False
        if suppression_required(lat, lon, now_ts, posted):
            reason = f'score {score} < {threshold} (region recently posted)'
        else:
            reason = f'score {score} < {POST_THRESHOLD}'
    return would_post, score, breakdown, threshold, reason
