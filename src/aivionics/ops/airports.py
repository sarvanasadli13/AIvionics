"""Airport, runway and frequency lookup — entirely offline (PLAN 4B.5).

The OurAirports CSVs ship with the application (`assets/data/`, public
domain, licence row in `assets/LICENSES.md`). Nothing here opens a socket,
which is the point: the airport page must render identifiers, runways,
elevation and local time with the cable out, and only the weather and
traffic panels are allowed to go dark.

Three decisions worth stating, because each has a cheaper wrong version:

* **The index is built once.** `airports.csv` is 12.7 MB / 85,836 rows. A
  linear re-parse per keystroke is not a slow search, it is an unusable one,
  so the file is parsed into an in-memory index on first use and the
  singleton is reused. The build belongs on a worker thread — call `warm()`.
* **Timezone is derived, not read.** OurAirports has **no** timezone column.
  The zone comes from `timezonefinder` (MIT, offline, bundled tables) via
  lat/lon, and what is stored is the **IANA zone name** — never a fixed
  offset. Local time is then resolved through `zoneinfo`, so the tz database
  handles DST instead of us being wrong twice a year.
* **Runways and frequencies load on demand.** Most sessions open one or two
  airports; parsing 5.3 MB of runway and frequency data to show none of it
  is work nobody asked for. First request pays, the rest are dictionary
  lookups.

The projection at the bottom lives here rather than in the map screen
because both the aircraft markers and the airport reference dots are drawn
on the same grid, and the maths must be testable without a Qt widget.
"""
from __future__ import annotations

import bisect
import csv
import sys
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import config

SOURCE = "OurAirports (bundled, public domain)"
TZ_SOURCE = "timezonefinder (offline, from lat/lon)"

DATA_DIR = config.ASSETS_DIR / "data"

# csv fields can exceed the default 128 kB limit on the keywords column.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

# Ranked so a search for "FRA" offers Frankfurt before a Kansas airstrip that
# happens to share three letters. Anything unlisted sorts last.
SIZE_RANK: dict[str, int] = {
    "large_airport": 0,
    "medium_airport": 1,
    "small_airport": 2,
    "seaplane_base": 3,
    "balloonport": 4,
    "heliport": 4,
    "closed": 5,
}

TYPE_LABEL: dict[str, str] = {
    "large_airport": "Large airport",
    "medium_airport": "Medium airport",
    "small_airport": "Small airport",
    "seaplane_base": "Seaplane base",
    "balloonport": "Balloonport",
    "heliport": "Heliport",
    "closed": "Closed",
}

# Which airports are drawn as reference points on the fleet map. There are no
# map tiles and no bundled coastline; the populated airports trace the
# coastlines well enough to orient a marker, and they cost nothing extra.
MAP_REFERENCE_TYPES = ("large_airport",)


@dataclass(frozen=True, slots=True)
class Airport:
    """One row of `airports.csv`, with the blanks kept as blanks."""

    ident: str
    type: str
    name: str
    latitude: float
    longitude: float
    elevation_ft: int | None
    continent: str
    iso_country: str
    iso_region: str
    municipality: str
    scheduled_service: bool
    icao: str
    iata: str
    local_code: str

    @property
    def size_rank(self) -> int:
        return SIZE_RANK.get(self.type, 6)

    @property
    def type_label(self) -> str:
        return TYPE_LABEL.get(self.type, self.type.replace("_", " ").title() or "Unknown")

    def codes(self) -> tuple[str, ...]:
        """Every identifier this airport answers to, deduplicated, in order."""
        seen: list[str] = []
        for code in (self.icao or self.ident, self.iata, self.local_code):
            if code and code not in seen:
                seen.append(code)
        return tuple(seen)

    def code_line(self) -> str:
        icao = self.icao or self.ident
        return f"{icao} / {self.iata}" if self.iata else icao

    def where(self) -> str:
        parts = [p for p in (self.municipality, country_name(self.iso_country)) if p]
        return ", ".join(parts)

    def elevation_text(self) -> str:
        if self.elevation_ft is None:
            return "elevation not recorded"
        return f"{self.elevation_ft:,} ft ({round(self.elevation_ft * 0.3048):,} m)"

    def position_text(self) -> str:
        ns = "N" if self.latitude >= 0 else "S"
        ew = "E" if self.longitude >= 0 else "W"
        return f"{abs(self.latitude):.4f}°{ns} {abs(self.longitude):.4f}°{ew}"


