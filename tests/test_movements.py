"""Phase 8: airport movements, and who is entitled to claim one.

**No test in this file opens a socket.** Every fetching path is driven
through an injected transport, exactly as `tests/test_ops_page.py` does, and
the two providers that do not fetch at all — the operational level and the
observed level — are exercised without a client.

The thing under test is not really "does the arrivals list populate". It is
whether the screen can still be read correctly when it does not. Four
measured facts sit behind these assertions and none of them is negotiable:

* OpenSky's own documentation says the flights tables are *updated by a batch
  process at night*. EDDF — well over a thousand movements a day — returned
  **one arrival** for a twelve-hour window, and then HTTP 429.
* adsb.lol has no schedule endpoint at all. It is a position aggregator, and
  its data is ODbL 1.0: attribution is required wherever it is shown.
* Baku returned **zero aircraft** on every network tried. An empty area is
  not a report that the sky is empty.
* There is no FIDS or AODB to integrate with here, and there is no test
  instance of one. The operational level is therefore an interface plus an
  explicit unavailable state, and a test that passed against a fake adapter
  would be testing the fake.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from aivionics.ops import adsb, adsblol, movements as mv, net

UTC = timezone.utc
NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


# ── the harness, mirroring tests/test_ops_page.py ───────────────────────

class Recorder:
    """A transport that never opens a socket and remembers what it was asked."""

    def __init__(self, body: str = "OK", status: int = 200) -> None:
        self.body, self.status, self.calls = body, status, []

    def __call__(self, url: str, timeout: float) -> tuple[int, str]:
        self.calls.append(url)
        return self.status, self.body


class Flaky:
    """Answers once, then fails — so the disk cache has to serve the rest.

    This is the only way to reach a genuinely stale `Fetch` without reaching
    into `net` internals, and the stale board is the state worth testing
    hardest: it is the one where rows *are* on screen and every time on them
    has stopped moving.
    """

    def __init__(self, body: str) -> None:
        self.body, self.calls = body, []

    def __call__(self, url: str, timeout: float) -> tuple[int, str]:
        self.calls.append(url)
        return (200, self.body) if len(self.calls) == 1 else (503, "")


def client(tmp_path, online=True, transport=None, **kw) -> net.NetClient:
    return net.NetClient(
        online=lambda: online,
        transport=transport or Recorder(),
        cache=net.DiskCache(tmp_path / "cache"),
        limiter=net.RateLimiter(min_interval=0.0),
        log=net.FetchLog(), retries=0, **kw)


def state(icao24="3c6444", *, lat=50.033, lon=8.570, ground=False,
          altitude_m=None, at=NOW, callsign="DLH491", registration="D-AIZA",
          kind="A320", source=adsblol.SOURCE) -> adsb.StateVector:
    return adsb.StateVector(
        icao24=icao24, callsign=callsign, latitude=lat, longitude=lon,
        baro_altitude_m=altitude_m, on_ground=ground, last_contact=at,
        registration=registration, aircraft_type=kind, source=source)


def flights_body(**over) -> str:
    row = {"icao24": "3c6444", "callsign": "DLH491",
           "estDepartureAirport": "KSEA", "estArrivalAirport": "EDDF",
           "firstSeen": 1785990000, "lastSeen": 1786000000,
           "departureAirportCandidatesCount": 1,
           "arrivalAirportCandidatesCount": 1}
    row.update(over)
    return json.dumps([row])


def at_eddf(latitude: float, longitude: float) -> str:
    """A locator stub, so no test builds the 12.7 MB airport index."""
    return "EDDF"


# ── observed: inferred, and labelled inferred everywhere it is rendered ─

def observed_landing() -> tuple[mv.ObservedProvider, mv.Movement]:
    provider = mv.ObservedProvider(source=adsblol.SOURCE, locate=at_eddf)
    provider.observe([state(altitude_m=200.0)], now=NOW)
    found = provider.observe([state(ground=True, at=NOW + timedelta(minutes=1))],
                             now=NOW + timedelta(minutes=1))
    assert len(found) == 1
    return provider, found[0]


def test_an_observed_landing_is_labelled_observed_and_inferred():
    _provider, movement = observed_landing()
    assert movement.level == mv.LEVEL_OBSERVED
    assert movement.kind == mv.KIND_OBSERVED
    assert movement.confidence == mv.CONFIDENCE_INFERRED
    assert movement.status == mv.STATUS_OBSERVED_LANDING
    assert movement.source == adsblol.SOURCE
    # Every line this movement can put on a screen names what it is.
    assert "ads-b observed" in movement.provenance()
    assert "inferred" in movement.provenance()


def test_an_observed_movement_carries_a_seen_time_and_never_a_schedule():
    """The single most dangerous field to invent, so it is asserted absent.

    A scheduled time beside the word "landed" reads as an on-time report.
    Nothing observed from a transponder measured that, so there is no
    schedule to compare against and `delay` says so rather than returning
    zero.
    """
    _provider, movement = observed_landing()
    assert movement.scheduled_at is None
    assert movement.estimated_at is None
    assert movement.actual_at is not None
    assert movement.time_basis() == "actual"
    assert "actual" in movement.time_text()
    assert movement.delay() is None
    assert "no scheduled time" in movement.delay_text()


def test_an_observed_movement_never_claims_a_gate():
    _provider, movement = observed_landing()
    assert movement.gate == "" and movement.terminal == ""
    assert "does not carry one" in movement.place_text()


def test_the_observed_note_says_what_a_landing_here_actually_means():
    provider, _movement = observed_landing()
    board = provider.arrivals("EDDF")
    assert board.ok is True
    assert mv.OBSERVED_NOTE in board.notes
    assert "stopped reporting airborne" in mv.OBSERVED_NOTE
    assert "neither is a confirmed movement" in mv.OBSERVED_NOTE
    assert "live" not in board.headline().lower()


def test_an_observed_takeoff_leaves_the_far_end_unknown():
    """It saw a departure. It did not see where the aircraft is going."""
    provider = mv.ObservedProvider(source=adsblol.SOURCE, locate=at_eddf)
    provider.observe([state(ground=True)], now=NOW)
    found = provider.observe([state(altitude_m=300.0, at=NOW + timedelta(minutes=2))],
                             now=NOW + timedelta(minutes=2))
    assert len(found) == 1
    movement = found[0]
    assert movement.arriving is False
    assert movement.status == mv.STATUS_OBSERVED_TAKEOFF
    assert movement.origin == "EDDF"
    assert movement.other_end == "unknown"


def test_a_ground_bit_that_flickers_at_altitude_is_not_a_landing():
    """ADS-B ground bits do flicker, and a cruise dropout is not an arrival."""
    provider = mv.ObservedProvider(source=adsblol.SOURCE, locate=at_eddf)
    provider.observe([state(altitude_m=11000.0)], now=NOW)
    found = provider.observe(
        [state(ground=True, altitude_m=11000.0, at=NOW + timedelta(seconds=30))],
        now=NOW + timedelta(seconds=30))
    assert found == ()
    assert provider.arrivals("EDDF").ok is False


def test_a_state_with_no_position_is_not_folded_in():
    provider = mv.ObservedProvider(source=adsblol.SOURCE, locate=at_eddf)
    assert provider.observe([adsb.StateVector(icao24="3c6444")], now=NOW) == ()


# ── recorded: history, and never dressed up as "now" ────────────────────

def test_opensky_movements_are_labelled_historical_not_live(tmp_path):
    provider = mv.RecordedProvider(client(tmp_path,
                                          transport=Recorder(body=flights_body())))
    board = provider.arrivals("EDDF")
    assert board.ok is True
    assert board.level == mv.LEVEL_RECORDED
    assert board.movements[0].kind == mv.KIND_RECORDED
    assert board.movements[0].confidence == mv.CONFIDENCE_REPORTED
    # The word that must never appear on this board, and the warning that
    # must always accompany it.
    assert "live" not in board.headline().lower()
    assert "nightly batch" in board.headline()
    assert adsb.MOVEMENTS_WARNING in board.notes
    assert "nightly batch" in adsb.MOVEMENTS_WARNING.lower()


def test_a_recorded_row_becomes_an_actual_time_never_a_scheduled_one():
    """OpenSky has no schedule. Inventing one from a track is the whole error."""
    flight = adsb.Flight(icao24="3c6444", callsign="DLH491",
                         departure="KSEA", arrival="EDDF",
                         first_seen=NOW - timedelta(hours=10), last_seen=NOW)
    movement = mv.from_recorded_flight(flight, "EDDF", arriving=True)
    assert movement.scheduled_at is None
    assert movement.actual_at == NOW
    assert movement.time_basis() == "actual"
    assert movement.delay() is None
    assert "inferred from where the track" in movement.note


def test_a_recorded_row_with_competing_candidates_says_so():
    flight = adsb.Flight(icao24="3c6444", departure="KSEA", arrival="EDDF",
                         last_seen=NOW, arrival_candidates=4)
    movement = mv.from_recorded_flight(flight, "EDDF", arriving=True)
    assert "several candidates" in movement.note


# ── the states an empty board can be in, told apart ─────────────────────

def test_a_level_nobody_has_connected_is_unavailable_not_empty(tmp_path):
    """There is no FIDS or AODB here, and no adapter pretending to be one."""
    board = mv.operational_provider().arrivals("EDDF")
    classified = mv.board_state(board)
    assert classified.state == mv.BOARD_UNAVAILABLE
    assert classified.blank_is_explained is True
    assert "not connected" in classified.line()
    assert "FIDS" in classified.line() and "AODB" in classified.line()
    assert board.movements == ()


def test_an_unconfigured_commercial_provider_says_what_it_would_need(tmp_path):
    provider = mv.commercial_provider(client(tmp_path))
    availability = provider.availability()
    assert availability.available is False
    assert mv.board_state(provider.departures("EDDF")).state == mv.BOARD_UNAVAILABLE
    assert "subscription" in availability.reason
    assert mv.CommercialConfig().configured is False


def test_an_empty_observed_board_says_nothing_was_seen_not_nothing_happened():
    """Baku returned zero aircraft on every network tried (measured)."""
    board = mv.ObservedProvider(locate=at_eddf).arrivals("UBBB")
    classified = mv.board_state(board)
    assert classified.state == mv.BOARD_NO_COVERAGE
    assert "nothing was seen, not that nothing happened" in classified.line()


def test_an_empty_recorded_window_is_no_coverage_not_a_failure(tmp_path):
    """OpenSky answering with `[]` is an answer. A 429 is not, and they are
    the same shape by the time they reach a panel."""
    provider = mv.RecordedProvider(client(tmp_path, transport=Recorder(body="[]")))
    classified = mv.board_state(provider.arrivals("EDDF"))
    assert classified.state == mv.BOARD_NO_COVERAGE
    assert "coverage is incomplete" in classified.line()


def test_a_source_that_could_not_be_reached_is_an_error_not_no_coverage(tmp_path):
    provider = mv.RecordedProvider(
        client(tmp_path, transport=Recorder(body="", status=429)))
    classified = mv.board_state(provider.arrivals("EDDF"))
    assert classified.state == mv.BOARD_ERROR
    assert "OpenSky" in classified.headline


def test_the_switch_being_off_is_not_reported_as_an_empty_airport(tmp_path):
    transport = Recorder(body=flights_body())
    provider = mv.RecordedProvider(client(tmp_path, online=False,
                                          transport=transport))
    classified = mv.board_state(provider.arrivals("EDDF"))
    assert classified.state == mv.BOARD_ERROR
    assert transport.calls == []


def test_a_cached_list_after_a_failed_fetch_is_marked_stale(tmp_path):
    """Rows on screen, every time on them frozen. The state that needs saying."""
    transport = Flaky(flights_body())
    api = client(tmp_path, transport=transport)
    fresh = mv.RecordedProvider(api).arrivals("EDDF")
    assert mv.board_state(fresh).state == mv.BOARD_OK

    # Same URL, TTL expired by asking for none: the fetch is retried, fails,
    # and the disk cache answers instead.
    stale = mv.RecordedProvider(api)._board(
        adsb.fetch_recorded_movements(api, "EDDF", arriving=True, ttl=0.0), True)
    classified = mv.board_state(stale)
    assert stale.movements, "the cached rows must survive, or there is nothing to mark"
    assert classified.state == mv.BOARD_STALE
    assert "came from the cache" in classified.line()
    assert len(transport.calls) == 2


def test_an_ok_board_with_rows_needs_no_explanation(tmp_path):
    board = mv.RecordedProvider(
        client(tmp_path, transport=Recorder(body=flights_body()))).arrivals("EDDF")
    classified = mv.board_state(board)
    assert classified.is_ok is True
    assert classified.blank_is_explained is False
    assert classified.line() == ""


# ── stale tracking on the map ───────────────────────────────────────────

def test_a_position_older_than_the_limit_is_stale_and_says_so():
    old = state(at=NOW - timedelta(seconds=adsb.STALE_AFTER_S + 60))
    assert old.is_stale(NOW) is True
    traffic = adsb.AreaTraffic(fetch=net.Fetch(source=adsblol.SOURCE, data=[],
                                               fetched_at=NOW),
                               states=(old,))
    classified = adsb.tracking_state(online=True, traffic=traffic, now=NOW)
    assert classified.state == adsb.TRACKING_STALE
    assert "not where they are" in classified.line()


def test_a_feed_that_never_said_when_it_saw_an_aircraft_counts_as_stale():
    """The pessimistic reading of an unknown age is the safe one."""
    assert state(at=None).is_stale(NOW) is True


def test_an_area_with_no_aircraft_is_coverage_not_an_empty_sky():
    traffic = adsb.AreaTraffic(fetch=net.Fetch(source=adsblol.SOURCE, data=[],
                                               fetched_at=NOW))
    classified = adsb.tracking_state(online=True, traffic=traffic, now=NOW)
    assert classified.state == adsb.TRACKING_NO_COVERAGE
    assert "not a report that the sky is empty" in classified.line()
    assert "Baku" in classified.line()
    assert classified.blank_is_explained is True


def test_the_switch_being_off_explains_the_map_before_coverage_does():
    classified = adsb.tracking_state(online=False)
    assert classified.state == adsb.TRACKING_OFFLINE
    assert "it is not looking" in classified.line()


# ── attribution, which the licence requires wherever the data is shown ──

def test_the_adsb_lol_credit_names_the_source_and_the_licence():
    assert "adsb.lol" in adsblol.ATTRIBUTION
    assert "ODbL" in adsblol.ATTRIBUTION


def test_every_panel_that_renders_adsb_lol_data_also_renders_the_credit():
    """ODbL 1.0 asks for attribution wherever the data appears — which is
    four places, not just the map that draws the chevrons.

    Asserted against the source rather than a rendered widget so that it
    fails on the commit that adds a fifth panel, not in front of a lawyer.
    """
    import ast
    import inspect

    from aivionics.ui.pages import ops as page

    tree = ast.parse(inspect.getsource(page))
    bodies = {node.name: ast.dump(node) for node in ast.walk(tree)
              if isinstance(node, ast.FunctionDef)}
    for name in ("_show_contact", "_on_tail", "_traffic_note", "_on_fleet"):
        assert name in bodies, f"{name} has been renamed; update this test"
        assert "ATTRIBUTION" in bodies[name], (
            f"{name} renders adsb.lol data without the ODbL credit")


def test_an_observed_movement_is_credited_to_the_feed_that_supplied_it():
    """The vector names its feed; the row must not overrule it.

    `ObservedProvider` does not fetch — it is handed states by whoever does,
    so its own `source` is a guess about somebody else's fetch. It guessed
    OpenSky while the Ops screen was folding in adsb.lol, and every observed
    row on the airport page went out crediting a network that had supplied
    none of it.
    """
    provider = mv.ObservedProvider(source=adsb.SOURCE, locate=at_eddf)
    provider.observe([state(altitude_m=200.0, source=adsblol.SOURCE)], now=NOW)
    found = provider.observe(
        [state(ground=True, at=NOW + timedelta(minutes=1),
               source=adsblol.SOURCE)], now=NOW + timedelta(minutes=1))
    assert len(found) == 1
    assert found[0].source == adsblol.SOURCE, (
        "the movement was credited to the provider default rather than to "
        "the feed whose position report produced it")
    assert adsb.SOURCE not in found[0].provenance()


def test_a_vector_that_names_no_feed_falls_back_to_the_provider():
    """The fallback still has to work: not every caller is a known feed."""
    provider = mv.ObservedProvider(source="a private receiver", locate=at_eddf)
    provider.observe([state(altitude_m=200.0, source="")], now=NOW)
    found = provider.observe(
        [state(ground=True, at=NOW + timedelta(minutes=1), source="")],
        now=NOW + timedelta(minutes=1))
    assert found[0].source == "a private receiver"


def test_an_observed_board_carries_the_odbl_credit_for_the_rows_it_shows():
    """An inferred landing is adsb.lol data one derivation removed.

    ODbL 1.0 wants the credit wherever the data appears, and the arrivals
    board is one of those places: the credit has to travel with the row onto
    the panel, not stop at the map that drew the position it came from. The
    AST test above cannot see this one — the movements card takes its notes
    from the board rather than naming `ATTRIBUTION` in its own source.
    """
    provider, _movement = observed_landing()
    board = provider.arrivals("EDDF")
    assert board.movements
    assert adsblol.ATTRIBUTION in board.notes, (
        "the observed board renders adsb.lol data without the ODbL credit")
    # Once, however many rows came from that feed.
    assert board.notes.count(adsblol.ATTRIBUTION) == 1
    assert mv.OBSERVED_NOTE in board.notes


def test_the_ops_screen_folds_adsb_lol_positions_into_the_matching_provider():
    """The wiring, not the class — this is where it actually went wrong.

    Every observed test above constructs the provider with an explicit
    source, so all of them passed while the shipped screen built it with the
    default and mislabelled every row. Pin the construction site itself.
    """
    import ast
    import inspect

    from aivionics.ui import opsservice

    tree = ast.parse(inspect.getsource(opsservice))
    init = next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    built = ast.dump(init)
    assert "ObservedProvider" in built, "the provider is no longer built here"
    assert "adsblol" in built, (
        "OpsService folds adsb.lol positions into the observed provider "
        "(see area_traffic) but does not name that feed when building it")

    traffic = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef)
                   and node.name == "area_traffic")
    fed = ast.dump(traffic)
    assert "adsblol" in fed and "observe" in fed, (
        "the feed folded into the observed provider changed; the source it "
        "is constructed with has to change with it")


# ── choosing a level ────────────────────────────────────────────────────

class StubProvider:
    """A level that is connected. Used only to prove the ordering."""

    def __init__(self, level: str, name: str, available: bool = True) -> None:
        self.level, self.name, self._available = level, name, available

    def availability(self) -> mv.Availability:
        return mv.Availability(level=self.level, name=self.name,
                               available=self._available,
                               reason="" if self._available else "stub is off")

    def arrivals(self, airport: str) -> mv.MovementBoard:
        return mv.MovementBoard(airport=airport, level=self.level,
                                provider=self.name,
                                movements=(mv.Movement(airport=airport),))

    departures = arrivals


def test_the_best_available_level_wins_not_the_fastest_one(tmp_path):
    """A recorded batch answers faster than a system nobody has connected.

    Letting speed decide would quietly demote a real schedule, so the order
    is `LEVEL_ORDER` and the providers are sorted into it before anything is
    asked whether it can answer.
    """
    api = client(tmp_path)
    recorded = mv.RecordedProvider(api)
    operational = StubProvider(mv.LEVEL_OPERATIONAL, "a real FIDS")
    # Deliberately worst-first, which is the order a careless caller supplies.
    selection = mv.select([recorded, operational])
    assert selection.provider is operational
    assert selection.level == mv.LEVEL_OPERATIONAL


def test_an_unavailable_level_is_skipped_but_still_reported(tmp_path):
    api = client(tmp_path)
    selection = mv.select(mv.default_providers(api))
    # Nothing above the batch is connected in this installation, so the batch
    # answers — and the panel is still told why, for each level above it.
    assert isinstance(selection.provider, mv.RecordedProvider)
    reasons = " ".join(selection.reasons())
    assert "No operational movement system is connected" in reasons
    assert "No commercial flight-information provider is configured" in reasons
    assert "recorded history" in selection.summary()


def test_an_observed_movement_promotes_the_observed_level_above_the_batch(tmp_path):
    api = client(tmp_path)
    observed = mv.ObservedProvider(source=adsblol.SOURCE, locate=at_eddf)
    assert isinstance(mv.select(mv.default_providers(api, observed=observed)).provider,
                      mv.RecordedProvider)
    observed.observe([state(altitude_m=200.0)], now=NOW)
    observed.observe([state(ground=True, at=NOW + timedelta(minutes=1))],
                     now=NOW + timedelta(minutes=1))
    selection = mv.select(mv.default_providers(api, observed=observed))
    assert selection.provider is observed
    assert selection.level == mv.LEVEL_OBSERVED


def test_no_level_at_all_is_a_state_rather_than_an_exception():
    selection = mv.select([StubProvider(mv.LEVEL_COMMERCIAL, "off", False)])
    assert selection.ok is False
    assert "No movement source is available at any level" in selection.summary()


def test_the_default_providers_are_the_four_levels_in_order(tmp_path):
    levels = [p.level for p in mv.default_providers(client(tmp_path))]
    assert levels == list(mv.LEVEL_ORDER)


# ── credentials: never rendered, never logged, never in a URL ───────────

SECRET = "sk-live-9f3c17d4e2b8"


def vendor_config(url: str, *, secret: str = SECRET) -> mv.CommercialConfig:
    def adapter(payload, airport, arriving):
        return (mv.Movement(
            airport=airport, arriving=arriving, flight_number="LH491",
            scheduled_at=NOW, estimated_at=NOW + timedelta(minutes=7),
            gate="A14", terminal="1", status=mv.STATUS_ESTIMATED,
            source="Vendor", level=mv.LEVEL_COMMERCIAL,
            kind=mv.KIND_CONFIRMED, confidence=mv.CONFIDENCE_REPORTED,
            last_updated=NOW),)

    return mv.CommercialConfig(
        vendor="Vendor", arrivals_url=lambda icao: url,
        departures_url=lambda icao: url, adapter=adapter,
        credential=mv.Credential(secret, origin="VENDOR_KEY"), ttl=60.0)


def test_a_credential_is_redacted_in_every_representation_it_has():
    credential = mv.Credential(SECRET, origin="VENDOR_KEY")
    assert credential.present is True
    for text in (repr(credential), str(credential), credential.describe(),
                 f"{credential}", format(credential)):
        assert SECRET not in text
    assert "VENDOR_KEY" in credential.describe()
    assert "never shown" in credential.describe()


def test_a_url_carrying_the_secret_is_refused_before_the_fetch(tmp_path):
    """`net.DiskCache.put` writes the fetched URL into a plaintext JSON file
    and `Fetch.url` is rendered in provenance lines. A vendor that
    authenticates by query parameter cannot be used through this client, and
    saying so is the correct outcome rather than a workaround."""
    transport = Recorder(body="{}")
    api = client(tmp_path, transport=transport)
    leaky = f"https://opensky-network.org/api/flights?key={SECRET}"
    board = mv.CommercialProvider(client=api,
                                  config=vendor_config(leaky)).arrivals("EDDF")
    assert board.ok is False
    assert "refused" in board.error
    assert SECRET not in board.error
    assert transport.calls == [], "the secret reached the transport"
    assert list((tmp_path / "cache").glob("*.json")) == []


def test_the_secret_reaches_no_rendered_row_no_log_and_no_repr(tmp_path):
    """The successful path, which is the one that would leak quietly.

    No vendor host is on the allow-list, because no vendor has been bought.
    The fixture borrows an allow-listed host so the adapter path runs at all;
    what is being asserted is the credential handling, not the vendor.
    """
    transport = Recorder(body=json.dumps({"arrivals": []}))
    api = client(tmp_path, transport=transport)
    config = vendor_config("https://opensky-network.org/api/vendor/arrivals")
    provider = mv.CommercialProvider(client=api, config=config)
    assert provider.availability().available is True

    board = provider.arrivals("EDDF")
    assert board.ok is True
    movement = board.movements[0]

    rendered = [board.headline(), board.provenance(), mv.board_state(board).line(),
                movement.identity, movement.provenance(), movement.time_text(),
                movement.delay_text(), movement.place_text(),
                repr(board), repr(movement), repr(config), repr(provider),
                repr(config.credential)]
    rendered += [row.line() for row in mv.select([provider]).availability]
    rendered += [repr(row) for row in api.log.rows()]
    rendered += [path.read_text(encoding="utf-8")
                 for path in (tmp_path / "cache").glob("*.json")]
    rendered += transport.calls
    for text in rendered:
        assert SECRET not in text, f"the credential leaked into: {text[:120]}"


def test_a_configured_vendor_with_no_credential_is_unavailable_not_broken(tmp_path):
    config = vendor_config("https://opensky-network.org/api/vendor/arrivals",
                           secret="")
    provider = mv.CommercialProvider(client=client(tmp_path), config=config)
    assert provider.availability().available is False
    assert "no credential" in provider.availability().reason
    assert mv.board_state(provider.arrivals("EDDF")).state == mv.BOARD_UNAVAILABLE


def test_a_commercial_movement_keeps_its_three_times_apart(tmp_path):
    """Scheduled, estimated and actual are three fields and never one column.

    Collapsed into one, a screen shows a scheduled time beside the word
    "landed" and the reader takes the pair as an on-time report.
    """
    api = client(tmp_path, transport=Recorder(body=json.dumps({"arrivals": []})))
    board = mv.CommercialProvider(
        client=api,
        config=vendor_config("https://opensky-network.org/api/vendor/arrivals"),
    ).arrivals("EDDF")
    movement = board.movements[0]
    assert movement.scheduled_at is not None
    assert movement.estimated_at is not None
    assert movement.actual_at is None
    assert movement.time_basis() == "estimated"
    assert "estimated" in movement.time_text()
    assert movement.delay() == timedelta(minutes=7)
    assert movement.delay_text() == "7 min late"
    # This level is the only one entitled to a gate, and it has one.
    assert "Gate A14" in movement.place_text()


# ── the geometry the observed level rests on ────────────────────────────

def test_the_distance_used_to_attribute_a_movement_is_a_great_circle():
    # EDDF to KSEA: about 8,200 km, so a shade over 4,420 nm by great
    # circle, and nothing like it by any flat approximation.
    gap = mv.distance_nm(50.033, 8.570, 47.449, -122.309)
    assert 4400 < gap < 4460
    assert mv.distance_nm(50.0, 8.0, 50.0, 8.0) == pytest.approx(0.0)
    assert mv.distance_nm(50.0, 8.0, 50.1, 8.0) == pytest.approx(6.0, abs=0.2)
