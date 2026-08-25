"""One photograph per airport, fetched rather than bundled (round 2, R5).

The owner's instruction was explicit: *"Do not need to manually download all
the pictures of airports. Software has online connection mostly. So it can
get picture from online."*

**How this sits with standing rule 11.** That rule governs what the
application *ships*: every bundled image needs a licence row, and it bans
operator logos and aircraft photographs precisely because of trademark and
attribution obligations. Nothing here is bundled. A photograph is fetched at
view time, cached like any other response, and **rendered with its author and
licence beside it** — which is what CC-BY and CC-BY-SA actually ask for. The
allow-list rows in `net.HOST_REGISTRY` say so, and `assets/LICENSES.md`
records the arrangement.

**Logos are still refused.** The obvious API for this (`prop=pageimages`)
returns the airport's *logo* for Frankfurt, and a logo is a trademark rather
than a licensed photograph. So the page's file list is read instead and
filtered down to things that are actually photographs.

**A missing photo is a normal outcome.** Most of the 48,000 airports in the
bundled data are airstrips with no article at all, and the panel says so
rather than showing a hole.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

from . import net as _net
from .net import Fetch, NetClient

SOURCE = "Wikipedia / Wikimedia Commons"
API = "https://en.wikipedia.org/w/api.php?"

SEARCH_TTL = 86400.0        # an airport's article does not move
IMAGE_TTL = 86400.0
PHOTO_TTL = 604800.0        # a week; the bytes never change
THUMB_WIDTH = 900

# Wikipedia asks for a courteous rate, not a five-second gap between calls.
_net.SOURCE_MIN_INTERVAL[SOURCE] = 0.4

PHOTO_SUFFIX = re.compile(r"\.jpe?g$", re.I)

# Names that are not photographs of the place. Logos are trademarks (rule 11);
# maps, diagrams and flags are not what "a picture of the airport" means.
NOT_A_PHOTO = re.compile(
    r"logo|wordmark|icon|flag|coat[\s_]of[\s_]arms|seal\b|map\b|diagram|chart|"
    r"layout|plan\b|locator|symbol|arrow|commons-|wiki|edit-|ambox|question",
    re.I)

_TAGS = re.compile(r"<[^>]+>")
_WORDS = re.compile(r"[a-z0-9]+")

# A search always returns *something*. These are the words that make a hit
# plausibly about an airfield at all; without one, the top hit is rejected
# rather than displayed as though it were the airport.
AIRFIELD_WORDS = ("airport", "airfield", "aerodrome", "air base", "airbase",
                  "airstrip", "aeroport", "flughafen", "airways", "heliport")


def _plain(value: object) -> str:
    """Strip the HTML Wikimedia puts in its credit fields."""
    return " ".join(_TAGS.sub(" ", str(value or "")).split())


@dataclass(frozen=True)
class Photo:
    """One picture, and everything that has to be shown with it."""

    title: str = ""
    url: str = ""
    page_url: str = ""
    author: str = ""
    licence: str = ""
    article: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.url)

    def credit(self) -> str:
        """The line that renders under the image. Never optional."""
        parts = [p for p in (self.author, self.licence) if p]
        return " · ".join(parts) if parts else "credit not published"


def _query(**params) -> str:
    params.setdefault("action", "query")
    params.setdefault("format", "json")
    params.setdefault("formatversion", "2")
    return API + urllib.parse.urlencode(params)


def article_title(client: NetClient, name: str, where: str = "") -> Fetch:
    """The article most likely to be about this airport.

    The name alone, deliberately. Adding the city looks helpful and is not:
    "Frankfurt Airport" finds the airport, while "Frankfurt Airport
    Frankfurt am Main, Germany" finds the *Rhine-Main region*, because the
    extra terms outweigh the ones that mattered. `where` is kept as a
    fallback for the handful of airports whose bare name is ambiguous.
    """
    found = client.get_json(
        _query(list="search", srsearch=name, srlimit="3"), SOURCE,
        ttl=SEARCH_TTL)
    if found.ok or not where:
        return found
    return client.get_json(
        _query(list="search", srsearch=f"{name} {where}", srlimit="3"),
        SOURCE, ttl=SEARCH_TTL)


def plausible_article(title: str, name: str) -> bool:
    """Is this hit about the airport, or just the best of a bad search?

    Two ways to qualify: the title reads like an airfield, or it shares a
    distinctive word with the airport's own name. Without this, searching an
    airstrip that has no article returns whatever Wikipedia thought was
    closest \u2014 in testing, a list of Beverly Hillbillies episodes \u2014
    and the panel would present it as that airport.
    """
    lower = title.lower()
    if any(word in lower for word in AIRFIELD_WORDS):
        return True
    wanted = {w for w in _WORDS.findall(name.lower()) if len(w) > 3}
    return bool(wanted & set(_WORDS.findall(lower)))


def score_photo(filename: str, article: str) -> int:
    """Prefer a picture of the place over a picture of something near it.

    Seattle-Tacoma's article carries a photograph of a light-rail train at a
    downtown station. Alphabetically it comes first; it is not the airport.
    Files sharing words with the article title win, and transport that is not
    the airport is pushed down.
    """
    lower = filename.lower()
    words = {w for w in _WORDS.findall(article.lower()) if len(w) > 3}
    score = sum(2 for w in words if w in lower)
    if any(w in lower for w in ("terminal", "apron", "tower", "runway",
                                "airport", "aerial", "concourse")):
        score += 3
    if any(w in lower for w in ("train", "station", "bus", "rail", "metro",
                                "tram", "protest", "portrait")):
        score -= 4
    return score


def find_photo(client: NetClient, name: str, where: str = "") -> Photo:
    """Search, pick a photograph, and read its credit. Never raises."""
    found = article_title(client, name, where)
    if not found.ok:
        return Photo()
    hits = (found.data or {}).get("query", {}).get("search", [])
    if not hits:
        return Photo()
    title = next((str(h.get("title") or "") for h in hits
                  if plausible_article(str(h.get("title") or ""), name)), "")
    if not title:
        return Photo()

    listing = client.get_json(
        _query(titles=title, prop="images", imlimit="60"), SOURCE,
        ttl=IMAGE_TTL)
    if not listing.ok:
        return Photo(article=title)
    pages = (listing.data or {}).get("query", {}).get("pages", [])
    files = [str(i.get("title") or "") for i in
             (pages[0].get("images", []) if pages else [])]
    candidates = [f for f in files
                  if PHOTO_SUFFIX.search(f) and not NOT_A_PHOTO.search(f)]
    if not candidates:
        return Photo(article=title)

    chosen = max(candidates, key=lambda f: score_photo(f, title))
    info = client.get_json(
        _query(titles=chosen, prop="imageinfo",
               iiprop="url|extmetadata", iiurlwidth=str(THUMB_WIDTH)),
        SOURCE, ttl=IMAGE_TTL)
    if not info.ok:
        return Photo(article=title)
    pages = (info.data or {}).get("query", {}).get("pages", [])
    entries = pages[0].get("imageinfo", []) if pages else []
    if not entries:
        return Photo(article=title)

    entry = entries[0]
    meta = entry.get("extmetadata", {}) or {}
    return Photo(
        title=chosen.removeprefix("File:"),
        url=str(entry.get("thumburl") or entry.get("url") or ""),
        page_url=str(entry.get("descriptionurl") or ""),
        author=_plain(meta.get("Artist", {}).get("value")),
        licence=_plain(meta.get("LicenseShortName", {}).get("value")),
        article=title)


def fetch_photo(client: NetClient, photo: Photo) -> bytes | None:
    """The image bytes, through the one client like everything else."""
    if not photo.ok:
        return None
    fetched = client.get_bytes(photo.url, SOURCE, ttl=PHOTO_TTL)
    data = fetched.data
    return bytes(data) if fetched.ok and isinstance(data, (bytes, bytearray)) \
        else None