@dataclass(frozen=True, slots=True)
class Runway:
    airport_ident: str
    length_ft: int | None
    width_ft: int | None
    surface: str
    lighted: bool
    closed: bool
    le_ident: str
    he_ident: str
    le_heading: float | None
    he_heading: float | None

    @property
    def designation(self) -> str:
        ends = [e for e in (self.le_ident, self.he_ident) if e]
        return "/".join(ends) if ends else "(unnamed)"

    def dimension_text(self) -> str:
        if self.length_ft is None:
            return "dimensions not recorded"
        metres = round(self.length_ft * 0.3048)
        if self.width_ft is None:
            return f"{self.length_ft:,} ft ({metres:,} m)"
        return (f"{self.length_ft:,} × {self.width_ft} ft "
                f"({metres:,} × {round(self.width_ft * 0.3048)} m)")

    def surface_text(self) -> str:
        return self.surface or "surface not recorded"


@dataclass(frozen=True, slots=True)
class Frequency:
    airport_ident: str
    type: str
    description: str
    mhz: float | None

    def mhz_text(self) -> str:
        return f"{self.mhz:.3f}" if self.mhz is not None else "—"


def _text(value: str) -> str:
    return (value or "").strip()


def _upper(value: str) -> str:
    return (value or "").strip().upper()


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: str) -> int | None:
    parsed = _float(value)
    return None if parsed is None else int(round(parsed))


# A name search stops after this many matches. Airports are stored in
# importance order, so the cap discards the *worst* candidates for a broad
# query ("air" matches 54,435 rows) and never the answer anyone wanted.
SEARCH_SCAN_CAP = 3000


