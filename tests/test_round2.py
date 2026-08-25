"""Round 2 of the owner's feedback — the map viewport, area traffic, radar,
airport photos, the editable clock and the recovery code.

Where a thing can only be got wrong on screen, it is asserted against the
real widget. Where it is maths, it is asserted as maths. Nothing here opens a
socket: every network path is exercised through an injected transport, so
these tests say the same thing on a machine with the cable out.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from aivionics import db
from aivionics.ops import adsb, net, photos, radar
from aivionics.ui import auth, store


@pytest.fixture
def con(tmp_path):
    connection = db.connect(tmp_path / "round2.db")
    auth.seed(connection)
    auth.reset_throttle()
    yield connection
    connection.close()


# ── R1: the map has a viewport ──────────────────────────────────────────

@pytest.fixture(scope="module")
def qt_app():
    import os
    if os.environ.get("QT_QPA_PLATFORM") in {"offscreen", "minimal"}:
        pytest.skip("needs the native platform")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    try:
        return widgets.QApplication.instance() or widgets.QApplication([])
    except Exception as exc:                        # pragma: no cover
        pytest.skip(f"no Qt platform available: {exc}")


def _map(qt_app, width=900, height=460):
    from PySide6.QtCore import Qt

    from aivionics.ui.mapview import MapView
    view = MapView("light")
    view.resize(width, height)
    view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    view.show()
    for _ in range(4):
        qt_app.processEvents()
    return view


def test_the_projection_round_trips_at_every_zoom(qt_app):
    view = _map(qt_app)
    try:
        for zoom in (1.0, 4.0, 32.0, 200.0):
            view.reset_view()
            view.focus_on(52.5, 13.4, zoom)
            x, y = view._point(52.5, 13.4)
            back = view._latlon_at(x, y)
            assert abs(back[0] - 52.5) < 0.01, zoom
            assert abs(back[1] - 13.4) < 0.01, zoom
    finally:
        view.close()


def test_zooming_holds_the_ground_under_the_cursor(qt_app):
    """The one property that separates a map from a picture that changes size.
    Without it the thing you were looking at slides away as you zoom in."""
    from PySide6.QtCore import QPoint

    view = _map(qt_app)
    try:
        anchor = QPoint(300, 200)
        before = view._latlon_at(300, 200)
        for _ in range(4):
            view.zoom_by(1.6, anchor)
        after = view._latlon_at(300, 200)
        assert abs(after[0] - before[0]) < 0.01
        assert abs(after[1] - before[1]) < 0.01
    finally:
        view.close()


def test_the_world_cannot_be_dragged_off_the_widget(qt_app):
    view = _map(qt_app)
    try:
        view.zoom_by(8.0)
        view._set_view(view.zoom, -50.0, 90.0)
        assert 0.0 < view._cx < 1.0
        assert 0.0 < view._cy < 1.0
        # at zoom 1 the world is centred and cannot be moved at all
        view.reset_view()
        view._set_view(view.zoom, 0.1, 0.9)
        assert (view._cx, view._cy) == (0.5, 0.5)
    finally:
        view.close()


def test_focus_puts_the_place_in_the_middle(qt_app):
    view = _map(qt_app)
    try:
        view.focus_on(40.4728, 50.0509, 28.0)      # Heydar Aliyev
        x, y = view._point(40.4728, 50.0509)
        assert abs(x - view.width() / 2) < 1.5
        assert abs(y - view.height() / 2) < 1.5
    finally:
        view.close()


# ── R2: traffic is always bounded by what is on screen ──────────────────

def test_an_area_query_is_a_box_and_never_the_world_feed():
    url = adsb.area_url(47.0, 5.0, 55.0, 16.0)
    assert "lamin=47.0" in url and "lomax=16.0" in url
    assert "icao24" not in url
    assert adsb.area_too_large(-90, -180, 90, 180), "the whole world is refused"
    assert not adsb.area_too_large(47, 5, 55, 16)


def test_a_box_too_large_is_refused_without_calling_out():
    """The refusal has to happen before the request, not after it — the point
    is not to spend the credit, not to spend it and apologise."""
    calls = []

    def transport(url, timeout):
        calls.append(url)
        return 200, "{}"

    client = net.NetClient(online=lambda: True, transport=transport)
    result = adsb.area_traffic(client, -80.0, -170.0, 80.0, 170.0)
    assert not result.ok
    assert "zoom in" in result.fetch.error.lower()
    assert calls == []


# ── R3: Mercator tiles on a plate-carrée map ────────────────────────────

def test_tile_bounds_agree_with_the_band_helper():
    from aivionics.ui.mapview import _band_lat
    z, x, y = 5, 16, 10
    north, _west, south, _east = radar.tile_bounds(x, y, z)
    assert _band_lat(y, 2 ** z) == pytest.approx(north, abs=1e-9)
    assert _band_lat(y + 1, 2 ** z) == pytest.approx(south, abs=1e-9)


def test_a_mercator_tile_is_not_uniform_in_latitude():
    """The reason the renderer slices tiles instead of blitting them once.
    If these spans were equal, a single stretched blit would be correct."""
    from aivionics.ui.mapview import _band_lat
    z, y, n = 5, 3, 2 ** 5           # a high-latitude row
    lats = [_band_lat(y + i / 16, n) for i in range(17)]
    spans = [a - b for a, b in zip(lats, lats[1:])]
    assert all(a > b for a, b in zip(lats, lats[1:])), "must run north to south"
    assert max(spans) / min(spans) > 1.15, "the distortion is real and matters"


def test_tile_selection_is_capped_and_scaled_to_the_view():
    assert radar.zoom_for(360.0, 900) <= 2
    assert radar.zoom_for(2.0, 900) >= 7
    assert len(radar.tiles_for(-80, -170, 80, 170, 6, limit=12)) == 12


def test_radar_says_why_it_is_empty_rather_than_going_quiet():
    client = net.NetClient(online=lambda: False)
    index = radar.radar_index(client)
    assert not index.ok
    assert index.fetch.error
    assert index.latest is None


# ── the one client learned to fetch bytes, and still obeys the switch ───

def test_binary_fetches_go_through_the_same_switch_and_cache(tmp_path):
    payload = bytes(range(256)) * 4
    calls = []

    def binary(url, timeout):
        calls.append(url)
        return 200, payload

    cache = net.DiskCache(tmp_path / "cache")
    client = net.NetClient(online=lambda: False, binary_transport=binary,
                           cache=cache)
    off = client.get_bytes("https://api.rainviewer.com/x.png", "T")
    assert not off.ok and calls == [], "the switch is checked before anything"

    client.online = lambda: True
    first = client.get_bytes("https://api.rainviewer.com/x.png", "T", ttl=60)
    assert first.ok and first.data == payload and len(calls) == 1
    second = client.get_bytes("https://api.rainviewer.com/x.png", "T", ttl=60)
    assert second.data == payload and second.from_cache
    assert len(calls) == 1, "a cache hit must not contact the host"


def test_a_refused_host_is_refused_for_bytes_too(tmp_path):
    client = net.NetClient(online=lambda: True,
                           cache=net.DiskCache(tmp_path / "c"))
    refused = client.get_bytes("https://example.com/tile.png", "T")
    assert not refused.ok and "example.com" in refused.error


# ── R5: a photograph, and never a logo ──────────────────────────────────

def test_logos_and_diagrams_are_not_photographs():
    for name in ("File:Frankfurt Airport Logo 2019.svg",
                 "File:Airport diagram KSEA.jpg",
                 "File:Flag of Germany.jpg",
                 "File:Location map Germany.jpg"):
        assert photos.NOT_A_PHOTO.search(name), name
    assert not photos.NOT_A_PHOTO.search("File:GYD (2).jpg")


def test_a_search_that_found_nothing_relevant_is_rejected():
    """Wikipedia always returns *something*. Presenting a list of Beverly
    Hillbillies episodes as an airstrip is worse than showing nothing."""
    assert photos.plausible_article("Heydar Aliyev International Airport",
                                    "Heydar Aliyev International Airport")
    assert photos.plausible_article("Magdeburg", "Magdeburg City Airport")
    assert not photos.plausible_article(
        "List of The Beverly Hillbillies episodes", "Nowhere Airstrip 42")


def test_the_picker_prefers_the_airport_over_what_is_next_to_it():
    """Seattle-Tacoma's article carries a photograph of a light-rail train;
    alphabetically it comes first, and it is not the airport."""
    article = "Seattle-Tacoma International Airport"
    train = "File:Airport-bound Link train at Westlake Station.jpg"
    airport = "File:Seattle-Tacoma International Airport aerial.jpg"
    assert photos.score_photo(airport, article) > photos.score_photo(train, article)


def test_a_photo_always_carries_a_credit():
    assert photos.Photo(url="u", author="A. Photographer",
                        licence="CC BY-SA 4.0").credit() == \
        "A. Photographer · CC BY-SA 4.0"
    assert photos.Photo(url="u").credit() == "credit not published"


# ── R7: the clock strip belongs to the operator ─────────────────────────

def test_the_clock_strip_round_trips_through_settings(con):
    assert store.world_clock_zones(con) == [], "empty means the built-in strip"
    chosen = [("ZULU", "UTC"), ("GYD", "Asia/Baku"), ("NRT", "Asia/Tokyo")]
    store.set_world_clock_zones(con, chosen)
    assert store.world_clock_zones(con) == chosen


def test_a_corrupt_clock_preference_costs_the_list_not_the_screen(con):
    store.set_setting(con, "world_clock_zones", "{not json at all")
    assert store.world_clock_zones(con) == []


def test_a_city_search_resolves_offline_to_an_iana_zone():
    from aivionics.ui.zoneeditor import default_label, search_zones
    hits = dict(search_zones("Baku"))
    assert "Asia/Baku" in hits
    assert any("GYD" in description for description in hits.values())
    assert "Asia/Tokyo" in dict(search_zones("Asia/Tokyo"))
    assert search_zones("x") == [], "one character is not a search"
    assert default_label("America/Los_Angeles") == "LOS ANGE"


# ── R8: a way back in, on a machine with no mail server ─────────────────

def _account(con) -> auth.User:
    user = auth.unclaimed_setup_user(con)
    assert user is not None
    return auth.change_password(con, user, "a-real-password-1")


def test_a_recovery_code_is_issued_once_and_stored_hashed(con):
    user = _account(con)
    code = auth.issue_recovery_code(con, user)
    assert len(auth.normalise_recovery(code)) == 20
    stored = con.execute("SELECT recovery_hash FROM app_user WHERE id=?",
                         (user.id,)).fetchone()[0]
    assert stored and auth.normalise_recovery(code).encode() not in bytes(stored)


def test_the_reset_prompt_cannot_be_used_to_enumerate_accounts(con):
    """A wrong code, an unknown user and an account with no code on file must
    be indistinguishable, or this becomes a way to discover usernames."""
    user = _account(con)
    auth.issue_recovery_code(con, user)
    messages = set()
    for username, code in (("admin", "AAAAA-BBBBB-CCCCC-DDDDD"),
                           ("nobody", "AAAAA-BBBBB-CCCCC-DDDDD")):
        auth.reset_throttle()
        with pytest.raises(ValueError) as caught:
            auth.reset_with_recovery(con, username, code, "another-pass-99")
        messages.add(str(caught.value))
    assert len(messages) == 1, messages


def test_a_recovery_code_is_spent_when_it_is_used(con):
    user = _account(con)
    code = auth.issue_recovery_code(con, user)
    _, replacement = auth.reset_with_recovery(con, "admin", code,
                                              "brand-new-pass-77")
    assert replacement != code
    assert auth.authenticate(con, "admin", "brand-new-pass-77").ok
    auth.reset_throttle()
    with pytest.raises(ValueError):
        auth.reset_with_recovery(con, "admin", code, "third-password-33")


def test_a_code_is_accepted_however_a_human_types_it(con):
    user = _account(con)
    code = auth.issue_recovery_code(con, user)
    for spelling in (code.lower(), code.replace("-", " "), code.replace("-", "")):
        assert auth.normalise_recovery(spelling) == auth.normalise_recovery(code)


def test_a_reset_refuses_a_weak_password_before_spending_the_code(con):
    user = _account(con)
    code = auth.issue_recovery_code(con, user)
    with pytest.raises(ValueError, match="10 characters"):
        auth.reset_with_recovery(con, "admin", code, "short")
    auth.reset_throttle()
    replaced, _ = auth.reset_with_recovery(con, "admin", code, "still-works-123")
    assert replaced.username == "admin", "the code survived the refused attempt"


def test_every_credential_event_reaches_the_audit_chain(con):
    from aivionics import audit
    user = _account(con)
    code = auth.issue_recovery_code(con, user)
    auth.reset_with_recovery(con, "admin", code, "audited-password-1")
    actions = [row[0] for row in con.execute("SELECT action FROM audit_log")]
    for expected in ("password_change", "recovery_issued", "password_reset"):
        assert expected in actions, expected
    ok, _rows = audit.verify_chain(con)
    assert ok, "the chain must still verify after a reset"


# ── R9: the mark cannot rely on something Qt does not implement ─────────

def test_no_shipped_icon_uses_clip_path():
    """Qt's SVG renderer is SVG Tiny 1.2 and ignores `clipPath`. The previous
    mark used one, so the application drew its own logo with the sky as a
    rectangle overhanging the dial. Nothing may depend on it again."""
    from aivionics import config
    icons = sorted((config.ASSETS_DIR / "icons").glob("*.svg"))
    assert icons, "no icons found"
    for path in icons:
        assert 'clip-path="' not in path.read_text(encoding="utf-8"), path.name


def test_every_icon_renders(qt_app):
    from PySide6.QtSvg import QSvgRenderer

    from aivionics import config
    for path in sorted((config.ASSETS_DIR / "icons").glob("*.svg")):
        assert QSvgRenderer(str(path)).isValid(), path.name


# ── an empty state is nothing but text, so it may never be cropped ──────

def test_an_empty_state_never_crops_its_own_text(qt_app):
    """A latent bug, found when a taller card above it squeezed the feed.

    `EmptyState` computed its text height from `fontMetrics()` at construction
    - before the stylesheet is applied, so from the wrong font. The number came
    out about a quarter short, the headline overlapped the body, and the last
    line disappeared. The height is measured after polish now.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QVBoxLayout, QWidget

    from aivionics.ui import fonts
    from aivionics.ui.widgets import EmptyState

    qt_app.setStyleSheet(fonts.qss("light"))
    host = QWidget()
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    state = EmptyState(
        "mdi6.clipboard-text-clock-outline", "No compliance data imported",
        "Checkups, MEL deferrals and AD/SB rows arrive from a CAMO export "
        "(Phase 4B.2). Until then this feed stays empty rather than showing "
        "a clock this application cannot vouch for.", theme="light")
    lay.addWidget(state)
    host.resize(1180, 210)          # deliberately tight
    host.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    host.show()
    for _ in range(6):
        qt_app.processEvents()

    needed = state.detail.heightForWidth(state.detail.width())
    assert state.detail.height() >= needed, (
        f"the last line is cropped: {state.detail.height()} px for {needed}")
    assert state.headline.geometry().bottom() < state.detail.geometry().top(),         "the headline is printing on top of the body text"
    host.close()


