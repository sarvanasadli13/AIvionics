# Asset register and licences

Every asset shipped with the application is listed here with its source and licence.
**Nothing enters `assets/` without a row in this file.** Verified 2026-08-07.

---

## Bundled data — `assets/data/`

| File | Rows / size | Source | Licence |
|---|---|---|---|
| `airports.csv` | 85,836 · 12.7 MB | [davidmegginson/ourairports-data](https://github.com/davidmegginson/ourairports-data) | **Public domain** |
| `runways.csv` | 4.0 MB | same | **Public domain** |
| `airport-frequencies.csv` | 1.3 MB | same | **Public domain** |
| `navaids.csv` | 1.5 MB | same | **Public domain** |
| `countries.csv` | 24.6 KB | same | **Public domain** |

Refreshed nightly upstream. Re-pull before each release and record the pull date in the
build manifest. Fields: `ident, type, name, latitude_deg, longitude_deg, elevation_ft,
continent, iso_country, iso_region, municipality, scheduled_service, icao_code, iata_code,
gps_code, local_code, home_link, wikipedia_link, keywords`.

> **⚠ Correction, 2026-08-07.** An earlier design note recorded that this dataset "carries the
> IANA tz field". **It does not** — there is no timezone column. See *Timezones* below.

## Timezones — resolved, not bundled

`airports.csv` has no timezone column. Two routes were compared:

| Route | Coverage | Licence | Verdict |
|---|---|---|---|
| **`timezonefinder` 8.2.5** — derive IANA zone from lat/lon offline | **all 85,836** | **MIT** | **Selected** |
| OpenFlights `airports.dat` — has a `tz` column | 7,698 only | ODbL (attribution + share-alike) | Rejected: 9% coverage, stale, and a second dataset to keep in sync |

Requires Python ≥3.11. Store the resolved IANA name (never a UTC offset) per
standing rule in Phase 4B.5, and let `zoneinfo` + `tzdata` handle DST.

## Fetched at runtime, never bundled — added 2026-08-22 (BACKLOG round 2)

Rule 11 governs what ships in `assets/`. Neither of the two sources below puts a byte
in this repository: both are requested when a screen needs them, cached in
`data/ops-cache/`, and rendered **with their attribution on screen**. They are recorded
here anyway, because "we did not ship it" is not an answer to "under what terms are you
displaying it".

| Source | Used for | Hosts | Licence and obligation |
|---|---|---|---|
| **adsb.lol** | Live aircraft positions: the fleet map and the traffic layer (`ops/adsblol.py`) | `api.adsb.lol` | **ODbL 1.0** — attribution and share-alike, **commercial use permitted**. The credit renders on the map. Chosen over adsb.fi, which is *personal, non-commercial only*, and over OpenSky, whose anonymous feed publishes no registration. |
| **RainViewer** | The precipitation-radar layer on the Ops map (`ops/radar.py`) | `api.rainviewer.com`, `tilecache.rainviewer.com` | Public API, free for **non-commercial** use, attribution required. The credit is painted onto the map itself, not hidden in a menu. |
| **Wikipedia / Wikimedia Commons** | One photograph per airport (`ops/photos.py`) | `en.wikipedia.org`, `upload.wikimedia.org` | Per-file: mostly **CC BY-SA 4.0**, **CC BY 4.0** or public domain. The author and the licence name are fetched with the image and rendered directly under it. |

> **⚠ Open question, and it is not a technical one.** RainViewer's free tier is
> **non-commercial**. That is fine for the machine this was built on and it is **not**
> a decision that has been made for a product sold to an airline. Before any commercial
> use, either take a RainViewer plan or move the layer to a public-domain source — the
> US NOAA/NWS MRMS composite is public domain but covers the United States only.
> Flagged here rather than discovered by a customer.

**Photographs are filtered, not taken blind.** The obvious API returns Frankfurt's
*logo*, and a logo is a trademark rather than a licensed photograph — so
`photos.NOT_A_PHOTO` refuses logos, wordmarks, flags, maps and diagrams, and the picker
prefers files that are actually of the airport. This keeps the ban on operator logos in
force even though nothing is bundled.

## Country flags — `assets/flags/`

| Asset | Count | Source | Licence |
|---|---|---|---|
| 4:3 SVG flags, ISO 3166-1 alpha-2 filenames | **271** | [lipis/flag-icons](https://github.com/lipis/flag-icons) | **MIT** (`flags/LICENSE` retained) |

## Manufacturer logos — removed

`assets/logos/` held six manufacturers' marks (Boeing, Airbus, Embraer,
Bombardier, Gulfstream) plus an authored ATR placeholder. **They were removed
before this repository was published, and purged from its history.**

They were justified here as public-domain-by-threshold-of-originality on the
grounds that this was "a private, non-distributed project". Publication ends
that assumption: a trademark stays a trademark whatever the copyright status of
the file, and nothing in the application rendered them in any case.

## Aircraft photographs — `assets/photos/`

12 types, from curated Wikimedia Commons categories. **All CC BY / CC BY-SA** — per-image author and
licence in `_manifest.json`; attribution is required if these are ever published.

| Slug | Type | Aircraft in shot | Licence |
|---|---|---|---|
| `b737-8max` | 737-8 MAX | Southwest N8942L *(Commons Quality Image)* | CC BY-SA 4.0 |
| `b737-800` | 737-800 | Southwest N8676A *(QI)* | CC BY-SA 4.0 |
| `a320` | A320neo | F-WNEO | CC BY-SA 4.0 |
| `a321neo` | A321neo | United N14523 | CC BY 4.0 |
| `a350` | A350-900 | SAS SE-RSC | CC BY 4.0 |
| `a330` | A330-300 | Swiss HB-JHF | CC BY-SA 4.0 |
| `b787` | 787-8 | United N27908 *(QI)* | CC BY-SA 4.0 |
| `b777` | 777-300ER | Qatar A7-BES *(QI)* | CC BY-SA 4.0 |
| `e175` | E175 | Air Canada Express C-FEKS | CC BY 4.0 |
| `e190` | E190 | KLM Cityhopper *(QI)* | CC BY-SA 4.0 |
| `atr72` | ATR 72 | G-LMTH | CC BY-SA 4.0 |
| `crj200` | CRJ-200 | United Express N982SW | CC BY 4.0 |

**Selection method — worth keeping, it took three passes.** Free-text Commons search is useless here:
it returned a *meal tray* for the 737-800, a *door* for the CRJ200, a *cockpit display* for the ATR,
and DLT's logo for Embraer. Curated categories helped but still failed, because *"X in flight"*
categories contain photos taken **from** aircraft as well as **of** them — the picks were a cabin
panorama for the 787 and a plain cloudscape for the E190. What finally worked:

1. **Hard requirement: the aircraft type token must appear in the filename** (`737-8`, `A350`, `CRJ-200`…).
   This is the discriminator — every bad result lacked one, every good result had one.
2. Reject an explicit blocklist (cockpit, cabin, engine, door, panorama, "above the clouds"…).
3. **Aspect ratio 1.25–2.10** — excludes panoramas and portraits.
4. Prefer Commons **Featured / Quality / Valued** assessments.
5. Bonus for a **registration token** in the filename (`N8942L`, `HB-JHF`) — spotter photos have one.
6. Width 1600–9000 px, mild size preference **capped**, because "largest file" selects panoramas.

Scripts: `fetch_assets4.py` (final) and `probe_cats.py` in the session scratchpad.
**Every pick was verified by rendering it, not by trusting the filename.**

## UI icons — not bundled

Supplied at runtime by **`qtawesome`** (MIT) — Font Awesome, Material Design Icons, Codicons,
Phosphor, Remix. ~7,000 glyphs, recoloured to the theme palette at load. No icon files in the repo.

## Authored for this project — `assets/aircraft/`, `assets/icons/`

No third-party licence. Free to modify.

| File | Purpose |
|---|---|
| `aircraft/marker-plane.svg` | Fleet-map marker. Nose at 0°, rotate about (16,16) by ADS-B true track |
| `aircraft/marker-plane-small.svg` | Chevron for zoomed-out views. Rotate about (8,8) |
| `icons/mark-light.svg` · `icons/mark-dark.svg` | **AIvionics app mark** — square, for the taskbar, title bar and installer |
| `icons/logo-light.svg` · `icons/logo-dark.svg` | **AIvionics horizontal lockup** — mark + wordmark, 198×64, wordmark converted to outlines so it cannot re-flow without Segoe UI Variable Display / Georgia |
| `icons/chevron-down-light.svg` · `icons/chevron-down-dark.svg` | Combo-box disclosure arrow, stroked in `--txt2` per theme. Qt draws no native arrow on a stylesheet-styled `QComboBox`, so the QSS references these by path |

### The mark — final, chosen 2026-08-08

Rounded tile · circular dial · sky above · manual index tabs · a diagnostic trace crossing it.
Rebuilt as clean vector from the owner's reference image, with three corrections: the trace is an
**avionics signal** (flat, one deflection) rather than an ECG pulse; the left bars are set as
**decreasing index tabs** so they read as a manual index rather than noise; and **the name was removed
from the tile** — at 32 px it is a smear, at 16 px it is noise, and Windows prints the app name beside
the icon anyway.

- **Identical geometry in both variants; only the tile and the neutral invert.** The blues never
  change: sky gradient `#8FCBEC → #CFE7F6`, trace `#2E9BE0`. That is what makes them one logo, not two.
- Light tile `#FFFFFF` with `#0F2C42` linework · dark tile `#0F1C29` with a `#7FB6DC` edge.
- In the app the title-bar mark is inline SVG using `var(--s1)` and `var(--txt)`, so it follows the
  theme toggle with no second file and no swap logic.
- **Known limit:** below ~24 px the tile, dial and tabs crowd each other. If the taskbar icon looks
  muddy, ship a simplified 16/20 px variant in the `.ico` — dial and trace only, no tile, no tabs.
  Different artwork per size is normal Windows practice.

### The wordmark

**`AI` in the accent, and the capital `I` set in a serif face.** Both are load-bearing:

- In Segoe UI a capital `I` and a lowercase `l` are the *same glyph*, so "AIvionics" reads as
  "Alvionics" and the AI — the whole point of the name — disappears. The owner's reference image had
  exactly this defect.
- Colour alone does not fix it: the moment the logo is printed in one colour, engraved on a plate or
  faxed, the accent is gone and the ambiguity returns. **The serif crossbars are permanent.**
- Light: `#0E74BC` on `#0F2C42`. Dark: `#5BB4F0` on `#FFFFFF`.
- **The lockups use live text.** Convert to outlines before any final export, or they re-flow on a
  machine without Segoe UI Variable or Georgia.

Contact sheet: `docs/mockups/logo-sheet.html`.
*Superseded and removed 2026-08-08: `app-mark.svg` (ATA index tabs + fault trace) and the
attitude-indicator concept.*

---

## Not included

| Asset | Why |
|---|---|
| **Airline / operator logos** | No complete permissively-licensed set exists. Operator identity is carried by the photo and by the tail number instead |
| **Aircraft type silhouettes** | Not available under a clean licence, and hand-drawn ones look amateur. The fleet register uses a type label plus the photo above |
| **Map basemap tiles** | Deferred to Phase 4B.4. OSM raster tiles carry a usage policy; Natural Earth (public domain) is the offline candidate. Decide with the map widget, not before |

## Rule

Adding an asset requires: source URL, licence identifier, licence text retained in-tree where the
licence demands it, and a row above. **If the licence cannot be established, the asset does not ship.**

*Project scope: private, not distributed, not for sale. Logos and photographs are used on that basis.*