class AirportIndex:
    """The parsed CSVs plus the lookup structures the screens need.

    Two structures carry the search, and both exist because the obvious
    version was measured and was too slow to type against:

    * **Records are held in importance order** — large scheduled airports
      first, closed strips last. Every later structure inherits that order,
      so "best match" falls out of position instead of needing a sort over
      85,836 candidates.
    * **Names live in one joined string, not 85,836 of them.** Substring
      search is then a single C-level `str.find` loop over 2.9 MB (1–35 ms)
      rather than a Python loop of 85,836 calls (230 ms — per keystroke).
      `_offsets` maps a hit back to its record by bisection.
    """

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.dir = Path(data_dir or DATA_DIR)
        self._airports: list[Airport] = []
        self._by_code: dict[str, int] = {}
        self._codes: list[str] = []              # sorted, for prefix bisection
        self._blob = ""                          # "name\tcity" per record, \n joined
        self._offsets: list[int] = []
        self._runways: dict[str, list[Runway]] | None = None
        self._frequencies: dict[str, list[Frequency]] | None = None
        self._countries: dict[str, str] | None = None
        self._loaded = False
        self.load_error = ""

    # ── loading ───────────────────────────────────────────────────────
    def _rows(self, name: str) -> tuple[list[str], list[list[str]]]:
        """Read one bundled CSV as (header, rows). A missing file is a state.

        `csv.reader` and positional indexes rather than `DictReader`: the
        dictionary construction alone costs 5.9 s on `airports.csv`, which is
        most of a cold start spent building 85,836 dictionaries to throw away.
        """
        path = self.dir / name
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.reader(handle)
                header = next(reader, [])
                return header, [row for row in reader if row]
        except (OSError, csv.Error, UnicodeDecodeError, StopIteration) as exc:
            self.load_error = f"{name} could not be read — {exc}"
            return [], []

    @staticmethod
    def _columns(header: list[str], *names: str) -> list[int]:
        """Column positions by name, so a reordered CSV cannot silently shift."""
        lookup = {name.strip().lower(): i for i, name in enumerate(header)}
        return [lookup.get(name, -1) for name in names]

    def load(self) -> "AirportIndex":
        if self._loaded:
            return self
        self._loaded = True
        header, rows = self._rows("airports.csv")
        if not rows:
            return self
        (c_ident, c_type, c_name, c_lat, c_lon, c_elev, c_cont, c_country,
         c_region, c_city, c_sched, c_icao, c_iata, c_gps, c_local) = self._columns(
            header, "ident", "type", "name", "latitude_deg", "longitude_deg",
            "elevation_ft", "continent", "iso_country", "iso_region",
            "municipality", "scheduled_service", "icao_code", "iata_code",
            "gps_code", "local_code")
        if min(c_ident, c_name, c_lat, c_lon) < 0:
            self.load_error = "airports.csv is missing a required column"
            return self

        # 85,836 rows x 15 fields: the per-cell bounds check that reads
        # naturally here costs 0.6 s of a 1.4 s cold start, so short rows are
        # padded once and every field below is a plain index.
        width = len(header)
        blank = [""] * width
        parsed: list[tuple[int, bool, str, Airport, str]] = []
        for row in rows:
            if len(row) < width:
                row = row + blank[len(row):]
            lat, lon = _float(row[c_lat]), _float(row[c_lon])
            if lat is None or lon is None:
                # Without a position there is no map pin and no timezone, and
                # a row that can only half render is worse than one absent.
                continue
            ident = row[c_ident].strip().upper()
            kind = sys.intern(row[c_type].strip())
            name = row[c_name].strip()
            scheduled = row[c_sched].strip().lower() == "yes"
            parsed.append((
                SIZE_RANK.get(kind, 6), not scheduled, name,
                Airport(
                    ident=ident,
                    type=kind,
                    name=name,
                    latitude=lat,
                    longitude=lon,
                    elevation_ft=_int(row[c_elev]),
                    continent=sys.intern(row[c_cont].strip().upper()),
                    iso_country=sys.intern(row[c_country].strip().upper()),
                    iso_region=sys.intern(row[c_region].strip().upper()),
                    municipality=row[c_city].strip(),
                    scheduled_service=scheduled,
                    icao=row[c_icao].strip().upper() or ident,
                    iata=row[c_iata].strip().upper(),
                    local_code=row[c_local].strip().upper(),
                ),
                row[c_gps].strip().upper()))

        # Importance order, once. Everything downstream inherits it: the first
        # claimant of a shared code is then the bigger airport by construction,
        # and a capped name scan keeps the best matches rather than the first
        # ones the file happened to list.
        parsed.sort(key=lambda item: item[:3])
        self._airports = [item[3] for item in parsed]

        names: list[str] = []
        prefixable: set[str] = set()
        for position, item in enumerate(parsed):
            airport, gps = item[3], item[4]
            names.append(f"{airport.name}\t{airport.municipality}".lower())
            for code in (airport.ident, airport.icao, airport.iata, gps,
                         airport.local_code):
                if code:
                    self._by_code.setdefault(code, position)
            # Prefix search is offered on ICAO and IATA only. Local and GPS
            # codes are indexed for exact lookup but not for prefixes: they
            # are freely invented, and letting "FRA" prefix-match the local
            # code FRAGN put an Italian ultralight strip above Frankfurt.
            prefixable.update(c for c in (airport.icao, airport.iata) if c)
        self._codes = sorted(prefixable)

        self._blob = "\n".join(names)
        offset = 0
        for name in names:
            self._offsets.append(offset)
            offset += len(name) + 1
        return self

    @property
    def count(self) -> int:
        return len(self.load()._airports)

    def provenance(self) -> str:
        self.load()
        if self.load_error:
            return f"Source: {SOURCE} · UNAVAILABLE — {self.load_error}"
        return (f"Source: {SOURCE} · {self.count:,} airports read from disk · "
                f"timezone from {TZ_SOURCE} · no network involved")

    # ── lookup ────────────────────────────────────────────────────────
    def get(self, code: str) -> Airport | None:
        """Exact match on ICAO, IATA, GPS or local code. Case-insensitive."""
        self.load()
        position = self._by_code.get(_upper(code))
        return None if position is None else self._airports[position]

    def search(self, query: str, limit: int = 20) -> list[Airport]:
        """Rank airports for a partial identifier, name or city.

        Four match classes, best first: the exact identifier, an identifier
        prefix, a word starting with the query, then a bare substring. An
        engineer who types four letters means an ICAO code, so burying EDDF
        under "Eddie's Field" would be perverse — the class beats every other
        consideration, and airport size only orders within a class.
        """
        self.load()
        needle = _text(query)
        if len(needle) < 2:
            return []
        upper, lower = needle.upper(), needle.lower()
        classes: dict[int, int] = {}

        exact = self._by_code.get(upper)
        if exact is not None:
            classes[exact] = 0
        for code in self._code_prefix(upper):
            classes.setdefault(self._by_code[code], 1)

        start = bisect.bisect_right
        offsets, blob = self._offsets, self._blob
        found = blob.find(lower)
        scanned = 0
        while found >= 0 and scanned < SEARCH_SCAN_CAP:
            scanned += 1
            position = start(offsets, found) - 1
            if position not in classes:
                # A hit at a record boundary or after a separator starts a
                # word; anything else is a substring buried inside one.
                at_word = found == offsets[position] or blob[found - 1] in " -\t/,.'’("
                classes[position] = 2 if at_word else 3
            found = blob.find(lower, found + 1)

        ranked = sorted(classes.items(), key=lambda item: (item[1], item[0]))
        return [self._airports[position] for position, _ in ranked[:limit]]

    def _code_prefix(self, prefix: str) -> list[str]:
        """Identifiers starting with `prefix`, found by bisection on a sorted list."""
        if len(prefix) > 4:
            return []
        first = bisect.bisect_left(self._codes, prefix)
        codes = []
        for code in self._codes[first:first + 400]:
            if not code.startswith(prefix):
                break
            codes.append(code)
        return codes

    def runways(self, ident: str) -> tuple[Runway, ...]:
        if self._runways is None:
            header, rows = self._rows("runways.csv")
            (c_ident, c_len, c_width, c_surface, c_lit, c_closed, c_le, c_he,
             c_leh, c_heh) = self._columns(
                header, "airport_ident", "length_ft", "width_ft", "surface",
                "lighted", "closed", "le_ident", "he_ident", "le_heading_degt",
                "he_heading_degt")
            table: dict[str, list[Runway]] = {}
            for row in rows:
                def cell(column: int, row=row) -> str:
                    return row[column] if 0 <= column < len(row) else ""
                key = _upper(cell(c_ident))
                table.setdefault(key, []).append(Runway(
                    airport_ident=key,
                    length_ft=_int(cell(c_len)),
                    width_ft=_int(cell(c_width)),
                    surface=_text(cell(c_surface)),
                    lighted=_text(cell(c_lit)) == "1",
                    closed=_text(cell(c_closed)) == "1",
                    le_ident=_upper(cell(c_le)),
                    he_ident=_upper(cell(c_he)),
                    le_heading=_float(cell(c_leh)),
                    he_heading=_float(cell(c_heh)),
                ))
            self._runways = table
        found = self._runways.get(_upper(ident), [])
        return tuple(sorted(found, key=lambda r: (-(r.length_ft or 0), r.le_ident)))

    def frequencies(self, ident: str) -> tuple[Frequency, ...]:
        if self._frequencies is None:
            header, rows = self._rows("airport-frequencies.csv")
            c_ident, c_type, c_desc, c_mhz = self._columns(
                header, "airport_ident", "type", "description", "frequency_mhz")
            table: dict[str, list[Frequency]] = {}
            for row in rows:
                def cell(column: int, row=row) -> str:
                    return row[column] if 0 <= column < len(row) else ""
                key = _upper(cell(c_ident))
                table.setdefault(key, []).append(Frequency(
                    airport_ident=key,
                    type=_upper(cell(c_type)),
                    description=_text(cell(c_desc)),
                    mhz=_float(cell(c_mhz)),
                ))
            self._frequencies = table
        found = self._frequencies.get(_upper(ident), [])
        return tuple(sorted(found, key=lambda f: (f.type, f.mhz or 0.0)))

    def country_name(self, iso: str) -> str:
        if self._countries is None:
            header, rows = self._rows("countries.csv")
            c_code, c_name = self._columns(header, "code", "name")
            self._countries = {
                _upper(row[c_code]): _text(row[c_name])
                for row in rows
                if 0 <= c_code < len(row) and 0 <= c_name < len(row)}
        return self._countries.get(_upper(iso), _upper(iso))

    def map_reference_points(self) -> list[tuple[float, float]]:
        """Positions for the map's faint airport backdrop (see the module note)."""
        self.load()
        return [(a.latitude, a.longitude) for a in self._airports
                if a.type in MAP_REFERENCE_TYPES]


