"""Phase 4B.4–4B.6: airports, weather, live traffic and the network layer.

**No test in this file opens a socket.** Every network path is driven through
an injected transport, and the allow-list, the master switch and the cache
are asserted rather than assumed. A test that reached aviationweather.gov
would pass on a laptop and fail in a locked-down hangar, which is exactly the
environment this application is built for.

The airport index is pointed at a fixture directory of small CSVs, so the
tests do not depend on the 12.7 MB bundled file or on any particular airport
still existing in a future OurAirports snapshot.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from aivionics import db
from aivionics.ops import adsb, airports, compliance, net, weather

UTC = timezone.utc

# ── fixture CSVs ────────────────────────────────────────────────────────
# Real coordinates, because the timezone assertions depend on the position
# resolving to a real IANA zone. Everything else is trimmed.

AIRPORTS_CSV = '''"id","ident","type","name","latitude_deg","longitude_deg","elevation_ft","continent","iso_country","iso_region","municipality","scheduled_service","icao_code","iata_code","gps_code","local_code","home_link","wikipedia_link","keywords"
1,"EDDF","large_airport","Frankfurt Main Airport",50.033333,8.570556,364,"EU","DE","DE-HE","Frankfurt am Main","yes","EDDF","FRA","EDDF",,,,
2,"KSEA","large_airport","Seattle-Tacoma International Airport",47.449001,-122.308998,433,"NA","US","US-WA","Seattle","yes","KSEA","SEA","KSEA","SEA",,,
3,"YSSY","large_airport","Sydney Kingsford Smith International Airport",-33.946098,151.177002,21,"OC","AU","AU-NSW","Sydney","yes","YSSY","SYD","YSSY",,,,
4,"FRAGN","small_airport","Campo di Volo Delta Club",45.1,10.2,120,"EU","IT","IT-25","Cremona","no",,,"FRAGN","FRAGN",,,
5,"XXNO","heliport","No Position Heliport",,,,"NA","US","US-TX","Nowhere","no",,,,,,,
6,"UBBB","large_airport","Heydar Aliyev International Airport",40.467498,50.046699,10,"AS","AZ","AZ-BA","Baku","yes","UBBB","GYD","UBBB",,,,
'''

RUNWAYS_CSV = '''"id","airport_ref","airport_ident","length_ft","width_ft","surface","lighted","closed","le_ident","le_latitude_deg","le_longitude_deg","le_elevation_ft","le_heading_degT","le_displaced_threshold_ft","he_ident","he_latitude_deg","he_longitude_deg","he_elevation_ft","he_heading_degT","he_displaced_threshold_ft"
10,1,"EDDF",13123,197,"ASP",1,0,"07C",,,,68,,"25C",,,,248,
11,1,"EDDF",9186,148,"CON",1,1,"07L",,,,68,,"25R",,,,248,
12,2,"KSEA",11901,150,"ASP",1,0,"16L",,,,181,,"34R",,,,1,
'''

FREQUENCIES_CSV = '''"id","airport_ref","airport_ident","type","description","frequency_mhz"
20,1,"EDDF","TWR","Frankfurt Tower",119.900
21,1,"EDDF","ATIS","Frankfurt ATIS (ARR)",118.030
22,2,"KSEA","TWR","Seattle Tower",119.900
'''

COUNTRIES_CSV = '''"id","code","name","continent","wikipedia_link","keywords"
30,"DE","Germany","EU",,
31,"US","United States","NA",,
32,"AU","Australia","OC",,
33,"AZ","Azerbaijan","AS",,
34,"IT","Italy","EU",,
'''


@pytest.fixture
def data_dir(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    for name, body in (("airports.csv", AIRPORTS_CSV),
                       ("runways.csv", RUNWAYS_CSV),
                       ("airport-frequencies.csv", FREQUENCIES_CSV),
                       ("countries.csv", COUNTRIES_CSV)):
        (directory / name).write_text(body, encoding="utf-8")
    return directory


@pytest.fixture
def index(data_dir):
    return airports.AirportIndex(data_dir).load()


# ── airport lookup (PLAN 4B.5) ──────────────────────────────────────────

def test_lookup_by_icao_iata_and_gps_code(index):
    assert index.get("EDDF").name == "Frankfurt Main Airport"
    assert index.get("eddf").iata == "FRA"
    assert index.get("FRA").icao == "EDDF"
    assert index.get("SEA").icao == "KSEA"
    assert index.get("ZZZZ") is None


def test_search_ranks_the_identifier_above_a_name_match(index):
    assert index.search("EDDF")[0].icao == "EDDF"
    assert index.search("FRA")[0].icao == "EDDF"
    assert index.search("Sydney")[0].icao == "YSSY"
    assert index.search("seattle")[0].icao == "KSEA"


def test_search_needs_two_characters(index):
    assert index.search("E") == []
    assert index.search("") == []


def test_local_code_does_not_win_a_prefix_search(index):
    """FRAGN is a local code; typing FRA must not surface an ultralight strip.

    Prefix matching is offered on ICAO and IATA only. The regression this
    guards is real: with local codes prefixable, "FRA" put an Italian
    ultralight field above Frankfurt.
    """
    results = index.search("FRA")
    assert results[0].icao == "EDDF"
    assert index.get("FRAGN").name == "Campo di Volo Delta Club"


def test_a_row_without_a_position_is_dropped(index):
    assert index.get("XXNO") is None
    assert index.count == 5


def test_runways_and_frequencies(index):
    runways = index.runways("EDDF")
    assert [r.designation for r in runways] == ["07C/25C", "07L/25R"]
    assert runways[0].dimension_text() == "13,123 × 197 ft (4,000 × 60 m)"
    assert runways[0].closed is False and runways[1].closed is True
    assert [f.type for f in index.frequencies("EDDF")] == ["ATIS", "TWR"]
    assert index.frequencies("EDDF")[1].mhz_text() == "119.900"
    assert index.runways("ZZZZ") == ()


def test_country_name_and_elevation(index):
    airport = index.get("EDDF")
    assert airport.where() == "Frankfurt am Main, Germany"
    assert airport.elevation_text() == "364 ft (111 m)"
    assert airport.code_line() == "EDDF / FRA"


def test_a_missing_csv_is_a_state_not_a_crash(tmp_path):
    empty = airports.AirportIndex(tmp_path / "nothing").load()
    assert empty.count == 0
    assert "could not be read" in empty.load_error
    assert "UNAVAILABLE" in empty.provenance()
    assert empty.search("EDDF") == []


# ── timezone: IANA name, resolved from position (PLAN 4B.5) ─────────────

def test_timezone_comes_from_the_position_as_an_iana_name(index):
    assert airports.timezone_for(index.get("EDDF")) == "Europe/Berlin"
    assert airports.timezone_for(index.get("KSEA")) == "America/Los_Angeles"
    assert airports.timezone_for(index.get("YSSY")) == "Australia/Sydney"


def test_local_time_is_correct_either_side_of_a_dst_boundary(index):
    """The reason a zone name is stored and an offset never is.

    Europe/Berlin was UTC+2 on 26 October 2025 at 00:00Z and UTC+1 an hour
    later; a cached offset is wrong for half the year, and wrong by an hour
    on the day it changes.
    """
    airport = index.get("EDDF")
    before = airports.local_time(airport, datetime(2025, 10, 26, 0, 30, tzinfo=UTC))
    after = airports.local_time(airport, datetime(2025, 10, 26, 1, 30, tzinfo=UTC))
    assert before.utcoffset() == timedelta(hours=2)
    assert before.strftime("%H:%M") == "02:30"
    assert after.utcoffset() == timedelta(hours=1)
    assert after.strftime("%H:%M") == "02:30"

    summer = airports.local_time(airport, datetime(2026, 7, 1, 12, 0, tzinfo=UTC))
    winter = airports.local_time(airport, datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    assert summer.strftime("%H:%M") == "14:00"
    assert winter.strftime("%H:%M") == "13:00"


def test_local_time_text_names_the_zone_never_an_offset_alone(index):
    text = airports.local_time_text(index.get("KSEA"),
                                    datetime(2026, 7, 1, 12, 0, tzinfo=UTC))
    assert "America/Los_Angeles" in text
    assert text.startswith("05:00")


# ── projection (PLAN 4B.4) ──────────────────────────────────────────────

def test_projection_places_the_corners_and_the_origin(index):
    assert airports.project(0, 0, 1000, 500) == (500.0, 250.0)
    assert airports.project(85, -180, 1000, 500) == (0.0, 0.0)
    assert airports.project(-85, 180, 1000, 500) == (1000.0, 500.0)


def test_projection_clamps_latitude_and_wraps_longitude():
    """A bad position lands on the canvas edge, never outside it."""
    _, y = airports.project(89.9, 0, 1000, 500)
    assert y == 0.0
    _, low = airports.project(-91.0, 0, 1000, 500)
    assert low == 500.0
    assert airports.project(0, 190, 1000, 500)[0] == pytest.approx(
        airports.project(0, -170, 1000, 500)[0])


def test_projection_round_trips():
    for latitude, longitude in ((50.03, 8.56), (-33.95, 151.18), (0.0, 0.0)):
        x, y = airports.project(latitude, longitude, 900, 425)
        back_lat, back_lon = airports.unproject(x, y, 900, 425)
        assert back_lat == pytest.approx(latitude, abs=1e-6)
        assert back_lon == pytest.approx(longitude, abs=1e-6)


# ── METAR decoding (PLAN 4B.5) ──────────────────────────────────────────

def test_metar_decodes_the_fields_the_screen_shows():
    report = weather.decode_metar(
        "EDDF 081720Z 25012G24KT 210V280 3000 -RA BR BKN008 OVC020 14/12 Q1004",
        now=datetime(2026, 8, 8, 18, 0, tzinfo=UTC))
    assert report.station == "EDDF"
    assert report.issued_at == datetime(2026, 8, 8, 17, 20, tzinfo=UTC)
    assert report.wind.direction_deg == 250
    assert report.wind.speed_kt == 12 and report.wind.gust_kt == 24
    assert (report.wind.varies_from, report.wind.varies_to) == (210, 280)
    assert report.visibility_m == 3000
    assert report.ceiling_ft == 800
    assert report.temperature_c == 14 and report.dewpoint_c == 12
    assert report.qnh_hpa == 1004
    assert report.relative_humidity == pytest.approx(88, abs=1)
    assert report.weather == ("light rain", "mist")
    assert report.flight_category == "IFR"


def test_metar_keeps_the_units_the_report_used():
    """10SM is 16 km, not "10 km or more" — that phrase belongs to 9999."""
    us = weather.decode_metar("KSEA 081753Z 19012KT 10SM BKN035 12/10 A2985")
    assert us.visibility_unit == "SM"
    assert "10 SM" in us.visibility_text()
    assert us.qnh_inhg == pytest.approx(29.85)
    assert us.qnh_hpa is None

    icao = weather.decode_metar("EDDF 081720Z 25008KT 9999 FEW035 24/14 Q1018")
    assert icao.visibility_text() == "10 km or more"
    assert icao.flight_category == "VFR"


def test_metar_handles_fractional_statute_miles_and_negative_temperatures():
    report = weather.decode_metar(
        "KJFK 081751Z 00000KT 1 1/2SM BR SCT002 OVC004 M03/M04 A3002")
    assert report.wind.calm is True
    assert report.wind.text() == "calm"
    assert report.visibility_m == pytest.approx(2414.0, abs=1)
    assert report.temperature_c == -3 and report.dewpoint_c == -4
    assert report.flight_category == "LIFR"


def test_cavok_is_a_flight_category_not_a_blank():
    report = weather.decode_metar("LOWW 081720Z VRB02KT CAVOK 26/12 Q1016")
    assert report.cavok is True
    assert report.wind.variable is True
    assert report.flight_category == "VFR"
    assert "CAVOK" in report.visibility_text()


def test_a_malformed_metar_leaves_the_raw_text_intact():
    """The rule that outranks every other decision in `weather`.

    A decoder bug must never be able to hide the observation it misread, so
    a report that decodes to nothing still carries its original string and
    says plainly that it did not decode.
    """
    raw = "EDDF 08172 GARBAGE ///// NOT-A-GROUP 99/99 QXYZ"
    report = weather.decode_metar(raw)
    assert report.raw == raw
    assert report.decoded is False
    assert "GARBAGE" in report.undecoded
    assert report.flight_category == ""
    assert report.visibility_text() == "visibility not reported"

    empty = weather.decode_metar("")
    assert empty.raw == "" and empty.decoded is False


def test_a_trend_group_is_never_read_as_current_conditions():
    report = weather.decode_metar(
        "EDDF 081720Z 25012KT 9999 SCT035 24/14 Q1018 TEMPO 2000 RA BKN008")
    assert report.visibility_m == 9999          # not the 2000 in the trend
    assert report.ceiling_ft is None            # not the BKN008 in the trend
    assert report.undecoded[0] == "TEMPO"


def test_remarks_are_kept_but_not_decoded():
    report = weather.decode_metar(
        "KSEA 081753Z 19012KT 10SM BKN035 12/10 A2985 RMK AO2 SLP110")
    assert report.decoded is True
    assert any(group.startswith("RMK") for group in report.undecoded)


def test_taf_is_shown_as_issued_and_not_interpreted():
    forecast = weather.decode_taf(
        "TAF EDDF 081700Z 0818/0924 25012KT 4000 -RA BKN010 "
        "BECMG 0820/0822 27008KT 9999 SCT020")
    assert forecast.station == "EDDF"
    assert forecast.valid_from == "08/18Z" and forecast.valid_to == "09/24Z"
    assert len(forecast.lines()) == 2
    assert forecast.lines()[1].startswith("BECMG")


# ── the network layer (PLAN 4B.6, standing rule 12) ─────────────────────

class Recorder:
    """A transport that never opens a socket and remembers what it was asked."""

    def __init__(self, body: str = "OK", status: int = 200) -> None:
        self.body, self.status, self.calls = body, status, []

    def __call__(self, url: str, timeout: float) -> tuple[int, str]:
        self.calls.append(url)
        return self.status, self.body


def client(tmp_path, online=True, transport=None, **kw) -> net.NetClient:
    return net.NetClient(
        online=lambda: online,
        transport=transport or Recorder(),
        cache=net.DiskCache(tmp_path / "cache"),
        limiter=net.RateLimiter(min_interval=0.0),
        log=net.FetchLog(), **kw)


def test_an_unlisted_host_is_refused_before_a_socket_is_opened(tmp_path):
    transport = Recorder()
    api = client(tmp_path, transport=transport)
    for url in ("https://example.com/x",
                "https://opensky-network.org.evil.test/api/states/all",
                "http://aviationweather.gov/api/data/metar"):
        result = api.get(url, "test")
        assert result.ok is False
        assert "allow-list" in result.error or "not https" in result.error
    assert transport.calls == []


def test_subdomains_of_an_allow_listed_host_are_accepted():
    assert net.is_allowed("https://aviationweather.gov/api/data/metar")
    assert net.is_allowed("https://api.opensky-network.org/states/all")
    assert not net.is_allowed("https://opensky-network.org.example.com/x")
    assert not net.is_allowed("http://aviationweather.gov/x")


def test_every_network_entry_point_is_a_no_op_when_online_is_off(tmp_path):
    """GATE 4B: the application does no work at all on behalf of an online
    feature while the switch is off."""
    transport = Recorder()
    api = client(tmp_path, online=False, transport=transport)
    con = seeded_db(tmp_path)

    results = [
        weather.fetch_metar(api, "EDDF"),
        weather.fetch_taf(api, "EDDF"),
        adsb.fetch_arrivals(api, "EDDF"),
        adsb.fetch_departures(api, "EDDF"),
    ]
    for result in results:
        assert result.ok is False
        assert result.fetch.error == net.OFFLINE_REASON

    snapshot = adsb.fleet_positions(api, con)
    assert snapshot.ok is False
    assert snapshot.fetch.error == net.OFFLINE_REASON
    assert snapshot.seen == ()

    assert transport.calls == []
    assert api.log.rows() == []


def test_the_cache_serves_a_second_call_within_the_ttl(tmp_path):
    transport = Recorder(body="EDDF 081720Z 25008KT CAVOK 24/14 Q1018")
    api = client(tmp_path, transport=transport)
    first = api.get("https://aviationweather.gov/api/data/metar?ids=EDDF",
                    "wx", ttl=600)
    second = api.get("https://aviationweather.gov/api/data/metar?ids=EDDF",
                     "wx", ttl=600)
    assert first.from_cache is False and second.from_cache is True
    assert second.data == first.data
    assert len(transport.calls) == 1


def test_an_expired_cache_entry_is_refetched(tmp_path):
    """`ttl=0` means fetch now, whatever the clock's resolution says."""
    transport = Recorder(body="first")
    api = client(tmp_path, transport=transport)
    url = "https://aviationweather.gov/api/data/metar?ids=EDDF"
    api.get(url, "wx", ttl=600)
    transport.body = "second"
    assert api.get(url, "wx", ttl=0).data == "second"
    assert len(transport.calls) == 2

    cache = net.DiskCache(tmp_path / "ttl")
    stamp = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    cache.put("https://aviationweather.gov/x", "body", "wx", stamp)
    fresh = cache.get("https://aviationweather.gov/x", 600,
                      now=stamp + timedelta(seconds=599))
    assert fresh is not None and fresh[0] == "body"
    assert cache.get("https://aviationweather.gov/x", 600,
                     now=stamp + timedelta(seconds=600)) is None