# ── arrivals and departures both have to arrive ─────────────────────────

def test_both_directions_survive_the_rate_limiter(tmp_path):
    """Found by actually asking the question the owner asked.

    `movements` fetches arrivals and then departures against the same source.
    The limiter puts an interval between calls to one source and *refuses*
    rather than waits - so departures came back as a rate-limit error every
    time an airport was opened, which reads on screen as "no departures".
    """
    from aivionics.ui.opsservice import OpsService

    calls, slept = [], []

    def transport(url, timeout):
        calls.append(url)
        return 200, json.dumps([{
            "icao24": "3c6444", "callsign": "DLH8AB",
            "estDepartureAirport": "LFPG", "estArrivalAirport": "EDDF",
            "firstSeen": 1755800000, "lastSeen": 1755806000,
            "departureAirportCandidatesCount": 1,
            "arrivalAirportCandidatesCount": 1}])

    # Fake time, so the wait is asserted rather than actually endured: the
    # clock only moves when the code under test decides to sleep.
    now = [0.0]

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    service = OpsService(tmp_path / "e.db", online=lambda: True)
    service.client.transport = transport
    service.client.cache = net.DiskCache(tmp_path / "cache")
    service.client.limiter = net.RateLimiter(clock=lambda: now[0])
    service.client.sleep = sleep
    # a real interval, so the second call genuinely has to wait it out
    net.SOURCE_MIN_INTERVAL.pop(adsb.SOURCE, None)

    arrivals, departures = service.movements("EDDF")
    assert arrivals.ok, arrivals.fetch.error
    assert departures.ok, departures.fetch.error
    assert len(calls) == 2, "both directions must actually be requested"
    assert slept and slept[0] > 0, "the wait is what makes the second one work"
    assert slept[0] <= adsb.SOURCE_WAIT_CEILING, "and it is bounded"


def test_an_empty_movement_list_does_not_claim_the_airport_was_quiet():
    """EDDF returned one arrival for a twelve-hour window in live testing.
    The panel may not present that as what happened at Frankfurt."""
    assert "not a quiet airport" in adsb.MOVEMENTS_WARNING
    assert "anonymous free tier" in adsb.MOVEMENTS_WARNING
