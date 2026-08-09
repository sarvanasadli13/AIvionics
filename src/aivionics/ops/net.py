"""The single outbound *internet* client (PLAN 4B.6, standing rule 12).

Nothing in this application reaches a host on the internet except through
here. That is the whole point: IT can read `ALLOWED_HOSTS` in Admin and know
exactly what the machine talks to, and there is one place to audit for
timeouts, retries and caching rather than one per feature.

The qualifier is deliberate and was earned by getting it wrong: this file
used to claim to be the only socket in the application, and it is not.
`llm/client.py` opens its own connection to an Ollama endpoint — this
machine by default, a LAN server by setting, never the internet. It is a
different category, so it is not allow-listed here; it is named on the Admin
screen beside this table instead, and `tests/test_ops_page.py` fails if a
third network path appears anywhere in the tree.

Five properties, each of which exists because its absence is a known failure:

* **Allow-list.** A host not in `ALLOWED_HOSTS` is refused before a socket is
  opened. Subdomains of a listed host are allowed; look-alikes are not, so
  ``opensky-network.org.evil.test`` is rejected rather than matched by a
  careless ``in``.
* **The master switch.** Every entry point checks `online_enabled` first and
  returns a `Fetch` carrying a reason instead of calling out. Not a wrapper
  the caller may forget — the check is inside `NetClient.get`.
* **Disk cache with a TTL.** OpenSky's free tier is credit-limited; a screen
  that refreshes must not spend a credit per repaint. A stale entry is also
  kept and served when the network fails, flagged as such, because a position
  from four minutes ago labelled *four minutes ago* beats an empty map.
* **Bounded work.** Hard connect/read timeout, a small number of retries with
  exponential backoff, and a per-source minimum interval between calls.
* **Never on the UI thread.** `submit` puts the call on the global
  `QThreadPool`, the same pattern as `ui/searchservice.py`.

Every result carries **what it is, where it came from and when it was
fetched** — `Fetch.source`, `Fetch.url`, `Fetch.fetched_at` — so the screens
can state provenance rather than implying freshness.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .. import config

# ── the allow-list ──────────────────────────────────────────────────────
# Admin renders this table verbatim. Adding a row here is the only way to
# add an outbound host, and it is a code change, not a setting.


@dataclass(frozen=True)
class Host:
    """One allow-listed destination, described in the terms IT will ask about."""

    host: str
    purpose: str
    terms: str


HOST_REGISTRY: tuple[Host, ...] = (
    Host("aviationweather.gov",
         "METAR and TAF for the airport page",
         "NOAA / US National Weather Service — free, no key, no account"),
    Host("opensky-network.org",
         "Live ADS-B positions and airport arrivals/departures",
         "OpenSky Network free tier — anonymous, credit-limited, "
         "coverage incomplete by design"),
)

ALLOWED_HOSTS: tuple[str, ...] = tuple(h.host for h in HOST_REGISTRY)

# Bundled offline data never appears above because it never calls out.
OFFLINE_SOURCES = (
    "OurAirports CSVs (airports, runways, frequencies, navaids) — bundled, "
    "public domain, read from disk",
    "timezonefinder — IANA zone resolved from lat/lon on this machine",
)

USER_AGENT = "AIvionics/0.1 (maintenance decision support; offline desktop)"

DEFAULT_TIMEOUT = 8.0          # hard ceiling on any single call, seconds
DEFAULT_RETRIES = 2            # attempts after the first, on transient errors
DEFAULT_BACKOFF = 0.6          # seconds; doubled per retry
DEFAULT_MIN_INTERVAL = 5.0     # seconds between calls to the same source

OFFLINE_REASON = ("online features are switched off — enable them in Admin "
                  "to fetch this")


class HostNotAllowed(ValueError):
    """A URL outside `ALLOWED_HOSTS`. Never downgraded to a warning."""


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def is_allowed(url: str) -> bool:
    """True when `url` is https and its host is allow-listed.

    Matching is on whole labels — a listed host, or a subdomain of one. A
    substring test would accept `opensky-network.org.example.com`.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = host_of(url)
    return any(host == allowed or host.endswith("." + allowed)
               for allowed in ALLOWED_HOSTS)


def check_url(url: str) -> None:
    if not is_allowed(url):
        raise HostNotAllowed(
            f"{url!r} is not in the outbound allow-list "
            f"({', '.join(ALLOWED_HOSTS)}) or is not https")


