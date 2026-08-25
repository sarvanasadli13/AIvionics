# AIvionics

**A reliability-analysis and manual-retrieval tool for an avionics engineering
department.** It indexes *into* controlled maintenance manuals; it does not
reproduce them. It answers one question:

> *"What has been attempted for this symptom before, and how did it turn out?"*

Decision support. Not part of the maintenance record, and not an autonomous
maintenance authority.

---

## Status — read this before the screenshots

This is an honest project, so the limitations come first.

| | |
|---|---|
| Retrieval quality gate (Gate 2) | **partly failed** — recall@50 is 0.333 against the 0.80 required |
| The cross-encoder reranker | **makes ranking worse** and is not shipped enabled |
| Confident-and-wrong rate | **0.922** — no engineer should see a ranked result until abstention is calibrated |
| Gold-set adjudication | **0 of 400 pairs done** — every retrieval number is agreement with a regex, not with an engineer |
| Confirmed maintenance outcomes | **0** — there is no learned root-cause capability and none is claimed |

What *did* pass: hybrid retrieval beats a stratified frequency baseline by
**5–6×** on NDCG@5, so the premise holds. And stage-1 recall is **0.784**
relaxed — the candidate generator usually finds the right task and the *ranker*
loses it. That is where the remaining headroom is.

**Structurally missing, and it caps what this can ever do:** no wiring manuals
(WDM/SWPM), so every retrieval path terminates at an LRU-level task — that is,
at a removal. No FIM/TSM procedure text. No shop findings, so true no-fault-found
does not exist in this data, only a repeat-defect proxy. And FAA SDR is a
*reportable-occurrence* sample, which systematically excludes the low-drama
removals where NFF concentrates.

---

## Screenshots

All rendered from the demo database — real aircraft, real defect history, no
mock data. Regenerate with `python scripts/github_screenshots.py`, which refuses to
shoot a rendered manual page.

| | |
|---|---|
| **Diagnose** — symptom to ranked task locators | **Reliability** — removal→repeat rates by ATA chapter |
| ![Diagnose](docs/screenshots/03-diagnose.png) | ![Reliability](docs/screenshots/06-reliability.png) |
| **Manuals** — ATA tree, revision control, in-app PDF | **Fleet** — the register, with per-tail defect counts |
| ![Manuals](docs/screenshots/04-manuals.png) | ![Fleet](docs/screenshots/05-fleet.png) |
| **Ops** — pan/zoom map, live ADS-B, radar overlay | **Home**, dark |
| ![Ops](docs/screenshots/08-ops-map.png) | ![Home dark](docs/screenshots/11-home-dark.png) |

![Home](docs/screenshots/02-home.png)

## What it does today

- **Diagnose** — free-text symptom → ranked task *locators* from the AMM and the
  FIM catalogue, with prior cases from 1.75 M service difficulty reports
- **Manuals** — ATA tree, revision control, in-app PDF viewer, coverage per chapter
- **Fleet / Reliability / Compliance** — per-tail defect history, repeat-defect
  proxy statistics, imported compliance clocks with provenance
- **Ops** — pan-and-zoom fleet map with live ADS-B, precipitation radar overlay,
  airport page with runways, frequencies, METAR/TAF and photographs
- **Notes** — anchored to a tail, defect, task or case; never free-floating
- **Login, two roles, hash-chained audit log**

Runs on a 16 GB office PC, installs per user, and **works with the network
unplugged** — the manuals core, retrieval, case base and statistics are all local.

---

## The rules it holds itself to

These are enforced in code and in tests, not just documented.

1. **Never render or print a task body outside the app.** Printing emits a
   locator only — task number, title, manual, revision, effectivity, tail,
   timestamp, user — and sends the engineer to the controlled source.
2. **Every numeral comes from the database**, never from generation. When there
   is no data the screen says so rather than showing a zero.
3. **Fail closed on effectivity.** Unresolved applicability renders
   *"applicability unresolved — verify in controlled data"*.
4. **Aggregate-only statistics.** No individual engineer attribution anywhere
   (BetrVG §87(1)(6), and because engineers who feel measured write vaguer
   narratives, which poisons the data).
5. **The LLM never touches procedural text.** Warnings and cautions render
   first, non-collapsible, outside any generated path.
6. **Online features are isolated behind one auditable setting**, with the
   outbound host list shown in Admin.

---

## Try it

```bash
pip install -e .
python -m aivionics.ui
```

The application ships with no corpus. To see it populated, build the demo
database — it registers twelve **real** aircraft tails that already carry
reported defects in the public FAA SDR data:

```bash
python scripts/make_demo_db.py
python -m aivionics.ui --db data/demo/aivionics-demo.db
```

Nothing in the demo dataset is fabricated. Registration details a fleet register
would normally hold — MSN, line number, hours, cycles — are left blank, because
SDR does not publish them and inventing them would undermine the point.

```bash
python -m pytest -q          # the test suite
python scripts/ui_preview.py # render every screen to docs/status/
```

---

## Data and licences

Every bundled asset has a row in [`assets/LICENSES.md`](assets/LICENSES.md).
Nothing ships without one.

The source is MIT ([`LICENSE`](LICENSE)). [`NOTICE.md`](NOTICE.md) states what
that does **not** grant: no right to aircraft maintenance data, and no
affiliation with any manufacturer named in the software.

| Source | Used for | Licence |
|---|---|---|
| [OurAirports](https://github.com/davidmegginson/ourairports-data) | 85,836 airports, runways, frequencies, navaids — bundled | Public domain |
| FAA SDR | 1.75 M service difficulty reports | US Government, public |
| [timezonefinder](https://github.com/jannikmi/timezonefinder) | IANA zone from lat/lon, offline | MIT |
| [flag-icons](https://github.com/lipis/flag-icons) | 271 country flags | MIT |
| [adsb.lol](https://www.adsb.lol/) | Live aircraft positions — fetched, not bundled | **ODbL 1.0**, attribution + share-alike |
| Wikimedia Commons | One photograph per airport — fetched, not bundled | Per file, credit rendered with the image |
| RainViewer | Precipitation radar tiles — fetched, not bundled | ⚠ **Non-commercial** — see below |

> **⚠ Open licensing question.** RainViewer's free tier is non-commercial. That
> is fine for a personal build and it is **not** cleared for a commercial
> product. Before any commercial use, either take a RainViewer plan or move the
> layer to the US NOAA/MRMS composite, which is public domain but covers the
> United States only.

**No OEM maintenance manuals are included.** Authorization to hold and use
AMM/FIM/WDM/CMM content varies by organization, and this repository ships none.

---

## Built with

PySide6 · SQLite + FTS5 + sqlite-vec · fastembed (BAAI/bge-small-en-v1.5) ·
PyMuPDF · bcrypt · numpy

Planning, evaluation protocol and the risk register are in
[`docs/PLAN.md`](docs/PLAN.md). The measured retrieval status is in
[`docs/status/gate2-report.md`](docs/status/gate2-report.md), including the
numbers that failed.