def test_a_failed_fetch_falls_back_to_a_stale_entry_and_says_so(tmp_path):
    """A position from four minutes ago, labelled, beats an empty map."""
    transport = Recorder(body="cached body")
    api = client(tmp_path, transport=transport)
    url = "https://aviationweather.gov/api/data/metar?ids=EDDF"
    api.get(url, "wx", ttl=600)

    def failing(url, timeout):
        raise TimeoutError("no route to host")

    api.transport = failing
    api.sleep = lambda seconds: None
    stale = api.get(url, "wx", ttl=0)
    assert stale.ok is True and stale.stale is True
    assert "SERVED STALE" in stale.provenance()


def test_the_fetch_log_records_contacts_but_not_cache_hits(tmp_path):
    """What Admin renders under "contacted this session"."""
    transport = Recorder(body="body")
    api = client(tmp_path, transport=transport)
    url = "https://aviationweather.gov/api/data/metar?ids=EDDF"
    api.get(url, "wx", ttl=600)
    api.get(url, "wx", ttl=600)                 # cache hit: not a contact
    api.get("https://example.com/x", "rogue")   # refused: very much a contact

    rows = {row.source: row for row in api.log.rows()}
    assert rows["wx"].attempts == 1
    assert rows["wx"].failures == 0
    assert rows["wx"].last_success is not None
    assert rows["wx"].host == "aviationweather.gov"
    assert "last succeeded" in rows["wx"].line()

    assert rows["rogue"].attempts == 1
    assert rows["rogue"].failures == 1
    assert rows["rogue"].last_success is None
    assert "none succeeded" in rows["rogue"].line()


