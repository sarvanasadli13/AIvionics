"""METAR and TAF from aviationweather.gov (PLAN 4B.5, standing rule 12).

NOAA's Aviation Weather Center serves both free and without a key. Every
request goes through `net.NetClient`, so the master switch, the allow-list,
the cache and the timeouts are not this module's business.

The decoder exists to make a METAR readable at a glance, and it is built on
one rule that outranks every other design consideration here:

    **The raw string is fetched, stored and displayed. Always.**

`format=raw` is requested rather than `format=json` for the same reason. The
raw METAR is a stable ICAO-standard string; a JSON schema is somebody's
implementation detail and can be reshaped without notice. Decoding it here
means a wrong field is a bug in this file, visible against the raw text
sitting next to it on screen — not a silent disagreement with an upstream
parser. `Metar.decoded` is false when nothing could be read, and the screen
still shows the string.

Nothing here raises. A malformed group lands in `undecoded` and the rest of
the report still decodes; a completely unparseable line still yields a
`Metar` carrying its raw text.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .net import Fetch, NetClient

SOURCE = "aviationweather.gov (NOAA/NWS)"
BASE = "https://aviationweather.gov/api/data"

# METAR is issued hourly; a shorter TTL only spends requests on a report that
# has not changed. TAF is amended a few times a day.
METAR_TTL = 600.0        # 10 minutes
TAF_TTL = 1800.0         # 30 minutes

CLOUD_COVER: dict[str, str] = {
    "SKC": "sky clear", "CLR": "clear below 12,000 ft", "NSC": "no significant cloud",
    "NCD": "no cloud detected", "FEW": "few", "SCT": "scattered",
    "BKN": "broken", "OVC": "overcast", "VV": "vertical visibility",
}

# Layers at or above BKN are what a ceiling is made of.
CEILING_COVERS = ("BKN", "OVC", "VV")

WEATHER_INTENSITY = {"-": "light", "+": "heavy", "VC": "in the vicinity"}
WEATHER_DESCRIPTOR = {
    "MI": "shallow", "PR": "partial", "BC": "patches of", "DR": "low drifting",
    "BL": "blowing", "SH": "showers of", "TS": "thunderstorm", "FZ": "freezing",
}
WEATHER_PHENOMENON = {
    "DZ": "drizzle", "RA": "rain", "SN": "snow", "SG": "snow grains",
    "IC": "ice crystals", "PL": "ice pellets", "GR": "hail",
    "GS": "small hail", "UP": "unknown precipitation",
    "BR": "mist", "FG": "fog", "FU": "smoke", "VA": "volcanic ash",
    "DU": "widespread dust", "SA": "sand", "HZ": "haze", "PY": "spray",
    "PO": "dust whirls", "SQ": "squalls", "FC": "funnel cloud",
    "SS": "sandstorm", "DS": "duststorm",
}

# Flight category is a US (FAA) construct. It is labelled as such everywhere
# it is shown, because "VFR" here does not mean an EASA operator may depart.
FLIGHT_CATEGORIES = ("LIFR", "IFR", "MVFR", "VFR")
CATEGORY_BADGE = {"VFR": "ok", "MVFR": "info", "IFR": "warn", "LIFR": "alert"}
CATEGORY_NOTE = ("flight category is the US/FAA ceiling-and-visibility "
                 "convention, shown for orientation — it is not an operational "
                 "clearance and does not reflect EASA minima")

_STATION = re.compile(r"^[A-Z][A-Z0-9]{3}$")
_ISSUED = re.compile(r"^(\d{2})(\d{2})(\d{2})Z$")
_WIND = re.compile(r"^(\d{3}|VRB|///)(\d{2,3}|//)(?:G(\d{2,3}))?(KT|MPS|KMH)$")
_WIND_VAR = re.compile(r"^(\d{3})V(\d{3})$")
_VIS_M = re.compile(r"^(\d{4})(NDV)?$")
_VIS_SM = re.compile(r"^(M)?(\d+)(?:\s+(\d+)/(\d+))?SM$")
_VIS_SM_FRAC = re.compile(r"^(M)?(\d+)/(\d+)SM$")
_CLOUD = re.compile(r"^(FEW|SCT|BKN|OVC|VV)(\d{3}|///)(CB|TCU)?$")
_TEMPS = re.compile(r"^(M?\d{1,2})/(M?\d{1,2})?$")
_QNH = re.compile(r"^([QA])(\d{4})$")
_RVR = re.compile(r"^R(\d{2}[LCR]?)/([MP]?\d{4})(V[MP]?\d{4})?(?:FT)?([UDN])?$")
_WX = re.compile(
    r"^(-|\+|VC)?((?:MI|PR|BC|DR|BL|SH|TS|FZ)*)"
    r"((?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS)+)$")


@dataclass(frozen=True, slots=True)
class Wind:
    direction_deg: int | None = None
    variable: bool = False
    speed_kt: float | None = None
    gust_kt: float | None = None
    varies_from: int | None = None
    varies_to: int | None = None

    @property
    def calm(self) -> bool:
        return self.speed_kt == 0

    def text(self) -> str:
        if self.speed_kt is None:
            return "wind not reported"
        if self.calm:
            return "calm"
        heading = ("variable" if self.variable
                   else f"{self.direction_deg:03d}°" if self.direction_deg is not None
                   else "direction not reported")
        line = f"{heading} at {self.speed_kt:.0f} kt"
        if self.gust_kt:
            line += f", gusting {self.gust_kt:.0f} kt"
        if self.varies_from is not None and self.varies_to is not None:
            line += f" (varying {self.varies_from:03d}°–{self.varies_to:03d}°)"
        return line


@dataclass(frozen=True, slots=True)
class CloudLayer:
    cover: str
    base_ft: int | None
    convective: str = ""

    @property
    def is_ceiling(self) -> bool:
        return self.cover in CEILING_COVERS and self.base_ft is not None

    def text(self) -> str:
        name = CLOUD_COVER.get(self.cover, self.cover)
        if self.base_ft is None:
            return f"{name}, base not reported"
        extra = {"CB": " cumulonimbus", "TCU": " towering cumulus"}.get(
            self.convective, "")
        return f"{name} at {self.base_ft:,} ft{extra}"


@dataclass(frozen=True)
class Metar:
    """A decoded observation that can never lose its original text."""

    raw: str
    station: str = ""
    issued_at: datetime | None = None
    auto: bool = False
    corrected: bool = False
    wind: Wind = field(default_factory=Wind)
    cavok: bool = False
    visibility_m: float | None = None
    visibility_unit: str = "m"        # the unit the report used: "m" or "SM"
    weather: tuple[str, ...] = ()
    clouds: tuple[CloudLayer, ...] = ()
    temperature_c: float | None = None
    dewpoint_c: float | None = None
    qnh_hpa: float | None = None
    qnh_inhg: float | None = None
    undecoded: tuple[str, ...] = ()

    @property
    def decoded(self) -> bool:
        """False when the string yielded nothing — the raw text still shows."""
        return bool(self.station) and (
            self.wind.speed_kt is not None or self.temperature_c is not None
            or self.visibility_m is not None or self.cavok or bool(self.clouds))

    @property
    def ceiling_ft(self) -> int | None:
        """Lowest broken/overcast/vertical-visibility base, in feet."""
        bases = [layer.base_ft for layer in self.clouds if layer.is_ceiling]
        return min(bases) if bases else None

    @property
    def relative_humidity(self) -> float | None:
        """Magnus approximation over water. None unless both temps decoded."""
        if self.temperature_c is None or self.dewpoint_c is None:
            return None
        def pressure(t: float) -> float:
            return 6.112 * math.exp(17.62 * t / (243.12 + t))
        return max(0.0, min(100.0, 100.0 * pressure(self.dewpoint_c)
                            / pressure(self.temperature_c)))

    @property
    def flight_category(self) -> str:
        """US/FAA category from ceiling and visibility. Always labelled."""
        ceiling = self.ceiling_ft
        visibility_sm = (None if self.visibility_m is None
                         else self.visibility_m / 1609.344)
        if self.cavok:
            # CAVOK is defined as 10 km or more with no cloud below 5,000 ft
            # and no significant weather. Returning "" here would blank the
            # category on exactly the days it is least in doubt.
            return "VFR"
        if ceiling is None and visibility_sm is None:
            return ""
        if (ceiling is not None and ceiling < 500) or \
                (visibility_sm is not None and visibility_sm < 1):
            return "LIFR"
        if (ceiling is not None and ceiling < 1000) or \
                (visibility_sm is not None and visibility_sm < 3):
            return "IFR"
        if (ceiling is not None and ceiling <= 3000) or \
                (visibility_sm is not None and visibility_sm <= 5):
            return "MVFR"
        return "VFR"

    def visibility_text(self) -> str:
        """Reported in the unit the report used, converted alongside.

        `10SM` is 16 km, not "10 km or more" — that phrase belongs to the
        metric group 9999 and only to it. Rewriting a US report into the
        ICAO idiom loses a real difference between the two.
        """
        if self.cavok:
            return "CAVOK — 10 km or more, no significant cloud or weather"
        if self.visibility_m is None:
            return "visibility not reported"
        if self.visibility_unit == "SM":
            miles = self.visibility_m / 1609.344
            return f"{miles:g} SM ({self.visibility_m / 1000:.1f} km)"
        if self.visibility_m >= 9999:
            return "10 km or more"
        if self.visibility_m >= 1000:
            return f"{self.visibility_m / 1000:.1f} km"
        return f"{self.visibility_m:,.0f} m"

    def temperature_text(self) -> str:
        if self.temperature_c is None:
            return "temperature not reported"
        line = f"{self.temperature_c:.0f} °C"
        if self.dewpoint_c is not None:
            line += f" / dewpoint {self.dewpoint_c:.0f} °C"
        humidity = self.relative_humidity
        if humidity is not None:
            line += f" · RH {humidity:.0f}%"
        return line

    def qnh_text(self) -> str:
        if self.qnh_hpa is None and self.qnh_inhg is None:
            return "QNH not reported"
        if self.qnh_hpa is not None and self.qnh_inhg is not None:
            return f"{self.qnh_hpa:.0f} hPa / {self.qnh_inhg:.2f} inHg"
        if self.qnh_hpa is not None:
            return f"{self.qnh_hpa:.0f} hPa ({self.qnh_hpa * 0.02953:.2f} inHg)"
        return f"{self.qnh_inhg:.2f} inHg ({self.qnh_inhg / 0.02953:.0f} hPa)"

    def ceiling_text(self) -> str:
        ceiling = self.ceiling_ft
        if ceiling is None:
            return "no ceiling reported" if self.clouds or self.cavok \
                else "cloud not reported"
        return f"{ceiling:,} ft"

    def cloud_text(self) -> str:
        if self.cavok and not self.clouds:
            return "no cloud below 5,000 ft or below the highest minimum sector altitude"
        if not self.clouds:
            return "cloud not reported"
        return " · ".join(layer.text() for layer in self.clouds)

    def weather_text(self) -> str:
        return " · ".join(self.weather) if self.weather else "no significant weather"

    def issued_text(self) -> str:
        if self.issued_at is None:
            return "issue time not decoded"
        return self.issued_at.strftime("%d %H:%MZ")


def _number(token: str) -> float | None:
    """`M03` -> -3.0. METAR writes negatives with a leading M."""
    text = token.strip()
    if not text:
        return None
    negative = text.startswith("M")
    digits = text[1:] if negative else text
    if not digits.isdigit():
        return None
    return -float(digits) if negative else float(digits)


def _describe_weather(token: str) -> str | None:
    match = _WX.match(token)
    if not match:
        return None
    intensity, descriptors, phenomena = match.groups()
    words = []
    if intensity:
        words.append(WEATHER_INTENSITY[intensity])
    for i in range(0, len(descriptors), 2):
        words.append(WEATHER_DESCRIPTOR[descriptors[i:i + 2]])
    for i in range(0, len(phenomena), 2):
        words.append(WEATHER_PHENOMENON[phenomena[i:i + 2]])
    return " ".join(words)


def decode_metar(raw: str, now: datetime | None = None) -> Metar:
    """Decode one METAR. Never raises; the raw string always survives.

    Groups this parser does not recognise — runway state, trend forecasts,
    remarks, national extensions — go to `undecoded` rather than being
    dropped, so the screen can say the report contains more than is shown.
    """
    text = (raw or "").strip()
    if not text:
        return Metar(raw="")

    # Everything from RMK onward is a national remark section with its own
    # grammar. It is kept in the raw text and not pretended to be decoded.
    body, _, remarks = text.partition(" RMK ")
    tokens = body.replace("=", " ").split()
    if not tokens:
        return Metar(raw=text)

    station = ""
    issued_at: datetime | None = None
    auto = corrected = cavok = False
    direction: int | None = None
    variable = False
    speed = gust = None
    varies_from = varies_to = None
    visibility: float | None = None
    visibility_unit = "m"
    weather: list[str] = []
    clouds: list[CloudLayer] = []
    temperature = dewpoint = qnh_hpa = qnh_inhg = None
    undecoded: list[str] = []

    index = 0
    if tokens[0] in ("METAR", "SPECI"):
        index = 1
    if index < len(tokens) and _STATION.match(tokens[index]):
        station = tokens[index]
        index += 1

    while index < len(tokens):
        token = tokens[index]
        index += 1

        if station and (match := _ISSUED.match(token)) and issued_at is None:
            issued_at = _issue_time(match, now)
            continue
        if token in ("AUTO", "NIL"):
            auto = auto or token == "AUTO"
            continue
        if token in ("COR", "CCA", "CCB"):
            corrected = True
            continue
        if token in ("CAVOK",):
            cavok = True
            continue
        if token in ("NOSIG", "NSW"):
            continue
        if token in ("TEMPO", "BECMG"):
            # A trend group forecasts a change; decoding it as if it were the
            # current observation is exactly the kind of quiet error this
            # module refuses to make. Everything after it is left undecoded.
            undecoded.extend(tokens[index - 1:])
            break

        if match := _WIND.match(token):
            head, spd, gst, unit = match.groups()
            variable = head == "VRB"
            direction = int(head) if head.isdigit() else None
            speed = _to_knots(spd, unit)
            gust = _to_knots(gst, unit) if gst else None
            continue
        if match := _WIND_VAR.match(token):
            varies_from, varies_to = int(match.group(1)), int(match.group(2))
            continue
        if match := _VIS_M.match(token):
            visibility = float(match.group(1))
            continue
        if visibility is None and (parsed := _statute_miles(token, tokens, index)) is not None:
            visibility, index = parsed
            visibility_unit = "SM"
            continue
        if match := _CLOUD.match(token):
            cover, base, convective = match.groups()
            clouds.append(CloudLayer(
                cover=cover,
                base_ft=None if base == "///" else int(base) * 100,
                convective=convective or ""))
            continue
        if token in ("SKC", "CLR", "NSC", "NCD"):
            clouds.append(CloudLayer(cover=token, base_ft=None))
            continue
        if match := _TEMPS.match(token):
            temperature = _number(match.group(1))
            dewpoint = _number(match.group(2)) if match.group(2) else None
            continue
        if match := _QNH.match(token):
            kind, value = match.groups()
            if kind == "Q":
                qnh_hpa = float(value)
            else:
                qnh_inhg = float(value) / 100.0
            continue
        if _RVR.match(token):
            undecoded.append(token)       # runway visual range: shown as raw
            continue
        if (described := _describe_weather(token)) is not None:
            weather.append(described)
            continue
        undecoded.append(token)

    if remarks:
        undecoded.append("RMK " + remarks.strip())

    return Metar(
        raw=text, station=station, issued_at=issued_at, auto=auto,
        corrected=corrected,
        wind=Wind(direction_deg=direction, variable=variable, speed_kt=speed,
                  gust_kt=gust, varies_from=varies_from, varies_to=varies_to),
        cavok=cavok, visibility_m=visibility, visibility_unit=visibility_unit,
        weather=tuple(weather),
        clouds=tuple(clouds), temperature_c=temperature, dewpoint_c=dewpoint,
        qnh_hpa=qnh_hpa, qnh_inhg=qnh_inhg, undecoded=tuple(undecoded))


def _to_knots(value: str, unit: str) -> float | None:
    if not value or "/" in value:
        return None
    speed = float(value)
    if unit == "MPS":
        return speed * 1.943844
    if unit == "KMH":
        return speed * 0.539957
    return speed


def _statute_miles(token: str, tokens: list[str], index: int
                   ) -> tuple[float, int] | None:
    """US visibility: `10SM`, `1/2SM`, or `1 1/2SM` split across two tokens."""
    if match := _VIS_SM_FRAC.match(token):
        _, num, den = match.groups()
        return float(num) / float(den) * 1609.344, index
    if match := _VIS_SM.match(token):
        _, whole, num, den = match.groups()
        miles = float(whole) + (float(num) / float(den) if num else 0.0)
        return miles * 1609.344, index
    # `1` followed by `1/2SM`
    if token.isdigit() and index < len(tokens):
        if match := _VIS_SM_FRAC.match(tokens[index]):
            _, num, den = match.groups()
            miles = float(token) + float(num) / float(den)
            return miles * 1609.344, index + 1
    return None


def _issue_time(match: re.Match, now: datetime | None) -> datetime | None:
    """`081750Z` -> a UTC datetime, using `now` to supply month and year.

    A report issued on the 31st and read on the 1st belongs to last month,
    so the month rolls back rather than producing a date in the future.
    """
    day, hour, minute = (int(g) for g in match.groups())
    now = now or datetime.now(timezone.utc)
    year, month = now.year, now.month
    if day > now.day:
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    try:
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except ValueError:
        return None


# ── TAF ─────────────────────────────────────────────────────────────────
# A TAF is a forecast with its own change-group grammar, and half-decoding
# one is worse than not decoding it: a BECMG group read as current
# conditions is a wrong answer that looks right. The station and validity
# are lifted out for the header; the body is shown verbatim, wrapped at its
# change groups so it can be read.

_TAF_VALID = re.compile(r"^(\d{2})(\d{2})/(\d{2})(\d{2})$")
_TAF_BREAK = re.compile(r"\s(?=(?:BECMG|TEMPO|PROB\d{2}|FM\d{6}|INTER)\b)")


@dataclass(frozen=True)
class Taf:
    raw: str
    station: str = ""
    valid_from: str = ""
    valid_to: str = ""

    @property
    def decoded(self) -> bool:
        return bool(self.station)

    def validity_text(self) -> str:
        """Day/hour exactly as the TAF writes it.

        Not converted to a wall-clock instant: TAF hour 24 is legal and means
        the end of that day, and "09 24:00Z" is a time that does not exist.
        """
        if not self.valid_from:
            return "validity not decoded — read the raw forecast"
        return f"valid {self.valid_from} to {self.valid_to} (day/hour UTC)"

    def lines(self) -> tuple[str, ...]:
        """The forecast split at its change groups. Text, never interpretation."""
        return tuple(part.strip() for part in _TAF_BREAK.split(self.raw.strip())
                     if part.strip())


def decode_taf(raw: str) -> Taf:
    text = (raw or "").strip()
    tokens = text.split()
    station = valid_from = valid_to = ""
    for token in tokens[:4]:
        if not station and _STATION.match(token) and token != "TAF":
            station = token
        elif match := _TAF_VALID.match(token):
            d1, h1, d2, h2 = match.groups()
            valid_from, valid_to = f"{d1}/{h1}Z", f"{d2}/{h2}Z"
    return Taf(raw=text, station=station, valid_from=valid_from, valid_to=valid_to)


# ── fetching ────────────────────────────────────────────────────────────

def metar_url(icao: str) -> str:
    return f"{BASE}/metar?ids={icao.strip().upper()}&format=raw&taf=false"


def taf_url(icao: str) -> str:
    return f"{BASE}/taf?ids={icao.strip().upper()}&format=raw"


@dataclass(frozen=True)
class Report:
    """A fetch plus what it decoded to. `fetch.provenance()` names the source."""

    fetch: Fetch
    metar: Metar | None = None
    taf: Taf | None = None

    @property
    def ok(self) -> bool:
        return self.fetch.ok

    @property
    def error(self) -> str:
        return self.fetch.error

    def provenance(self) -> str:
        return self.fetch.provenance()


def fetch_metar(client: NetClient, icao: str, *, ttl: float = METAR_TTL,
                now: datetime | None = None) -> Report:
    """Current observation for one station. Returns a Report either way.

    An empty body is a real answer from this API: it means the station files
    no METAR. That is reported as such rather than as a network failure.
    """
    url = metar_url(icao)
    fetched = client.get(url, SOURCE, ttl=ttl)
    if not fetched.ok:
        return Report(fetch=fetched)
    line = _first_line(fetched.data)
    if not line:
        return Report(fetch=Fetch(
            source=fetched.source, url=url, fetched_at=fetched.fetched_at,
            error=f"{icao.upper()} files no METAR — the station reported nothing"))
    return Report(fetch=fetched, metar=decode_metar(line, now))


def fetch_taf(client: NetClient, icao: str, *, ttl: float = TAF_TTL) -> Report:
    url = taf_url(icao)
    fetched = client.get(url, SOURCE, ttl=ttl)
    if not fetched.ok:
        return Report(fetch=fetched)
    body = " ".join(str(fetched.data).split())
    if not body:
        return Report(fetch=Fetch(
            source=fetched.source, url=url, fetched_at=fetched.fetched_at,
            error=f"{icao.upper()} files no TAF — not every airport does"))
    return Report(fetch=fetched, taf=decode_taf(body))


def _first_line(body: object) -> str:
    for line in str(body or "").splitlines():
        if line.strip():
            return line.strip()
    return ""