# ── the singleton ───────────────────────────────────────────────────────
# One index per process. Screens ask for it; only `warm()` pays the build,
# and `warm()` is what the worker thread calls.

_INDEX: AirportIndex | None = None


def index() -> AirportIndex:
    global _INDEX
    if _INDEX is None:
        _INDEX = AirportIndex()
    return _INDEX


def warm() -> AirportIndex:
    """Build the index. Call from a worker thread, never from a click handler."""
    return index().load()


def reset() -> None:
    """Drop the singleton. Tests point the index at a fixture directory."""
    global _INDEX
    _INDEX = None


def find(code: str) -> Airport | None:
    return index().get(code)


def search(query: str, limit: int = 20) -> list[Airport]:
    return index().search(query, limit)


def country_name(iso: str) -> str:
    return index().country_name(iso)


# ── timezone ────────────────────────────────────────────────────────────
# OurAirports carries no timezone column. The zone is derived from the
# position, and what travels through the application is the IANA *name* —
# an offset cached in the spring is wrong by an hour in the summer.

_FINDER = None


def _finder():
    global _FINDER
    if _FINDER is None:
        from timezonefinder import TimezoneFinder
        _FINDER = TimezoneFinder()
    return _FINDER


@lru_cache(maxsize=4096)
def timezone_at(latitude: float, longitude: float) -> str | None:
    """IANA zone name for a position, or None when it cannot be resolved.

    None is a real answer over open water and at a few disputed borders. The
    airport page says so rather than silently falling back to UTC, which
    would look like a working clock showing the wrong time.
    """
    try:
        return _finder().timezone_at(lat=float(latitude), lng=float(longitude))
    except Exception:                                            # noqa: BLE001
        # An unusable timezonefinder installation degrades this one field.
        return None