# ── the result type ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class Fetch:
    """Data plus the three things a screen must be able to say about it.

    `ok` is false for every failure mode — switched off, refused host, timed
    out, bad status, unparseable body — and `error` says which, in words a
    user can act on. There is no exception path a caller can forget to
    handle.
    """

    source: str
    data: Any = None
    fetched_at: datetime | None = None
    from_cache: bool = False
    stale: bool = False
    url: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.data is not None

    @property
    def age(self) -> timedelta | None:
        if self.fetched_at is None:
            return None
        return datetime.now(timezone.utc) - self.fetched_at

    def provenance(self) -> str:
        """One line naming the source and the fetch time. Never a tooltip."""
        if not self.ok:
            return f"Source: {self.source} · unavailable — {self.error}"
        stamp = self.fetched_at.strftime("%Y-%m-%d %H:%MZ") if self.fetched_at else "—"
        how = "cache" if self.from_cache else "live"
        tail = " · SERVED STALE, the live fetch failed" if self.stale else ""
        return f"Source: {self.source} · fetched {stamp} ({how}){tail}"


# ── rate limiting ───────────────────────────────────────────────────────

class RateLimiter:
    """A minimum interval between calls to the same source.

    Deliberately not a token bucket: the limit that matters here is OpenSky's
    daily credit budget, and the way to protect it is to stop a screen from
    hammering one endpoint, not to model a burst allowance.
    """

    def __init__(self, min_interval: float = DEFAULT_MIN_INTERVAL,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.min_interval = float(min_interval)
        self.clock = clock
        self._last: dict[str, float] = {}

    def wait_for(self, source: str) -> float:
        """Seconds the caller must wait before `source` may be called again."""
        last = self._last.get(source)
        if last is None:
            return 0.0
        return max(0.0, self.min_interval - (self.clock() - last))

    def mark(self, source: str) -> None:
        self._last[source] = self.clock()


# ── disk cache ──────────────────────────────────────────────────────────

class DiskCache:
    """URL -> body, on disk, with the fetch time stored alongside.

    Keyed by a hash of the URL so query strings (station lists, bounding
    boxes) cannot produce an illegal filename. Any unreadable or malformed
    entry is treated as a miss and deleted — a corrupt cache must never be
    able to take the application down.
    """

    def __init__(self, directory: Path | str | None = None) -> None:
        self.dir = Path(directory or (config.DATA_DIR / "ops-cache"))

    def _path(self, url: str) -> Path:
        return self.dir / (hashlib.sha256(url.encode("utf-8")).hexdigest()[:32] + ".json")

    def get(self, url: str, ttl: float,
            now: datetime | None = None) -> tuple[str, datetime] | None:
        """Return (body, fetched_at) when a fresh entry exists, else None.

        `ttl <= 0` always misses. A caller asking for zero seconds of
        freshness means "fetch it now", and on Windows the system clock has
        ~15 ms of resolution — an entry written in the same tick would
        otherwise be served back as fresh.
        """
        if ttl <= 0:
            return None
        entry = self.peek(url)
        if entry is None:
            return None
        body, fetched_at = entry
        now = now or datetime.now(timezone.utc)
        if (now - fetched_at).total_seconds() >= ttl:
            return None
        return body, fetched_at

    def peek(self, url: str) -> tuple[str, datetime] | None:
        """Return the entry regardless of age — the fallback when a fetch fails."""
        path = self._path(url)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw["body"], datetime.fromisoformat(raw["fetched_at"])
        except (OSError, ValueError, KeyError):
            path.unlink(missing_ok=True)
            return None

    def put(self, url: str, body: str, source: str,
            now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._path(url).write_text(json.dumps({
                "url": url, "source": source, "body": body,
                "fetched_at": now.isoformat(),
            }), encoding="utf-8")
        except OSError:
            pass          # a cache that cannot be written is not an error

    def clear(self) -> None:
        for path in self.dir.glob("*.json"):
            path.unlink(missing_ok=True)


# ── transport ───────────────────────────────────────────────────────────

def _urllib_transport(url: str, timeout: float) -> tuple[int, str]:
    """The real transport. Injectable so tests never open a socket."""
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json, text/plain",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.status, response.read().decode(charset, errors="replace")


class TransientError(Exception):
    """A failure worth retrying — timeout, reset, 5xx, 429."""


# ── the client ──────────────────────────────────────────────────────────

@dataclass
class NetClient:
    """The only object in the application permitted to make a request.

    `online` is a callable rather than a flag so the switch is read at call
    time: toggling it in Admin takes effect on the next fetch without
    rebuilding anything that holds a client.
    """

    online: Callable[[], bool] = lambda: False
    cache: DiskCache = field(default_factory=DiskCache)
    limiter: RateLimiter = field(default_factory=RateLimiter)
    timeout: float = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES
    backoff: float = DEFAULT_BACKOFF
    transport: Callable[[str, float], tuple[int, str]] = _urllib_transport
    sleep: Callable[[float], None] = time.sleep
    log: "FetchLog" = field(default_factory=lambda: ACTIVITY)

    def get(self, url: str, source: str, ttl: float = 120.0) -> Fetch:
        """Fetch `url`, or say precisely why not. Never raises for I/O.

        Order matters and is not negotiable: the master switch is checked
        before the allow-list, and both before the cache, so a switched-off
        application does no work at all on behalf of an online feature.

        Only outcomes that involved the host reach `log`. A cache hit did not
        contact anyone, and counting it would turn Admin's "what did this
        machine talk to" into a screen that overstates the answer.
        """
        if not self.online():
            return Fetch(source=source, url=url, error=OFFLINE_REASON)
        try:
            check_url(url)
        except HostNotAllowed as exc:
            # A refused host is the single most important row Admin can show.
            refused = Fetch(source=source, url=url, error=str(exc))
            self.log.record(refused)
            return refused

        cached = self.cache.get(url, ttl)
        if cached is not None:
            body, fetched_at = cached
            return Fetch(source=source, data=body, fetched_at=fetched_at,
                         from_cache=True, url=url)

        waiting = self.limiter.wait_for(source)
        if waiting > 0:
            # Rate-limited with nothing fresh cached: serve whatever is on
            # disk rather than block a worker thread for the full interval.
            entry = self.cache.peek(url)
            if entry is not None:
                body, fetched_at = entry
                return Fetch(source=source, data=body, fetched_at=fetched_at,
                             from_cache=True, stale=True, url=url)
            return Fetch(source=source, url=url,
                         error=f"rate limited — {source} may be called again in "
                               f"{waiting:.0f} s")

        body, error = self._attempt(url, source)
        if body is not None:
            now = datetime.now(timezone.utc)
            self.cache.put(url, body, source, now)
            fetched = Fetch(source=source, data=body, fetched_at=now, url=url)
            self.log.record(fetched)
            return fetched

        self.log.record(Fetch(source=source, url=url, error=error))
        entry = self.cache.peek(url)
        if entry is not None:
            cached_body, fetched_at = entry
            return Fetch(source=source, data=cached_body, fetched_at=fetched_at,
                         from_cache=True, stale=True, url=url)
        return Fetch(source=source, url=url, error=error)

    def get_json(self, url: str, source: str, ttl: float = 120.0) -> Fetch:
        """`get` plus a parse. A body that is not JSON is a failed fetch."""
        fetched = self.get(url, source, ttl)
        if not fetched.ok:
            return fetched
        try:
            data = json.loads(fetched.data)
        except ValueError as exc:
            return Fetch(source=source, url=url,
                         error=f"response was not JSON — {exc}")
        return Fetch(source=fetched.source, data=data,
                     fetched_at=fetched.fetched_at, from_cache=fetched.from_cache,
                     stale=fetched.stale, url=url)

    def _attempt(self, url: str, source: str) -> tuple[str | None, str]:
        """One call plus its retries. Returns (body, error message)."""
        delay = self.backoff
        last = "unreachable"
        for attempt in range(self.retries + 1):
            self.limiter.mark(source)
            try:
                status, body = self.transport(url, self.timeout)
            except urllib.error.HTTPError as exc:
                if exc.code in (408, 429, 500, 502, 503, 504):
                    last = f"{source} returned HTTP {exc.code}"
                else:
                    return None, f"{source} returned HTTP {exc.code}"
            except (urllib.error.URLError, TransientError, TimeoutError, OSError) as exc:
                last = f"could not reach {source} — {getattr(exc, 'reason', exc)}"
            else:
                if 200 <= status < 300:
                    return body, ""
                if status in (408, 429, 500, 502, 503, 504):
                    last = f"{source} returned HTTP {status}"
                else:
                    return None, f"{source} returned HTTP {status}"
            if attempt < self.retries:
                self.sleep(delay)
                delay *= 2
        return None, last


def default_client(online: Callable[[], bool]) -> NetClient:
    return NetClient(online=online)


# ── what this machine actually talked to ────────────────────────────────
# `ALLOWED_HOSTS` says what *may* be contacted; Admin also has to answer
# "and did it?". The log is in memory and per session on purpose: it is an
# operator's view of the running application, not a second audit trail
# competing with the hash-chained one in `aivionics.audit`.

@dataclass
class SourceActivity:
    """One outbound source, as Admin renders it."""

    source: str
    host: str = ""
    attempts: int = 0
    failures: int = 0
    last_attempt: datetime | None = None
    last_success: datetime | None = None
    last_error: str = ""

    def line(self) -> str:
        if self.last_success is not None:
            stamp = self.last_success.strftime("%H:%M:%SZ")
            tail = f" · {self.failures} failed" if self.failures else ""
            return f"last succeeded {stamp} · {self.attempts} calls{tail}"
        if self.attempts:
            return (f"{self.attempts} attempt{'s' if self.attempts > 1 else ''}, "
                    f"none succeeded — {self.last_error or 'no reason recorded'}")
        return "not contacted this session"


class FetchLog:
    """Per-source counters and timestamps for the current session."""

    def __init__(self) -> None:
        self._rows: dict[str, SourceActivity] = {}

    def record(self, fetch: "Fetch") -> None:
        row = self._rows.setdefault(fetch.source, SourceActivity(fetch.source))
        row.host = host_of(fetch.url) or row.host
        row.attempts += 1
        row.last_attempt = datetime.now(timezone.utc)
        if fetch.ok:
            row.last_success = fetch.fetched_at or row.last_attempt
            row.last_error = ""
        else:
            row.failures += 1
            row.last_error = fetch.error
        self._rows[fetch.source] = row

    def rows(self) -> list[SourceActivity]:
        return sorted(self._rows.values(), key=lambda row: row.source)

    def get(self, source: str) -> SourceActivity | None:
        return self._rows.get(source)

    def clear(self) -> None:
        self._rows.clear()


ACTIVITY = FetchLog()


# ── off the UI thread ───────────────────────────────────────────────────
# Imported lazily: `aivionics.ops.net` must stay usable in a test process
# with no Qt application, and the compliance/importer paths never need Qt.

def submit(fn: Callable[[], Any], signals: Any) -> None:
    """Run `fn` on the global thread pool and emit its result.

    Every online call in this package goes through here. A blocking network
    call on the UI thread freezes the window, and standing rule 12 forbids it
    outright.
    """
    from PySide6.QtCore import QThreadPool
    _signals, job = _qt_types()
    QThreadPool.globalInstance().start(job(fn, signals))


_QT_TYPES = None


def _qt_types():
    """Build the Qt-dependent types on first use, not at import.

    Deliberately a function and not a module-level `__getattr__`: that hook
    is consulted for attribute access on the *module object*, never for a
    global name looked up inside a function, so `_Job(...)` in `submit`
    raised NameError instead of building the class.
    """
    global _QT_TYPES
    if _QT_TYPES is None:
        from PySide6.QtCore import QObject, QRunnable, Signal

        class FetchSignals(QObject):
            done = Signal(object)      # the Fetch, or whatever fn returned
            failed = Signal(str)       # message

        class Job(QRunnable):
            def __init__(self, fn, signals):
                super().__init__()
                self.fn, self.signals = fn, signals

            def run(self) -> None:                          # pragma: no cover
                try:
                    result = self.fn()
                except Exception as exc:                    # noqa: BLE001
                    self._emit(self.signals.failed,
                               f"{type(exc).__name__}: {exc}")
                else:
                    self._emit(self.signals.done, result)

            @staticmethod
            def _emit(signal, payload) -> None:             # pragma: no cover
                """Emit unless the receiver is gone.

                A window closed, or a page destroyed, while a fetch is still
                in flight leaves this runnable holding a deleted QObject.
                Nobody is waiting for the answer, so dropping it is the
                correct outcome — but Qt raises RuntimeError on the emit, and
                an unhandled exception on a pool thread prints a traceback
                that looks like a crash.
                """
                try:
                    signal.emit(payload)
                except RuntimeError:
                    pass

        _QT_TYPES = (FetchSignals, Job)
    return _QT_TYPES


def fetch_signals():
    """The `QObject` carrying `done(object)` / `failed(str)` for `submit`."""
    return _qt_types()[0]