def test_every_outbound_host_appears_in_the_admin_registry():
    """GATE 4B: nothing may call out that Admin does not render."""
    listed = {host.host for host in net.HOST_REGISTRY}
    assert listed == set(net.ALLOWED_HOSTS)
    assert net.host_of(weather.metar_url("EDDF")) in listed
    assert net.host_of(weather.taf_url("EDDF")) in listed
    assert net.host_of(adsb.states_url(["3c6444"])) in listed
    assert all(host.purpose and host.terms for host in net.HOST_REGISTRY)


# ── ADS-B (PLAN 4B.4) ───────────────────────────────────────────────────

def seeded_db(tmp_path, tails=(("N101AV", "3C6444"), ("N202AV", "4008f3"),
                               ("N303AV", None))) -> sqlite3.Connection:
    con = db.connect(tmp_path / "ops.db")
    compliance.ensure_schema(con)
    for i, (tail, address) in enumerate(tails, start=1):
        con.execute("INSERT INTO aircraft(id,tail,type,icao24) VALUES(?,?,?,?)",
                    (i, tail, "B737-8", address))
    con.commit()
    return con


def states_body(*rows) -> str:
    return json.dumps({"time": 1786000000, "states": list(rows)})


def state_row(address, lat, lon, **kw):
    return [address, kw.get("callsign", "AVN101 "), "Germany", 1786000000,
            1786000000, lon, lat, kw.get("alt", 11277.0), kw.get("ground", False),
            kw.get("speed", 235.0), kw.get("track", 78.0), kw.get("rate", 2.5),
            None, kw.get("geo", 11360.0), "1000", False, 0]


