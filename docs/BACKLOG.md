# Backlog — owner feedback

Two rounds so far. **Round 1** (9 Aug) came from installing the build;
**Round 2** (21 Aug, at the bottom) came from opening it against the real
corpus for the first time.

---

# Round 1 — owner feedback, 2026-08-09

Raised after installing the build and opening it. Recorded verbatim first,
then what it means in code, so the next session works from the owner's list
rather than re-deriving one.

**Status, 2026-08-21:** items 3, 4 and 5 are done. Item 1 is half done — the
blank first run now explains itself, but how the data gets there is still an
open decision, and there is a second finding under it worth reading. Item 2
stays open and is largely blocked on item 1.

---

## 1. It opens empty — PARTLY DONE

> *"It opens empty"*

The installer creates a fresh database in `%LOCALAPPDATA%\AIvionics\data`, so a
new install shows a login, a forced password change, and then blank screens.
The 1.75 M reports, 2,426 AMM tasks, 8,194 indexed locators and the fitted
calibration all sit in the project folder and the installed application never
sees them.

This is the biggest one. Technically correct, practically useless: the first
thing the owner saw was nothing.

**Done:** Home no longer opens on three em-dashes. When the corpus is empty it
leads with a notice naming the database it actually opened and the two ways to
change that — `AIVIONICS_DATA` / `--db`, or the Phase 1 and Phase 2 scripts.
`FirstRunNotice` in `src/aivionics/ui/pages/home.py`; it hides itself the
moment a corpus is present.

**Still open — how the data gets there.** The routes, with the sizes measured
rather than estimated:

- **Ship the database with the installer.** Simple, honest, and **2.55 GB**,
  not the ~1 GB this file first guessed. That is the whole objection to it.
  The current spec deliberately excludes any `.db`; that exclusion exists to
  stop a *live* database being packaged by accident, not to prevent shipping a
  prepared one, so a separately-named seed file sidesteps it.
- **First-run import.** The app offers to point at an existing `aivionics.db`
  or to run the ingest. Small installer, one extra step, and on this machine
  the database is already on disk — one click and it opens full.
- **Trimmed seed.** Ship a cut-down corpus so it opens with something in it,
  full ingest optional.

`AIVIONICS_DATA` and `--db` both work today, and the notice now says so, but
nobody should need an environment variable to see their own data.

**And none of the three routes fully closes this item.** Counted read-only out
of `data/aivionics.db` on 2026-08-21: **1,754,410 cases**, **8,194 task
locators** (2,426 AMM bodies + 5,768 FIM catalogue rows), 2 manuals — and
**0 aircraft**. Point the app at the real database and Diagnose, Manuals and
Reliability fill up, but Home's Fleet tile still reads *"No aircraft registered
yet"*, because the fleet register is genuinely empty. Where aircraft come from
is a separate open question (Phase 4B registers them; nothing has registered
any), and it is worth answering before the seed-versus-picker choice, because
it may change that choice.

## 2. It looks and feels unfinished — OPEN

> *"It looks/feels unfinished"*

Treat as a full pass over every screen **as a user, not as a developer** — open
it, use it, and fix what feels wrong, rather than checking each screen renders.

**Done so far**, both of them things that were wrong regardless of data:

- Pages now cross-fade over 130 ms instead of swapping in one frame
  (`MainWindow.fade_in`). The effect is detached again a tick after the fade
  ends — left attached, it costs an off-screen repaint of the whole page on
  every later update.
- The Ops rail item, when the online switch is off, was painted in `$hair`.
  On the rail's own gradient that is not "greyed out", it is *gone*. The item
  now has three visible states — accent, `$txt2`, `$txt3` — and a pixel test
  guards it.

**Still open.** The rest needs the app open on real data; empty states are
most of what is reachable right now. See item 1.

## 3. No minimise animation — DONE

> *"there is no minimizing animation"*

`Qt.FramelessWindowHint` strips `WS_CAPTION` and `WS_THICKFRAME` off the
native window, and with them go the things the *desktop* provides rather than
the application: the minimise and restore animation, Aero Snap, and the window
controls on the taskbar thumbnail.

`src/aivionics/ui/nativewindow.py` puts the frame styles back and then answers
`WM_NCCALCSIZE` with a client area covering the whole window, so the
re-enabled frame is never drawn. Applied to the shell, the login and the
adjudicator. Two details that are not optional and are handled: a maximised
window is deliberately sized larger than the work area on the assumption the
frame will absorb it, so the client rect is pulled back in by hand; and an
auto-hiding taskbar is given one pixel on its own edge, or it can never
re-appear over a maximised window.

