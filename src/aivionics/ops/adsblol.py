"""Live traffic from adsb.lol — a community ADS-B network (round 2 follow-up).

Chosen over OpenSky and over adsb.fi, and the reasons are worth keeping
because two of them are not obvious:

* **adsb.fi is licensed "personal, non-commercial use only".** It is slightly
  faster and carries three fields adsb.lol does not (`desc`, `ownOp`, `year`).
  None of that survives the licence: this application already carries one
  non-commercial dependency in the radar layer, and a second one in the
  primary traffic source would make the product unsellable by accident.
  adsb.lol publishes under **ODbL 1.0** — attribution and share-alike,
  commercial use permitted.
* **Coverage between the two is a coin toss.** Measured on the same six
  locations, four seconds apart: 157 aircraft against 164. They aggregate
  overlapping volunteer feeder networks, so this was never going to be the
  deciding number.
* **The record carries the registration.** OpenSky's anonymous state vectors
  do not, and that is the difference between a fleet map that needs an ICAO
  24-bit address typed in for every tail and one that simply matches on the
  tail number. `/v2/registration/A,B,C` takes a comma-separated list, so the
  whole fleet is one request.

**Coverage is not uniform and the map must not imply that it is.** Volunteer
receivers are dense over Europe and North America and absent elsewhere — in
testing, Baku returned zero aircraft on every network tried. An aircraft
missing from this map has not been *seen*; it is not necessarily on the
ground, and it is certainly not "not flying".
"""
from __future__ import annotations

import math
import sqlite3

from . import adsb
from .net import Fetch, NetClient

SOURCE = "adsb.lol (community ADS-B)"
BASE = "https://api.adsb.lol/v2"

# ODbL 1.0 requires attribution wherever the data is shown. This renders on
# the map itself, not in a menu (see `assets/LICENSES.md`).
ATTRIBUTION = "Live traffic © adsb.lol contributors · ODbL 1.0"

COVERAGE_WARNING = (
    "Positions come from volunteer ADS-B receivers. Coverage is dense over "
    "Europe and North America and thin or absent elsewhere — an aircraft "
    "missing here has not been seen by the network, which is not the same as "
    "being on the ground. Situational awareness only: nothing here is "
    "airworthiness or separation data.")

AREA_TTL = 20.0            # positions move; the cache is a burst guard
FLEET_TTL = 20.0
# Probed against the live API: dist=500 returns without complaint (348
# aircraft over Frankfurt). 250 was the cautious guess and it refused a
# country-sized view, which is exactly the view a fleet map opens on.
MAX_RADIUS_NM = 500.0
MAX_AIRCRAFT = 900

# Dozens of small responses off one host, not a metered API. The global
# five-second default would make panning unusable.
adsb_source_interval = 0.4
try:                                            # pragma: no cover - trivial
    from . import net as _net
    _net.SOURCE_MIN_INTERVAL[SOURCE] = adsb_source_interval
except Exception:                               # pragma: no cover
    pass

NM_PER_DEGREE = 60.0