def timezone_for(airport: Airport) -> str | None:
    return timezone_at(airport.latitude, airport.longitude)


def local_time(airport: Airport, now: datetime | None = None) -> datetime | None:
    """`now` in the airport's own zone, DST resolved by the tz database."""
    zone = timezone_for(airport)
    if not zone:
        return None
    try:
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    if now is None:
        from datetime import timezone as _tz
        now = datetime.now(_tz.utc)
    return now.astimezone(tz)


def local_time_text(airport: Airport, now: datetime | None = None) -> str:
    """The one line the airport page shows. Names the zone, never an offset."""
    zone = timezone_for(airport)
    if not zone:
        return "local time unavailable — no IANA zone resolves at this position"
    stamp = local_time(airport, now)
    if stamp is None:
        return f"local time unavailable — zone {zone} is not in this tz database"
    return f"{stamp.strftime('%H:%M')} {zone} ({stamp.strftime('%a %d %b, UTC%z')})"


# ── projection ──────────────────────────────────────────────────────────
# Plate carrée: x is longitude, y is latitude, both linear. It distorts area
# badly towards the poles and is the right choice anyway — it needs no tiles,
# no projection library and no network, and the map's job is "which continent
# is this tail over", not navigation. Standing rule 12 pays for itself here.

MIN_LAT, MAX_LAT = -85.0, 85.0


def project(latitude: float, longitude: float, width: float, height: float,
            ) -> tuple[float, float]:
    """Lat/lon -> pixel inside a `width` x `height` rectangle, origin top-left.

    Latitude is clamped to ±85° so a bad position lands on the edge of the
    canvas instead of painting a marker outside it.
    """
    lat = max(MIN_LAT, min(MAX_LAT, float(latitude)))
    lon = ((float(longitude) + 180.0) % 360.0) - 180.0
    x = (lon + 180.0) / 360.0 * float(width)
    y = (MAX_LAT - lat) / (MAX_LAT - MIN_LAT) * float(height)
    return x, y


def unproject(x: float, y: float, width: float, height: float,
              ) -> tuple[float, float]:
    """The inverse of `project`, for hit-testing a click on the map."""
    lon = float(x) / float(width) * 360.0 - 180.0
    lat = MAX_LAT - float(y) / float(height) * (MAX_LAT - MIN_LAT)
    return lat, lon