def test_tails_map_to_transponder_addresses_case_insensitively(tmp_path):
    con = seeded_db(tmp_path)
    assert adsb.tail_to_icao24(con) == {"N101AV": "3c6444", "N202AV": "4008f3"}
    assert adsb.untracked_tails(con) == ["N303AV"]
    assert adsb.tail_to_icao24(None) == {}


def test_a_tail_with_no_position_is_reported_as_unseen_not_as_grounded(tmp_path):
    """The distinction the whole map hangs on (PLAN 4B.4)."""
    transport = Recorder(body=states_body(state_row("3c6444", 50.03, 8.56)))
    snapshot = adsb.fleet_positions(client(tmp_path, transport=transport),
                                    seeded_db(tmp_path))
    assert [p.tail for p in snapshot.seen] == ["N101AV"]
    assert [p.tail for p in snapshot.unseen] == ["N202AV"]
    assert snapshot.unseen[0].reason == "not seen by the network in this fetch"
    assert snapshot.untracked == ("N303AV",)
    assert "seen by the network" in snapshot.summary()
    assert "ground" not in snapshot.summary()


def test_state_vector_units_are_converted_for_the_panel(tmp_path):
    transport = Recorder(body=states_body(state_row("3c6444", 50.03, 8.56)))
    snapshot = adsb.fleet_positions(client(tmp_path, transport=transport),
                                    seeded_db(tmp_path))
    state = snapshot.seen[0].state
    assert state.altitude_ft == 36998
    assert state.speed_kt == 457
    assert state.vertical_rate_fpm == 492
    assert state.heading_text() == "078° true"
    assert "climbing" in state.vertical_text()