Verified: all five style bits present on the live handle, client rect covering
the full window (no native title bar drawn), maximise landing exactly on the
1920×1020 work area, and minimise/restore reaching and leaving `IsIconic`.
The animation itself is a desktop effect — it has to be watched, not asserted.

## 4. The side rail needs names and a hide button — DONE

> *"Side panel also must show section names and it must have hide button"*

Expanded is now the default: 196 px, icon and section name, left-aligned. The
control at the top of the rail collapses it back to the 58 px icons-only mode,
with a 160 ms width animation, and the choice is remembered in the
`rail_expanded` setting. Collapsing drops the visible name but never the
tooltip or the accessible name.

Two things worth knowing before touching it again:

- The nav items are `QPushButton`, not `QToolButton`, because only
  `QPushButton` honours `text-align: left` in a stylesheet. A tool button
  centres icon+text as one block, which puts the icons of "Home" and
  "Reliability" in different places.
- They are styled as `#RailNav`, deliberately not `#RailBtn` — the PDF viewer
  reuses `#RailBtn` as its generic quiet icon button, and the two must not
  drift into each other.

`Rail` in `src/aivionics/ui/widgets.py`.

## 5. Question the online/offline switch — DONE

> *"Why it is online offline"*

**Owner decision, 2026-08-21:**

> *"if there is internet connection so it must show ONLINE if there is no
> internet connection it must show offline and if it is restored the status
> must again show ONLINE."*

That is none of the four options this file previously listed, and it is a
better answer than any of them. The badge said `OFFLINE` and meant *"outbound
calls are not permitted"* — a permission, set once in Admin. Read as a status,
which is how anybody reads that word, it was simply wrong: an application that
had never been unplugged still said OFFLINE, and pulling the cable out of the
wall changed nothing on screen.

**What it does now.** ONLINE means the application can reach the network *right
now*, which takes both halves:

| Admin switch | Route off the machine | Badge |
|---|---|---|
| on | yes | **ONLINE**, in the accent |
| on | no | OFFLINE — *"no route to the network"* |
| off | yes | OFFLINE — *"switched off in Admin"* |
| off | no | OFFLINE |

**The one case the instruction did not cover** is the third row, and it is the
default state of a fresh install. Badging ONLINE there would announce a
connection on a machine that is deliberately making none — so it reads
OFFLINE, and the tooltip carries which of the two reasons applies. *Say so if
you want the badge to track connectivity alone regardless of the switch;
it is one condition in `MainWindow.refresh_online_badge`.*

**How reachability is known — and what it deliberately does not do.**
`QNetworkInformation` (`src/aivionics/ui/connectivity.py`), which on Windows
wraps the OS Network List Manager. It reports what the machine already knows
and **sends nothing**. A polling probe would have been an outbound call made on
behalf of a status light, on a product whose whole audit story is two
allow-listed hosts and a switch that turns them off. Its `reachabilityChanged`
signal is what makes "restored" work with nothing polled.

`Local` and `Site` — a network found but no internet — count as OFFLINE, which
is exactly the case the badge exists to get right. If the backend cannot load,
reachability is assumed rather than denied: an unknown state is not evidence of
a disconnection, and badging OFFLINE on a guess is the failure being replaced.

**ONLINE is painted in the accent, not in green.** Green for ONLINE and red for
OFFLINE would say a disconnected machine is faulty; this application is
*designed* to run with the cable out, and that reading is what got the badge
questioned in the first place. The accent carries no status meaning (§4A.1: the
accent may never be red, amber or green).

**Left alone deliberately:** the Admin card — the switch, the host allow-list
and the session activity — is untouched, so standing rule 12's auditable
surface is exactly as it was. So is the rail's dimming of Ops, which still
follows the *permission*, not the connection. Say if it should follow the
connection too.

## 6. "and etc."

The list above is not complete. Expect more once the app is used against real
data — see item 1, since most of the feel of the thing is unreachable while it
opens with nothing in it.

---

*The engineering backlog (retrieval quality, corpus repair, gold-set
adjudication) is unchanged and lives in `docs/PLAN.md` and
`docs/status/gate2-report.md`. This file is only the owner's product feedback.*

---

# Round 2 — owner feedback, 2026-08-21