def _f(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def parse_aircraft(row) -> adsb.StateVector | None:
    """One adsb.lol record as a `StateVector`.

    The units differ from OpenSky and the conversion runs the other way:
    adsb.lol reports **feet and knots**, `StateVector` stores metres and
    metres per second so that one model serves both feeds. Converting here
    rather than adding a second unit convention is deliberate — two aircraft
    models with different units in one map is how a 36,000 ft aircraft ends up
    drawn at 36,000 m.
    """
    if not isinstance(row, dict):
        return None
    icao24 = adsb.normalise_icao24(row.get("hex"))
    if not icao24:
        return None

    # `alt_baro` is the string "ground" for an aircraft on the surface.
    raw_baro = row.get("alt_baro")
    on_ground = isinstance(raw_baro, str) and raw_baro.strip().lower() == "ground"
    baro_ft = None if on_ground else _f(raw_baro)
    geom_ft = _f(row.get("alt_geom"))
    speed_kt = _f(row.get("gs"))
    climb_fpm = _f(row.get("baro_rate"))
    if climb_fpm is None:
        climb_fpm = _f(row.get("geom_rate"))

    seen = _f(row.get("seen_pos"))
    last_contact = None
    if seen is not None:
        from datetime import datetime, timedelta, timezone
        last_contact = datetime.now(timezone.utc) - timedelta(seconds=seen)

    return adsb.StateVector(
        icao24=icao24,
        callsign=str(row.get("flight") or "").strip(),
        origin_country="",                      # not published by this feed
        longitude=_f(row.get("lon")),
        latitude=_f(row.get("lat")),
        baro_altitude_m=None if baro_ft is None else baro_ft / adsb.METRES_TO_FEET,
        geo_altitude_m=None if geom_ft is None else geom_ft / adsb.METRES_TO_FEET,
        on_ground=on_ground,
        velocity_ms=None if speed_kt is None else speed_kt / adsb.MS_TO_KNOTS,
        true_track=_f(row.get("track")),
        vertical_rate_ms=None if climb_fpm is None else climb_fpm / adsb.MS_TO_FPM,
        squawk=str(row.get("squawk") or "").strip(),
        last_contact=last_contact,
        registration=str(row.get("r") or "").strip().upper(),
        aircraft_type=str(row.get("t") or "").strip().upper(),
        source=SOURCE,
    )


# ── queries ─────────────────────────────────────────────────────────────

def area_url(latitude: float, longitude: float, radius_nm: float) -> str:
    radius = max(1.0, min(MAX_RADIUS_NM, float(radius_nm)))
    return (f"{BASE}/lat/{float(latitude):.4f}/lon/{float(longitude):.4f}"
            f"/dist/{radius:.0f}")


def registration_url(tails) -> str:
    """One request for a list of tails. `/v2/registration/A,B,C` is honoured."""
    wanted = sorted({str(t).strip().upper() for t in tails} - {""})
    return f"{BASE}/registration/{','.join(wanted)}"


def bounds_to_circle(lat_min: float, lon_min: float,
                     lat_max: float, lon_max: float) -> tuple[float, float, float]:
    """The circle that covers a viewport rectangle.

    This feed is radial and the map is rectangular, so the box is enclosed
    rather than approximated: the radius is the half-diagonal, with longitude
    degrees shrunk by the cosine of the centre latitude. Under-sizing it would
    leave aircraft missing from the corners of the screen for no visible
    reason.
    """
    lat = (lat_min + lat_max) / 2.0
    lon = (lon_min + lon_max) / 2.0
    half_lat_nm = abs(lat_max - lat_min) / 2.0 * NM_PER_DEGREE
    half_lon_nm = (abs(lon_max - lon_min) / 2.0 * NM_PER_DEGREE
                   * max(0.05, math.cos(math.radians(lat))))
    radius = math.hypot(half_lat_nm, half_lon_nm)
    return lat, lon, min(MAX_RADIUS_NM, max(1.0, radius))


def area_too_large(lat_min: float, lon_min: float,
                   lat_max: float, lon_max: float) -> bool:
    _lat, _lon, radius = bounds_to_circle(lat_min, lon_min, lat_max, lon_max)
    return radius >= MAX_RADIUS_NM


def _aircraft(fetched: Fetch) -> list[adsb.StateVector]:
    payload = fetched.data if isinstance(fetched.data, dict) else {}
    states = []
    for row in payload.get("ac") or []:
        state = parse_aircraft(row)
        if state is not None and state.has_position:
            states.append(state)
        if len(states) >= MAX_AIRCRAFT:
            break
    return states


def area_traffic(client: NetClient, lat_min: float, lon_min: float,
                 lat_max: float, lon_max: float, *,
                 ttl: float = AREA_TTL) -> adsb.AreaTraffic:
    """Live traffic covering the visible rectangle."""
    bounds = (lat_min, lon_min, lat_max, lon_max)
    if area_too_large(*bounds):
        return adsb.AreaTraffic(
            fetch=Fetch(source=SOURCE,
                        error="zoom in to load live traffic — the visible "
                              "area is wider than one request may cover"),
            bounds=bounds)
    latitude, longitude, radius = bounds_to_circle(*bounds)
    fetched = client.get_json(area_url(latitude, longitude, radius), SOURCE,
                              ttl=ttl)
    if not fetched.ok:
        return adsb.AreaTraffic(fetch=fetched, bounds=bounds)
    return adsb.AreaTraffic(fetch=fetched, states=tuple(_aircraft(fetched)),
                            bounds=bounds)


def fleet_tails(con: sqlite3.Connection | None) -> list[str]:
    """Every tail on the register, whether or not it has an ICAO24 on file.

    This is the point of the move: matching happens on the tail number, so an
    aircraft is trackable the moment it is registered rather than after
    somebody looks up its transponder address.
    """
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT tail FROM aircraft WHERE tail IS NOT NULL "
            "AND TRIM(tail) <> '' ORDER BY tail").fetchall()
    except sqlite3.Error:
        return []
    return [str(tail).strip().upper() for (tail,) in rows if tail]


def fleet_positions(client: NetClient, con: sqlite3.Connection | None, *,
                    ttl: float = FLEET_TTL) -> adsb.FleetSnapshot:
    """Where the fleet is, matched by tail number, in one request."""
    tails = fleet_tails(con)
    if not tails:
        return adsb.FleetSnapshot(
            fetch=Fetch(source=SOURCE,
                        error="no aircraft on the fleet register — add "
                              "one in Admin and it becomes trackable by its "
                              "tail, with no transponder address needed"))

    fetched = client.get_json(registration_url(tails), SOURCE, ttl=ttl)
    if not fetched.ok:
        return adsb.FleetSnapshot(
            fetch=fetched,
            positions=tuple(adsb.FleetPosition(
                tail, "", reason=fetched.error or "not fetched")
                for tail in tails))

    by_tail = {state.registration: state for state in _aircraft(fetched)
               if state.registration}
    positions = []
    for tail in tails:
        state = by_tail.get(tail)
        positions.append(adsb.FleetPosition(
            tail=tail, icao24=state.icao24 if state else "", state=state,
            reason="" if state is not None
            else "not seen by the network in this fetch"))
    return adsb.FleetSnapshot(fetch=fetched, positions=tuple(positions),
                              untracked=())