def test_a_short_state_row_is_dropped_rather_than_read_off_by_one():
    assert adsb.parse_state(["3c6444", "X", "DE"]) is None
    assert adsb.parse_state([]) is None
    assert adsb.parse_state(["not-hex", "X", "DE", 0, 0, 1, 1, 1, False, 1, 1, 1]) is None
    assert adsb.parse_state(state_row("3c6444", 50.0, 8.0)).icao24 == "3c6444"


def test_an_aircraft_with_no_address_on_file_cannot_be_tracked(tmp_path):
    con = seeded_db(tmp_path, tails=(("N303AV", None),))
    snapshot = adsb.fleet_positions(client(tmp_path), con)
    assert snapshot.ok is False
    assert "no tail in the fleet register" in snapshot.fetch.error
    assert snapshot.untracked == ("N303AV",)


def test_the_states_url_filters_by_address_and_drops_junk():
    url = adsb.states_url(["3C6444", "not-hex", "", "4008f3", "3c6444"])
    assert url.count("icao24=") == 2
    assert "icao24=3c6444" in url and "icao24=4008f3" in url


def test_an_inferred_airport_with_several_candidates_is_flagged(tmp_path):
    body = json.dumps([{
        "icao24": "3c6444", "callsign": "DLH491",
        "estDepartureAirport": "KSEA", "estArrivalAirport": "EDDF",
        "firstSeen": 1785990000, "lastSeen": 1786000000,
        "departureAirportCandidatesCount": 1,
        "arrivalAirportCandidatesCount": 4}])
    movements = adsb.fetch_arrivals(
        client(tmp_path, transport=Recorder(body=body)), "EDDF")
    assert movements.ok is True
    flight = movements.flights[0]
    assert flight.other_end("EDDF") == "KSEA"
    assert flight.uncertain(arriving=True) is True
    assert flight.uncertain(arriving=False) is False