Raised after opening the app against the **real corpus** for the first time
(`data/aivionics.db`, not the installer's empty one). Verbatim first, then what
each one means in code.

**Status, 2026-08-22 — all nine built.** 363 tests pass (was 321 before round 1).

| | Item | State |
|---|---|---|
| R1 | Map pan and zoom | **Done** — cursor-anchored zoom, drag, keyboard, zoom-tiered airport backdrop |
| R2 | Live traffic, clickable, zoomable | **Done** — OpenSky area query bounded by the visible box |
| R3 | Weather radar | **Done** — RainViewer composite, reprojected onto the plate-carrée map |
| R4 | City, temperature, position | **Done** — city in the result row, *Show on the map*; temperature already existed |
| R5 | Retract the search, airport photo | **Done** — the search folds to one line; photo from Wikimedia with its credit |
| R6 | Open online and full screen | **Done** — `showMaximized()`, and `online_enabled` now defaults on |
| R7 | Editable world clock | **Done** — add/remove/reorder, offline city search, remembered |
| R8 | Forgot password | **Done** — one-time recovery code, since there is no mail server |
| R9 | The logo | **Done** — the old one was *broken*, not just ugly, and the replacement is settled: concept C, chosen by the owner 2026-08-24. See R9. |

**Two things were found while building this that were not on the list**, and both
were real defects rather than preferences:

1. **The logo has been rendering wrong all along.** Every icon drew its sky with
   `clip-path`, which Qt's SVG renderer does not implement — so the sky painted as
   a rectangle overhanging the dial. Four assets were affected. A test now refuses
   `clip-path` anywhere in `assets/icons/`.
2. **`EmptyState` cropped its own last line.** It sized its text from
   `fontMetrics()` at construction — before the stylesheet is applied, so from the
   wrong font — and came out about a quarter short. The headline printed over the
   body. Measured after polish now, with a test.

**One licence question is open and is not technical — see R3.**

Two pieces of context that explain several of these at once, and that the
engineering notes below refer back to:

- **He had the online switch off** (it is off by default), so every online
  panel was dark. Some of what he reports as missing is built and was simply
  unreachable. Where that is the case it is said so plainly, because "it
  already exists" and "he could not find it" are both true and only one of
  them is an excuse.
- **The fleet register is empty** — 0 rows in `aircraft`. Anything keyed to a
  tail has nothing to show.

---

## R1. The fleet map does not behave like a map — DONE

> *"Fleet map I cannot use it like google map I cannot zoom I cannot move."*

Correct, and by design until now. `src/aivionics/ui/mapview.py` paints an
equirectangular world with `QPainter` at a fixed extent — no `wheelEvent`, no
drag, no zoom state. Its own docstring says *"It is situational awareness, not
a traffic display."*

Two very different pieces of work sit behind this, and they should not be
confused:

- **Pan and zoom on the map we already have** — a scale factor and an offset
  threaded through the existing projection, plus wheel and drag handling. No
  new data, no network, no licence. This is the cheap 80%.
- **A basemap that looks like Google Maps** — coastlines, borders, place
  names — needs either a bundled vector dataset (Natural Earth is public
  domain and would keep the app offline) or raster tiles from a tile server,
  which is a **new allow-listed host plus a licence row** (standing rules 11
  and 12) and breaks the offline guarantee for that view.

The reference dots today are the 1,172 bundled large airports, which is what
currently traces the continents.

## R2. No live-traffic view with aircraft details and zoom — DONE

> *"In Ops page I did not see any page like live flightradar+its functionality
> to check aicraft information from the map+zoom."*

Partly built, entirely invisible to him, for two separate reasons:

- The Ops **map tab** already pulls live ADS-B from OpenSky (`ops/adsb.py`)
  and **clicking an aircraft already opens a panel** with its position, recent
  defects and compliance rows (`MapView.tail_selected` → `OpsPage._show_tail`).
- He saw none of it because **the online switch was off**, and even with it on
  the panel is keyed to tails in the fleet register, which has **0 rows**.

So the genuinely missing parts are: **zoom** (see R1), and a decision about
what a *non-fleet* aircraft should show when clicked. Today the map is a fleet
tool; FlightRadar24 shows everything in the sky. Those are different products,
and OpenSky's free tier is anonymous and credit-limited with incomplete
coverage by design — `adsb.COVERAGE_WARNING` already says so on screen. Worth
agreeing what "like flightradar" means before building toward it.

### Found while answering "can I check arrivals and departures?" (2026-08-22)

The panel existed and was **half broken**. `OpsService.movements` fetches
arrivals and then departures against the same source, and the rate limiter
puts an interval between calls to one source and *refuses* rather than waits —
so departures came back as a rate-limit error **every time an airport was
opened**, which reads on screen as "this airport has no departures". Fixed by
waiting out the interval on the worker thread, with a fake-clock test that
fails without the fix.

The second half is not fixable in code and is worth knowing before anyone
relies on this panel: **OpenSky's anonymous flights endpoint is thin.** EDDF —
well over a thousand movements a day — returned **one arrival** for a
twelve-hour window, and the API began returning HTTP 429 after a few dozen
test calls. The panel now says so instead of printing "1 recorded" and letting
the reader draw a conclusion about Frankfurt. **A registered OpenSky account
raises the allowance**; that means storing a credential, which is a decision
for the owner, not a change to make quietly.

## R3. No weather radar — DONE, with a licence question

> *"there is no weather radar in our software."*

True. `ops/weather.py` fetches **METAR and TAF text only** from
aviationweather.gov, decodes it and renders it as fields. There is no
precipitation imagery anywhere.

A radar layer is raster imagery over a map, so it needs all of: R1's zoomable
map, a **new allow-listed host** with a licence row (RainViewer is global;
NOAA nowCOAST/MRMS is US-only and public domain), and a tile cache. It is the
largest single item in this round.

## R4. Airport search should show temperature, city, and where it is — DONE

> *"When I am searching airport it must also indicate temprature in that
> airporta also city. It must also idicates where is that airport on world
> map."*

Three parts, in increasing order of work:

- **City — already there.** The identity card renders `Location` from
  `Airport.where()`, which is municipality + country. What is *not* there is
  the city in the **search results list**, which shows only the code line and
  the airport name (the city is in the tooltip). Moving it into the row is
  small.
- **Temperature — already there, and he could not see it.** The Weather card
  renders `Temperature` from the decoded METAR. It needs the online switch on
  and a successful fetch. This is an R6/visibility problem, not a missing
  feature.
- **Position on a world map — not there.** Needs the map to accept a "show
  this point" mode, which lands on R1.

## R5. Selecting an airport should reclaim the space, and show a photo — DONE

> *"When I searched by typing on search bar one airport and found it when I
> clicked it it must rectract the search bar for giving more space to indicate
> the airport itself.(It would be better to have also picture of each airport
> when I click on them. Do not need to manually download all the pictures of
> airports. Software has online connection mostly. So it can get picture from
> online)."*

- **Retracting the search** is pure layout — collapse the search panel once a
  result is chosen, with a way back. Small, and it is the kind of thing that
  makes the screen feel finished.
- **Airport photos** are a real feature with a licence question attached.
  Standing rule 11 governs *bundled* assets, and a fetched photo is not
  bundled — but attribution obligations still travel with the image.
  Wikimedia Commons is the obvious source (a REST API, one new allow-listed
  host, mostly CC-BY-SA, which means **the credit line has to render next to
  the picture**). Also needs a disk cache and an honest empty state, since
  plenty of small airfields have no photo at all.

## R6. Open online and full screen by default — DONE

> *"When log in happens and software opens let it open defaultly in online mode
> and full screen."*

- **Full screen** — one line: `showMaximized()` in place of the fixed
  1440×900. Worth confirming he means *maximised* (title bar and taskbar
  visible), which is almost certainly what he wants, rather than true
  borderless fullscreen.
- **Online by default is a policy reversal and should be recorded as one.**
  `online_enabled` currently defaults to `"0"`, and the Admin card says "Off by
  default. With this off the application makes no internet connection at all."
  Flipping the default means a fresh install starts calling out to
  aviationweather.gov and opensky-network.org without anyone opting in. That is
  the right call for **his** machine and a question for an airline IT install.
  Both can be true: default on for the developer build, and the installer asks
  on first run. **He has asked for it directly, so it happens — but it is a
  posture change, not a preference, and the Admin copy has to stop saying "off
  by default" when it no longer is.**

## R7. The world clock must be editable — DONE

> *"The world time section it must be edittable for every user. Basically Now I
> cannot delete or add some cities or countries time."*

True. `WORLD_CLOCK_ZONES` in `src/aivionics/ui/widgets.py` is a hardcoded list
of six: ZULU, BER, LHR, GYD, DXB, SEA. Needs a settings-backed list plus an
editor — add, remove, reorder, and a label per row.

Nothing new has to be fetched for this: `timezonefinder` and the bundled
OurAirports data already resolve any city or airport to an IANA zone offline,
so the picker can be a search box over data that is already on disk.

"for every user" is worth pinning down — per operator account, or one list for
the machine? The `settings` table is currently machine-wide.

## R8. The login page needs a forgot-password path — DONE

> *"Log in page must also have forgot password section."*

Nothing like it exists. The honest problem: this is a **local, single-machine
application** with bcrypt hashes and no mail server, so the usual reset link
has nowhere to go. Options, and this needs a decision rather than a guess:

- **Admin reset.** Another admin resets the password and the account comes back
  with `must_change_pw` set. Standard, auditable, and useless on a machine with
  exactly one admin — which is the current state.
- **Recovery code at account creation.** Shown once, written down, stored as a
  second hash. Works offline and for the only-admin case; it is a second
  credential to lose.
- **Security questions.** Weakest of the three; a real reduction in the value
  of the password.

Whatever is chosen, it goes through `aivionics.audit` like every other
credential event, and the "forgot password" link must not reveal whether a
username exists (`authenticate` already refuses to say which half was wrong).

## R9. The logo looks wrong — DONE, and it was broken

> *"Logo is wrong. Check the clipboard I screen shoted."* — then, on being
> asked: *"the porblem with logo is not text it is how it is looking"*

**It is the mark's visual design, not the wordmark.** His screenshot is kept as
`docs/status/owner-feedback-logo-2026-08-21.png`: the dark rounded tile with
the circular gauge and the ECG trace across it.

For the record, since it will otherwise be re-investigated: the *wordmark*
lockup in `assets/icons/logo-{light,dark}.svg` is correct — "AI" in the accent
with a serif capital I so the name does not read "Alvionics", "vionics" in the
text colour. That was fixed on 9 Aug and is not what he is objecting to.

### Settled 2026-08-24 — concept C, the ATA locator

Three marks were drawn on 22 August and shown to the owner on 24 August,
rendered **through Qt** at 176/64/32/16 px on both application grounds (a
browser preview would have shown him something Qt cannot draw, which is how
the broken mark shipped in the first place). He chose **C**.

| | | |
|---|---|---|
| A | attitude indicator | gradient sky over ground, horizon banked 14°, pitch bars. Was installed 22 Aug. |
| B | engraved horizon | same disc drawn flat, four-rung pitch ladder. |
| **C** | **ATA locator** | **a reticle sitting on one line of an index — a crosshair over an accent rule with two pale index rules. Chosen.** |

C draws what the product *does* — finding the one right task in an index —
rather than what the industry looks like, and its silhouette is the only one of
the three still unambiguous at 16 px.

**Installed:** `mark-{light,dark}.svg`, `mark-small-{light,dark}.svg`, both
lockups in `logo-{light,dark}.svg` (mark spliced in at cy=30, wordmark
untouched), and `packaging/aivionics.ico` rebuilt at all ten sizes. Previews
re-shot. 658 tests pass. The pre-change assets are in the session scratchpad
under `backup-before-C/`.

**The small form drops the reticle ticks, and that was measured rather than
assumed.** Rendered at 16/20/24 px both ways, the four ticks merge into the
ring below 24 px and the mark reads as a lozenge. What survives is ring + dot
+ bar, so that is all the small form draws. The index rules are 0.85 device
pixels at 16 px and are gone either way.

The 16 px constraint that drove this is unchanged: `scripts/make_icon.py`
renders the .ico at ten sizes from 16 up, and the small entries are rendered
directly rather than downsampled because a detailed mark turns to mud.

---

## What this round costs, roughly

| | Item | Size | Blocked on |
|---|---|---|---|
| Small | R4 city in the result row · R5 retract the search · R6 full screen · R6 online default | hours | — |
| Medium | R1 pan and zoom on the existing map · R7 editable world clock · R4 "show this airport" | days | R1 first |
| Large | R2 a real traffic view · R3 weather radar · R5 airport photos | weeks, and each adds a network dependency | R1, plus a host and a licence row each |
| Decision | R8 forgot-password mechanism ✅ · R9 what the mark should look like ✅ *(concept C, 2026-08-24)* | — | owner |

**R1 is the keystone.** R2, R3 and half of R4 all sit on top of a map that can
zoom and pan, so it should be built first and built properly.

