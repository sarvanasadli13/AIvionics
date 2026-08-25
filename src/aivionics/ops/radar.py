"""Precipitation radar imagery (BACKLOG round 2, R3).

The application had METAR and TAF — decoded text for one field at a time —
and no way to see weather as a shape. This adds a radar layer over the map.

**Where the pictures come from, and the licence problem, stated plainly.**
RainViewer publishes a global composite of national radar networks as web
map tiles. Its public API is free **for non-commercial use, with
attribution**, and that attribution renders on the map rather than living in
a file (`assets/LICENSES.md` carries the row). Standing rule 11 says an asset
whose licence cannot be established does not ship; this one *can* be
established, and it says non-commercial — which is fine for the machine this
was built on and is **a question that has to be answered before the product
is sold to anyone**. It is flagged here rather than discovered later.

**Coverage is not uniform and the map must not imply that it is.** The
composite is dense over North America and Europe, thin over much of Asia and
Africa, and absent over most oceans. Empty is not "no rain".

**Nothing here is aviation weather.** It is ground-based precipitation radar
for situational awareness. Convective weather avoidance is done with the
aircraft's own radar and the products a dispatcher is licensed to use, and
this is neither. The UI says so where it is shown, not only here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import net as _net
from .net import Fetch, NetClient

SOURCE = "RainViewer (radar composite)"
INDEX_URL = "https://api.rainviewer.com/public/weather-maps.json"

INDEX_TTL = 300.0          # the frame list moves every 10 minutes
TILE_TTL = 600.0           # a frame, once published, never changes
TILE_PX = 256

# Rendering options, in the order the tile path wants them:
#   colour scheme 4 (the "universal blue" ramp), smoothed, no snow overlay.
COLOUR_SCHEME = 4
SMOOTH = 1
SNOW = 0

ATTRIBUTION = "Radar composite © RainViewer · non-commercial use"

COVERAGE_WARNING = (
    "Ground-based precipitation radar, composited from national networks. "
    "Coverage is dense over North America and Europe and thin or absent "
    "elsewhere — an empty area is not a report of clear weather. This is "
    "situational awareness only: it is not an aviation weather product and "
    "nothing here supports a weather-avoidance decision.")

MAX_TILES = 48             # one screen's worth; a cap, not a target

# One radar view is dozens of small images off a CDN, not one metered API
# call. The global 5 s default would make the layer take two minutes to
# appear; this is the number that belongs with this source.
TILE_MIN_INTERVAL = 0.05


_net.SOURCE_MIN_INTERVAL[SOURCE] = TILE_MIN_INTERVAL


@dataclass(frozen=True)
class Frame:
    """One radar composite, at one instant."""

    path: str
    time: datetime

    def label(self) -> str:
        return self.time.strftime("%H:%MZ")


@dataclass(frozen=True)
class RadarIndex:
    """What frames exist right now, and where the tiles for them live."""

    fetch: Fetch
    host: str = ""
    past: tuple = ()
    nowcast: tuple = ()

    @property
    def ok(self) -> bool:
        return self.fetch.ok and bool(self.past or self.nowcast)

    @property
    def latest(self) -> Frame | None:
        frames = self.past or self.nowcast
        return frames[-1] if frames else None

    def tile_url(self, frame: Frame, z: int, x: int, y: int) -> str:
        return (f"{self.host}{frame.path}/{TILE_PX}/{z}/{x}/{y}/"
                f"{COLOUR_SCHEME}/{SMOOTH}_{SNOW}.png")


def _frames(rows) -> tuple:
    out = []
    for row in rows or ():
        try:
            out.append(Frame(path=str(row["path"]),
                             time=datetime.fromtimestamp(int(row["time"]),
                                                         tz=timezone.utc)))
        except (KeyError, TypeError, ValueError, OSError):
            continue
    return tuple(out)


def radar_index(client: NetClient, *, ttl: float = INDEX_TTL) -> RadarIndex:
    """The current frame list. Returns an index in every case, offline too."""
    fetched = client.get_json(INDEX_URL, SOURCE, ttl=ttl)
    if not fetched.ok:
        return RadarIndex(fetch=fetched)
    payload = fetched.data if isinstance(fetched.data, dict) else {}
    radar = payload.get("radar") or {}
    return RadarIndex(fetch=fetched,
                      host=str(payload.get("host") or ""),
                      past=_frames(radar.get("past")),
                      nowcast=_frames(radar.get("nowcast")))


# ── Web Mercator, because the tiles are and this map is not ─────────────
#
# The tiles are EPSG:3857 and this application draws EPSG:4326. That is not a
# detail to paper over: a Mercator tile covers a latitude band that grows
# towards the poles, so painting it into a plate-carrée rectangle stretches
# it wrongly. `tile_bounds` gives the true latitude range of every tile, and
# the renderer slices each one into bands and places each band at its own
# correct latitude. The error left is a fraction of a pixel.

MERCATOR_MAX_LAT = 85.05112878


def tile_xy(latitude: float, longitude: float, z: int) -> tuple[int, int]:
    lat = max(-MERCATOR_MAX_LAT, min(MERCATOR_MAX_LAT, float(latitude)))
    n = 2 ** z
    x = int((float(longitude) + 180.0) / 360.0 * n)
    radians = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(radians)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def tile_bounds(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    """(lat_north, lon_west, lat_south, lon_east) for one tile."""
    n = 2 ** z
    lon_west = x / n * 360.0 - 180.0
    lon_east = (x + 1) / n * 360.0 - 180.0
    lat_north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return lat_north, lon_west, lat_south, lon_east


def zoom_for(span_degrees: float, width_px: int) -> int:
    """The tile zoom whose pixels are closest to the screen's.

    Asking for a zoom finer than the view needs costs a tile fetch per step
    and buys nothing; asking for a coarser one is visibly soft.
    """
    span = max(0.05, float(span_degrees))
    ideal = math.log2(max(1.0, (360.0 / span) * (max(1, width_px) / TILE_PX)))
    return max(0, min(10, int(round(ideal))))


def tiles_for(lat_min: float, lon_min: float, lat_max: float, lon_max: float,
              z: int, limit: int = MAX_TILES) -> list[tuple[int, int, int]]:
    """Every (z, x, y) covering the box, capped."""
    x0, y0 = tile_xy(lat_max, lon_min, z)
    x1, y1 = tile_xy(lat_min, lon_max, z)
    tiles = []
    for x in range(min(x0, x1), max(x0, x1) + 1):
        for y in range(min(y0, y1), max(y0, y1) + 1):
            tiles.append((z, x, y))
            if len(tiles) >= limit:
                return tiles
    return tiles


@dataclass
class RadarTiles:
    """Fetched imagery for one frame, keyed by tile. Bytes, not decoded."""

    frame: Frame | None = None
    tiles: dict = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.tiles)


def fetch_tiles(client: NetClient, index: RadarIndex, frame: Frame,
                bounds: tuple, width_px: int, *,
                limit: int = MAX_TILES) -> RadarTiles:
    """Imagery covering `bounds`. Every tile goes through the one client."""
    if not index.ok or not index.host:
        return RadarTiles(error=index.fetch.error or "no radar frames published")
    lat_min, lon_min, lat_max, lon_max = bounds
    z = zoom_for(abs(lon_max - lon_min), width_px)
    out: dict = {}
    error = ""
    for tz, x, y in tiles_for(lat_min, lon_min, lat_max, lon_max, z, limit):
        fetched = client.get_bytes(index.tile_url(frame, tz, x, y), SOURCE,
                                   ttl=TILE_TTL)
        if fetched.ok and isinstance(fetched.data, (bytes, bytearray)):
            out[(tz, x, y)] = bytes(fetched.data)
        elif not error:
            error = fetched.error
    return RadarTiles(frame=frame, tiles=out,
                      error="" if out else (error or "no tiles returned"))