def test_no_movements_is_reported_as_an_answer_not_a_failure(tmp_path):
    movements = adsb.fetch_departures(
        client(tmp_path, transport=Recorder(body="[]")), "EDDF")
    assert movements.ok is False
    assert "no departures recorded" in movements.fetch.error
    assert "coverage is incomplete" in movements.fetch.error


def test_the_coverage_warning_says_what_a_missing_aircraft_means():
    assert "incomplete coverage by design" in adsb.COVERAGE_WARNING
    assert "not necessarily on the ground" in adsb.COVERAGE_WARNING
    assert "not a traffic display" in adsb.COVERAGE_WARNING


# ── the service the screen actually calls ───────────────────────────────

def test_the_service_never_writes_the_database(tmp_path):
    """`mode=ro` is the contract, not a precaution."""
    from aivionics.ui.opsservice import OpsService

    path = tmp_path / "ops.db"
    seeded_db(tmp_path).close()
    service = OpsService(path, online=lambda: False)
    con = service.connection()
    assert con is not None
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO aircraft(tail,type) VALUES('N999AV','B737-8')")
    service.close()


def test_the_service_tolerates_a_missing_database(tmp_path):
    from aivionics.ui.opsservice import OpsService

    service = OpsService(tmp_path / "absent.db", online=lambda: False)
    assert service.connection() is None
    assert service.tail_record("N101AV").reason.startswith("no database")
    assert service.fleet().ok is False


def test_the_click_through_returns_defects_and_compliance(tmp_path, monkeypatch):
    from aivionics.ui.opsservice import OpsService

    path = tmp_path / "ops.db"
    con = seeded_db(tmp_path)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    con.execute(
        "INSERT INTO defect(id,aircraft_tail,reported_at,ata_ref,defect_text,"
        "source) VALUES(1,'N101AV','2026-07-29','34-11','PITOT HEAT FAIL','sdr')")
    con.execute(
        "INSERT INTO import_batch(batch_id,source_system,rows_total,"
        "rows_imported,rows_rejected,imported_at) VALUES('b1','AMOS',1,1,0,?)",
        (stamp,))
    con.execute(
        "INSERT INTO compliance_item(id,aircraft_tail,kind,ref,description,"
        "due_date,source_system,imported_at,batch_id,status)"
        " VALUES(1,'N101AV','mel','MEL 34-11-02A','Pitot heat monitor',"
        "'2026-12-01','AMOS',?, 'b1','open')", (stamp,))
    con.commit()
    con.close()

    service = OpsService(path, online=lambda: False)
    record = service.tail_record("n101av")
    assert record.tail == "N101AV"
    assert record.total_defects == 1
    assert record.defects[0]["ata_ref"] == "34-11"
    assert len(record.compliance_rows) == 1
    assert record.compliance_rows[0].provenance.source_system == "AMOS"
    service.close()
