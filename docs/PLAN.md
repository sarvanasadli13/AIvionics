# AIvionics — Build Plan

*Product name fixed 2026-08-08: **AIvionics** — a portmanteau of AI and avionics. Set as
`AI` in accent + `vionics` in text colour wherever the name is rendered.*

**Version 1.7 · 2026-08-08 · Sarvan Asadli**

*Changes in 1.7 — gold-set qualification, and a retraction.* §5 claimed the adjudicator had
"licensed-engineer-equivalent domain knowledge". **Withdrawn** — a B.Sc. in Aeronautical Engineering
plus AMM/ICAO inspection work is not a Part-66 licence. §5 now states exactly which judgements that
level supports and which it does not. **"Unsure" added as a first-class verdict**, its rate reported,
those pairs excluded from scoring. **New task 0.7b — 50-pair professional overlap:** a licensed engineer
re-adjudicates a subset, giving a measured agreement rate with explicit ≥80 / 60–80 / <60 decision
thresholds. New risk-register row. §8 gains "a Part-66 engineer for two hours" as the
highest-value-per-hour item in the table.

*Changes in 1.6 — assets:* `assets/` created and populated — OurAirports (5 CSVs, 19.5 MB, public
domain, **85,836 airports**), 271 MIT country flags, authored map markers and app mark, and
`assets/LICENSES.md` as the binding register. New **standing rule 11** (no asset without a licence row).
**Correction:** OurAirports has **no timezone column** — an earlier note said it did. Timezones now come
from `timezonefinder` (MIT, offline, full coverage).

*Changes in 1.5 — Phase 4C, engineer notes:* anchored notes (tail/defect/task/case, never free-floating)
with optional due dates, `.ics` export and **one-action promotion to `defect_finding`** — the only
mechanism in the plan that captures *what was found*. In-app calendar grid and in-app alarms **cut**.
`note` table + two schema invariants added. +1 week → 26–31 weeks.

*Changes in 1.4 — UI stack (§4A):* GitHub survey of 13 candidate repos, licences read from source.
**BreezeStyleSheets (MIT) selected** over the 8,056-star PyQt-Fluent-Widgets, which is GPL-3.0 /
paid-commercial. Companions: qtawesome, superqt, pyqtgraph — all permissive and active. Visual rules
fixed: **accent may not be red/amber/green**, status never colour-only, two deliberate densities,
seven rail items with Ops holding the whole online surface.

*Changes in 1.3 — multi-type:* parser, ingester, effectivity and `manual_type` are now **per-OEM
pluggable** (§P1-M); schema gains `oem`, `doc_standard`, `parser_plugin`. Gate 1 gains a stub-second-OEM
test. Corpus scan of the collection: **maintenance manuals exist for the 737 MAX only** — everything
else is training or flight-ops material. Embraer/ATR MEL+MMEL adopted for Phase 4B.1; A320/A340/787
training adopted for terminology normalisation only. "AMM for a second type" added to §8.

*Changes in 1.2 — prior-art scan (§11):* the retrieval representation changes from multi-vector
body embedding to **ATA-hierarchy + task-title embedding**, following Jo (2025), which dissolves the
61.8% truncation problem. **FIM retrieval partially unblocks** — the 3,674 IFIM filenames are a task
catalogue, and titles are all the representation needs. LLM-rerank added as the challenger to the
cross-encoder. Boeing/FAA SDR labelled data adopted in Phase 0. Fifth evaluation baseline added.

*Changes in 1.1 — owner decision:* live ADS-B tracking, the airport page and full compliance
alerting (checkups / MEL / AD-SB) are **restored into v1** as **Phase 4B**. Standing rule 2 rewritten;
new standing rule 11 (online isolation). Total estimate 20–25 → 25–30 weeks.

Written after a six-model adversarial review (ChatGPT, Grok, DeepSeek V4 Pro, Opus 5 MAX,
Fable 5 MAX, Kimi K3) and direct measurement against the actual data. Every number in this
document was measured, not estimated, unless explicitly marked as an estimate.

---

## 0. What this is — and what it is not

### 0.1 The product

> A **reliability-analysis and manual-retrieval tool** for an avionics engineering department.
> It **indexes into** controlled maintenance manuals; it does not reproduce them.
> It answers *"what has been attempted for this symptom before, and how did it turn out?"*

### 0.2 What it explicitly is NOT

| Not this | Why |
|---|---|
| "The AI tells you the correct task" | The available labels record what engineers *did*, not what was *correct*. Six independent reviews killed this claim. |
| A line tool for AOG decisions | Under AOG the swap is the fastest legally defensible act. No statistic beats that incentive. |
| A source of maintenance data | Part-145 145.A.45 requires data in use to be current and from the approved source. A local extraction is uncontrolled by definition. |
| **The** system of record for compliance | The CAMO remains the legal record for checkups, MEL deferrals and AD/SB. This tool tracks and alerts on an **imported mirror** — every row stamped with source system and import time (see §2 rule 2 and §3 Phase 4B). |
| Part of the official maintenance record | It is decision support. This must be stated in the UI. |

### 0.3 Primary user

**The reliability engineer**, not the line engineer. Value lives in repeat-defect analysis,
deferred-defect review, hangar-check preparation and shift handover — where time pressure is
low enough for evidence to change a decision.

---

## 1. Evidence inventory — what is proven, assumed, and missing

### 1.1 PROVEN (measured this session)

| Fact | Value | How verified |
|---|---|---|
| FAA SDR is public, CSV, 1995–2026 | 32 year-files | Downloaded 2000/2010/2020/2025 |
| SDR-2025 size | 67,617 reports, 76 columns | Parsed |
| `JASCCode` populated (ATA-aligned) | 100% | Parsed |
| `Discrepancy` narrative populated | 100% | Parsed |
| Narratives citing a manual | 61.0% (41,279) | Regex |
| Reports with tail + parseable date | 99.6% | Parsed — enables repeat linkage |
| **Leak-free queries after narrative split** | **26,296** (58.8% of cited) | Split test |
| Unsplittable (citation before any action verb) | 3,391 | Split test |
| Avionics-chapter reports per year | ~890–1,240 | Parsed 4 years |
| Reports on currently-flying types | 29% (2000) → 56–58% (2020–25) | Parsed |
| 737 MAX AMM chapters present | 16 | Listed |
| …readable | **10** (4,001 pages) | PyMuPDF |
| …**task bodies extracted** | **851** | `TASK`…`END OF TASK` pairing |
| Task word count | median 462, p75 1,012, p90 1,918, max 16,268 | Measured |
| **Tasks exceeding ~512 tokens** | **61.8%** | Measured |
| EFFECTIVITY present in task bodies | 83.6% | Measured |
| WARNING / CAUTION present | 34.3% / 45.9% | Measured |
| Function-code skew (action : diagnostic) | **7.5 : 1** (24.8% vs 3.3%) | Measured |
| ATA refs carrying a manual type | 91.9% (only 8.1% bare) | Measured |
| IFIM task files (names readable) | 3,674 | Listed |
| IFIM task content | **DRM-encrypted — not available** | Verified, decryption declined |

### 1.2 ASSUMED (not yet verified — verify before relying on)

- That the 6 unreadable AMM chapters can be repaired.
- That an LLM splitter meaningfully beats the 58.8% regex split rate.
- That a cross-encoder reranker delivers the published 65–80% → 85–90% gain on *this* corpus.
- That extracted `References` sections form a usable task graph.
- That Ollama can be installed on the target office PCs.

### 1.3 MISSING — and the consequence of each

| Missing | Consequence |
|---|---|
| **WDM / SWPM** | Interconnect (wiring, connectors, bonding, moisture) is the dominant true root cause of avionics NFF. Without it **every retrieval path terminates at an LRU-level task — i.e. at a removal.** |
| **FIM / TSM procedure text** (IFIM encrypted) | Fault-code paths cannot *execute a decision tree*. **Partially recovered in v1.2:** the 3,674 IFIM filenames are a task catalogue, and Jo (2025) shows title + ATA hierarchy is sufficient for retrieval — so **fault-code queries can return FIM task locators**, which is the only output standing rule 1 permits anyway. What stays missing is the conditional branching *inside* a FIM task. |
| **CMM / shop teardown findings** | True NFF is a shop finding. Without it, the NFF metric does not exist — only a repeat-defect proxy. |
| **Operator's own tech log** | SDR is a *reportable-occurrence* sample (14 CFR 121.703/145.221) and systematically excludes the low-drama removals where NFF concentrates. **Statistics cannot be validated without operator data.** |
| STC ICAs, Service Letters, AOTs, ISBs | Retrofitted systems return OEM procedure with false confidence. |

---

## 2. Standing rules — non-negotiable, apply to every phase

1. **Never render or print a task body outside the app.** Print locators only: task number, title,
   manual, revision, effectivity, tail, timestamp, user. Then send the engineer to the controlled source.
2. **Compliance clocks are tracked and alerted on, but never presented as authoritative.**
   Owner decision, 2026-08-07 — this reverses the earlier "display-only" rule. Every checkup /
   MEL / AD-SB row must carry: **source system · import timestamp · staleness state**. If the
   last import is older than its configured freshness window, the whole module renders in a
   degraded state with the alerts greyed and a banner reading *"data imported <date> — verify
   against the CAMO."* No alert may ever render without its provenance line visible.
3. **The LLM never touches procedural text.** Task bodies render verbatim. Warnings and cautions
   render first, non-collapsible, outside the LLM path. The LLM may summarise *case narratives* only.
4. **Every numeral in the UI comes from the database.** Never from generation.
5. **No network share, ever.** SQLite locking is broken over SMB/NFS. One host, WAL, local disk.
6. **Aggregate-only statistics.** No individual engineer attribution in any view
   (BetrVG §87(1)(6) — a system suitable for performance monitoring triggers Betriebsrat involvement;
   and if engineers believe they are measured, narratives get vaguer and the data source is poisoned).
7. **Flag tool-assisted narratives at write time.** Once live, engineers will paste tool output into
   write-ups; without a flag, tomorrow's labels are the system's own echo. **Cannot be retrofitted.**
8. **Fail closed on effectivity.** Unresolved applicability shows *"applicability unresolved — verify
   in controlled data"*, never a clean answer.
9. **Every index carries a version stamp.** Changing the embedding model invalidates every stored
   vector and every prior measurement.
10. **`git init` on day one.** Commit before every refactor.
11. **No asset ships without a licence row.** Every bundled image, icon set or dataset must appear in
    `assets/LICENSES.md` with source URL and licence identifier, with licence text retained in-tree where
    required. **If the licence cannot be established, the asset does not ship.** No manufacturer or
    operator logos (trademarks), no aircraft photographs (all-rights-reserved or CC-BY with attribution
    obligations).
12. **Online features are isolated and optional.** Live tracking, weather and flight schedules sit in
    a separate module behind a single `online_enabled` setting, with their own network layer, their own
    cache and their own failure state. **The manuals core, retrieval, case base and statistics must run
    identically with the network unplugged.** No online call may block a UI thread or a core query.
    Outbound hosts are allow-listed and shown in Admin, so IT can audit exactly what the machine talks to.

---

## 3. Phase plan

Phases 0 and 1 run **in parallel** (independent inputs). **Phase 2 does not start until Gate 0 passes.**

```
   ┌── PHASE 0  SDR / labels ───┐
   │                            ├─ GATE 0 ─► PHASE 2 ─► GATE 2 ─► PHASE 3 ─► PHASE 4 ─► 4B ─► 4C ─► 5 ─► 6
   └── PHASE 1  AMM / catalogue ┘  GATE 1                                              GATE 4B
```

---

### PHASE 0 — Data truth *(the gate; nothing downstream is measurable without it)*

**Estimated effort: 3–4 weeks part-time.**

| # | Task | Output | Done when |
|---|---|---|---|
| 0.1 | Download all SDR years 1995–2026 | 32 CSVs in `data/sdr/` | 32 files, row counts logged |
| 0.2 | Normalise into one table; provenance columns (`source_year`, `ingested_at`) | `sdr_raw` table | Row count = sum of files |
| 0.3 | **Narrative splitter** — defect vs rectification. Regex baseline (58.8%) then LLM pass via NIM (`llama-3.3-70b` / `gpt-oss-120b`) | `defect_text`, `rectification_text` | Split rate measured on a 200-row hand-checked sample |
| 0.4 | Reference extractor — manual type + task number + **function code**; fix the false-positive problem (`001`, `101`, `201`, `404`, `911` are not Boeing function codes) | `label_silver` table | FP rate <5% on a 200-row sample |
| 0.5 | Outcome linkage — same tail + ATA + normalised symptom within 30/90 d | `repeat_link` table | Repeat rate plausible after normalisation (the naive tail×chapter count of 69,870/30d is inflated — must drop) |
| 0.6 | Confidence tiering — HIGH (root-cause-found, no repeat) / MED (cited, no outcome) / LOW (NFF language, replaced+NFF, repeat<30 d) | `confidence_tier` column | Tier distribution reported |
| 0.7 | **Gold set — 400 pairs hand-adjudicated** (§5 for who, and the limits of that). Stratified by ATA chapter, function code, narrative length, fault-code presence. Verdicts: **yes / no / partial / unsure** + what it should have been. **Adjudicate with the AMM open.** Build the adjudication tool first — one pair per screen, keyboard verdicts, auto-advance, progress saved, stratified queue (~1 day, halves the 10–15 h of manual effort) | `label_gold` table | 400 adjudicated, unsure-rate reported, held out permanently |
| 0.7b | **Professional overlap — 50 pairs re-adjudicated by a Part-66 licensed engineer** (Silk Way / AZAL, §8). Gives a measured adjudicator-vs-professional agreement rate | `label_gold_pro` + agreement % | 50 done; thresholds in §5 decide whether the gold set stands |
| 0.8 | Baselines — stratified top-20-task frequency per ATA chapter | baseline metrics | Recorded |
| 0.9 | **Adopt `Boeing/sdr-hazards-classification`** (MIT, FAA×Boeing, on PyPI). Contains SME-annotated SDR records and a text pipeline built for exactly these narratives — typos, part numbers, abbreviations. Use its preprocessing as the baseline normaliser; use its annotations as free evaluation data for the splitter | normaliser module + external annotation set | Their models reproduce on our SDR pull; normaliser measurably beats naive lowercasing on the 200-row sample |

**GATE 0 — pass/fail:**
- ✅ ≥15,000 leak-free query/label pairs after splitting
- ✅ Gold audit shows silver-label agreement ≥50%
- ✅ Function-code false-positive rate <5%
- ❌ **If silver labels are <50% correct, or <10,000 clean pairs survive → STOP.**
  Pivot to manual-retrieval-only; drop the case-base claim entirely.

---

### PHASE 1 — Corpus and catalogue *(parallel with Phase 0)*

**Estimated effort: 2–3 weeks part-time.**

| # | Task | Output | Done when |
|---|---|---|---|
| 1.1 | Repair the 6 unreadable AMM chapters (21, 24, 26, 27, 29, 32). Try in order: `qpdf --replace-input`, Ghostscript rewrite, `pypdf` re-save, then **VLM parsing — Jo (2025) measured 99.64% precision / 99.27% recall / 2.57% CER with Qwen 2.5-VL on 20 B737 manuals**, so this route is evidenced, not hopeful. `nvidia/nemotron-parse` via NIM is the same class | readable PDFs or documented failure | Each chapter readable or formally abandoned |
| 1.2 | Task extractor as a **per-OEM plugin**, not one regex (see §P1-M below). Boeing plugin first, with **all four known gotchas handled and unit-tested**:<br>① engine `-G00` 6th group ② TOC entries look identical to task headings (pair with `END OF TASK`) ③ whitespace inside task numbers (`34-41-1 1-020-002`) ④ DDG refs embedded in task titles | `task` table + `parsers/boeing.py` behind a common interface | Regression test per gotcha, all green; adding a second OEM requires **no change to core code** |
| 1.3 | **Coverage manifest** — extracted tasks vs each chapter's own TOC | `coverage` table | Per-chapter % recorded and surfaced in UI |
| 1.4 | Section splitter — title / references / zones / tooling / prerequisites / **warnings** / **cautions** / numbered steps | `task_section` table | Warnings+cautions never merged into step text |
| 1.5 | Cross-reference graph from `References` sections | `task_link` table | Link resolution rate measured |
| 1.6 | Effectivity extraction (`EFFECTIVITY`, `TBC ALL`, `AIRPLANES WITH/WITHOUT`, POST/PRE-SB) preserved **inside** the body, never stripped | `effectivity_raw` + parsed | Verified on tasks known to contain conditionals |
| 1.7 | **FIM task catalogue from the 3,674 IFIM filenames.** Parse task number + title from each filename, resolve the ATA hierarchy, store as `task` rows with `manual_type='FIM'`, `body = NULL`, `catalogue_only = true`. **No decryption, no DRM circumvention** — the filenames are already readable | 3,674 FIM catalogue rows | Numbers parse against the ATA tree; UI marks them locator-only and never implies a body exists |
| 1.8 | **Title-embedding text builder** — for every task, compose `chapter title → section title → subject title → task title` (Jo 2025's revision-robust representation). Store as `task.embed_text`, versioned | `embed_text` column | Populated for all AMM **and** FIM catalogue rows; stable across a revision bump (test with two revisions of one chapter) |

#### P1-M. Multi-type support — architecture now, corpus later

The case base is **already multi-type**: SDR covers the whole US fleet (2025 top types 737-800 variants,
A321neo, ERJ-170, A320; 2000 top types CRJ-200, EMB-120, Beech 1900D, Do-328, Saab 340, DC-9). Nothing
to build there. **Only the manual corpus is single-type**, and that is a data-access problem (§8),
not an engineering one. So: **build type-agnostic, ingest 737 MAX only in v1.**

The ATA chapter hierarchy is the shared taxonomy across every manufacturer — ATA 34 is Navigation on a
737, an A320, an E175 and an ATR 72 — which is precisely why the §2.3 title+hierarchy representation
carries across OEMs. **Evidenced, not assumed:** Jo (2025) measured VLM extraction on **20 A320 manuals
alongside 20 B737**, and Airbus parsed *better* — 99.39% precision / 99.82% recall / 1.14% CER vs
99.64 / 99.27 / 2.57.

What must be pluggable from day one:

| Dimension | Boeing | Airbus | Consequence |
|---|---|---|---|
| Task numbering | `34-11-01-400-801`, 6 groups on engine chapters | `34-11-00-000-801-A`, config-variant suffix | **Per-OEM parser plugin**, common interface (1.2) |
| Document standard | iSpec 2200 SGML (737, legacy A320) | **S1000D XML data modules** (A350, A220, newer) | Separate ingester — and S1000D is *easier*: a data module is already the task unit, no PDF scraping |
| Fault isolation | FIM | **TSM** (also ATR); Embraer FIM | Widen the `manual_type` enum: AMM/FIM/TSM/IPC/WDM/SRM/CMM/MEL/DDG |
| Effectivity | line numbers, MSN blocks, `TBC ALL` | MSN ranges + mod/SB status | Per-OEM effectivity parser (1.6) |

**Corpus reality — measured on the collection 2026-08-07, not assumed.** A filename scan for
AMM / TSM / FIM / IPC / WDM / SRM / CMM across every non-737 folder returned **zero hits**. The
collection holds maintenance manuals for **one type**:

| Type | Size | What it actually is | Usable for task retrieval |
|---|---|---|---|
| **Boeing 737-8 MAX** | 5.81 GB | AMM, IFIM, DDG, ActiveSchematic, SRG | **Yes — the only one** |
| Airbus A320 | 2.11 GB | Part-66 **B1/B2 type training**, SR Technics, organised by ATA chapter | No |
| Embraer 190 | 1.60 GB | AFM, AOM, DDPM, QRH, OMA, type-rating course, **MEL + MMEL** | No (MEL: yes, see below) |
| ATR 72-500 / 42-500 | 1.64 GB | AFM, FCOM 1–3, FCTM, QRH, W&B, performance, CBT, **MMEL** | No (MMEL: yes) |
| Boeing 787 / A340 | 0.90 GB | B1/B2 training by system | No |
| A350 / A380 / 777 / 767 / Concorde | ~0.6 GB | Level-1 familiarisation | No |

Two things in that non-Boeing material **are** usable and are hereby adopted:
- **Embraer 170/190 MEL + MMEL and ATR 72-500 MMEL** — real structured operational documents, and
  Phase 4B.1's MEL register otherwise has no content at all. Two non-Boeing types' worth.
- **A320 / A340 / 787 B1/B2 training, ATA-chaptered** — used **only** for per-OEM **abbreviation and
  terminology normalisation** (Airbus ECAM/CFDS vs Boeing EICAS/CMC). Explicitly **not** as a retrieval
  corpus and **not** for embedder fine-tuning — retraction 4 of the six-model review stands.

**Scope note, product not technology.** Gulfstream and ATR are technically the same job. But business
jets and regional turboprops are flown by small flight departments without a reliability engineer or a
CAMO — the user this product is aimed at. And per-type SDR volume is thin: ~890 avionics reports across
the *entire* US fleet in 2025, so once split by type, anything outside the 737/A320 families will be
suppressed by the small-n rule (3.4) almost everywhere. **Support them in the schema; do not target them.**

**GATE 1:**
- ✅ Coverage ≥70% vs TOC on every ingested chapter (below that, refuse queries in the uncovered range)
- ✅ Zero tasks where warnings/cautions were lost during sectioning
- ✅ All four gotcha regression tests pass
- ✅ **A stub second-OEM parser registers and runs without touching core code** — proves the seam is real
  before there is a second corpus to prove it with

---

### PHASE 2 — Retrieval engine *(headless; no UI yet)*

**Estimated effort: 3–4 weeks part-time. Do not start before Gate 0.**

| # | Task | Notes |
|---|---|---|
| 2.1 | SQLite schema (§4) + WAL + `sqlite-vec` | Local disk only |
| 2.2 | FTS5 index over task text and case narratives | Handles exact tokens: `34-11-01-400-801`, part numbers, `SMYD` |
| 2.3 | **Primary representation: `embed_text` (ATA hierarchy + task title), body excluded.** Multi-vector body embedding is demoted to *challenger*, run as an ablation | **Reversal from v1.0.** Jo (2025) reaches 91.64% Hit@5 on 8,229 B737 AMM+FIM tasks embedding titles only, and excludes body text deliberately because it churns across revisions. This **dissolves the 61.8% truncation problem** rather than engineering around it, and it is the only representation available for the 3,674 FIM catalogue rows. **Caveat that must be tested, not assumed:** their queries were GPT-4o-generated *from the task titles*, so the benchmark is partly circular. Real SDR narratives are not title-derived. If titles alone underperform on our gold set, the body multi-vector is the fallback — hence keeping it as an ablation |
| 2.4 | **Two rerankers, measured against each other:** (a) cross-encoder `ms-marco-MiniLM-L-6-v2` via FlashRank; (b) **LLM rerank** — structured prompt with query + top-50 candidates (ATA id, hierarchy, title), model returns **only a JSON array of reordered indices**, never text. **Mandatory fail-safe: invalid JSON → fall back to dense ranking** | Jo (2025): dense alone 85.34% Hit@5 → LLM rerank 91.64% (Llama 3.3 70B), and **Qwen3-4B holds 91.02%** — i.e. an office-PC model captures nearly all the gain. Note the LLM rerank *orders* candidates and cannot emit content, so it stays inside standing rule 3 |
| 2.4b | **Weight FTS5 down on noisy input.** Jo (2025) measured BM25 collapsing 87.57% → 63.38% Hit@5 on typo-injected queries while reranked dense held >88% | SDR narratives are typo-dense by nature. FTS5 stays for exact tokens (task numbers, P/N, `SMYD`) — it must not dominate free-text scoring |
| 2.5 | **JASC as soft boost, not hard gate** | Reporter-entered codes are miscoded at 22/24/27/31/34 boundaries; a hard filter makes the right task unreachable with no recovery |
| 2.6 | Separate filter paths for **tasks** (MSN effectivity) and **cases** (no mod state exists in SDR — tag with year-of-manufacture proxy and show provenance) | DeepSeek/Fable finding |
| 2.7 | Confidence calibration **per ATA chapter** on the gold set | A single global threshold is simultaneously too loose in 34 and too tight in 23 |
| 2.8 | **Evaluation harness** (§5) | Reports every metric on every run |

**GATE 2 — the viability test:**
- ✅ Recall@50 ≥ 80% on the gold set *(this is the ceiling on everything downstream)*
- ✅ NDCG@5 beats the stratified frequency baseline by a meaningful margin
- ✅ Reranked beats un-reranked
- ✅ Confident-and-wrong rate measured and reported
- ❌ **If hybrid retrieval does not beat top-20 frequency, the retrieval premise fails. Stop.**

---

### PHASE 3 — Case base and statistics

**Estimated effort: 2 weeks part-time.**

| # | Task | Notes |
|---|---|---|
| 3.1 | Case schema with **schema-enforced** separation of `defect_action` (what was replaced) and `defect_finding` (what was found) | Finding is **NOT NULL at closure**; FK back to the defect. Incomplete records are **excluded** from statistics, never counted as successes |
| 3.2 | Repeat-defect detection, **normalised**: ATA × symptom × elapsed × corrective action × confirmed cause | Naive tail×chapter over-counts badly (measured) |
| 3.3 | **The metric, correctly named:** *"removals on this P/N followed by a repeat of the same defect within 30 days."* **Not "NFF."** | NFF is a shop finding and is not in this data |
| 3.4 | Small-n suppression + confidence intervals; `n` always displayed, never in a tooltip | "43% from 7 cases" is statistically void and rhetorically powerful — the worst combination |
| 3.5 | Provenance split: **SDR-mined proxy** vs **operator-confirmed** — visually distinct, never pooled | |
| 3.6 | Present alternatives, not discouragement: *"7 of last 12 resolved at the connector — 20 min check, AMM 34-11-01-2xx"* | A bare percentage is the weakest possible nudge |
| 3.7 | Cross-standard down-weighting for pooled statistics | Simpson's paradox: pooling 1999- and 2015-standard airframes can be **directionally wrong** |

> **Honest limitation, to be stated in the UI:** without operator tech-log and shop-finding data,
> every statistic here is a **proxy computed from a reportable-occurrence sample.** It is
> directional evidence, not a measured rate.

---

### PHASE 4 — Application

**Estimated effort: 6–8 weeks part-time. UI polish is ~⅓ of total project effort — do not underestimate it.**

| # | Screen | Contents |
|---|---|---|
| 4.1 | Shell | PySide6, frameless window, custom title bar, QSS dark theme, left icon rail, `QSystemTrayIcon`. **UI stack fixed — see §4A** |
| 4.2 | Login | Users, **role table (not an `is_admin` flag)**, bcrypt/argon2 |
| 4.3 | Audit | **Hash-chained** rows (each carries predecessor hash), separate append-only file, periodic off-machine anchor |
| 4.4 | **Fault lookup** | Symptom box → ranked task **locators** + prior cases with `replaced:` / `found:` columns + repeat-defect banner for *that tail first* |
| 4.5 | Manuals browser | Aircraft type × manual type × revision selector; ATA tree; task view; **coverage % shown**; scoped search; **locator-only print** |
| 4.6 | Statistics | Per-tail and fleet, windows 1w–1y, repeat-defect detector, aggregate only |
| 4.7 | Fleet register | Tail, type, MSN, **line number / year of manufacture**, TT/cycles, per-tail config record (SB/mod/software where known) |
| 4.8 | Homepage | Recent defects, repeat-defect alerts, search, world clock (IANA `zoneinfo`, never fixed offsets), **plus the compliance triage feed from Phase 4B** |
| 4.9 | Admin (IT) | Document ingest, fleet CRUD, users/roles, **model management with forced re-index warning**, coverage report, audit viewer, **online-features toggle + allow-listed host list + API keys** |

#### 4A. UI stack — decided 2026-08-07 after a GitHub survey

All figures verified against the GitHub API and the actual `LICENSE` files. **Method note:** `pushed_at`
is misleading (it moves on any branch push) — dates below are the **last commit on the default branch**.

| Repo | ★ | Licence | Last commit | Verdict |
|---|---|---|---|---|
| `zhiyiYo/PyQt-Fluent-Widgets` | 8,056 | **GPL-3.0** | Aug 2026 | **Disqualified.** README: *"GPLv3 for non-commercial. For commercial use, please purchase a commercial license."* Target deployment is an MRO department = commercial. Same class of trap as PyQt6-vs-PySide6 |
| **`Alexhuszagh/BreezeStyleSheets`** | 661 | **MIT** | Mar 2025 | **SELECTED** |
| `ColinDuquesnoy/QDarkStyleSheet` | 3,083 | MIT (code) | Nov 2023 | Stale; 56 open issues; hand-maintained monolithic QSS |
| `UN-GCPDS/qt-material` | 2,858 | BSD-2 | ~May 2024 | Stale; Material look wrong for dense engineering data |
| `5yutan5/PyQtDarkTheme` | 750 | MIT | Dec 2022 | Dead |
| `Wanderson/PyDracula` | 3,076 | MIT | Jan 2024 | An app template, not a library — inherits someone else's skeleton |
| `Qt-Advanced-Docking-System` | 2,517 | LGPL-2.1 | Alive | Good code, wrong problem — docking suits IDEs, not a fixed 8-screen tool |
| Flet / NiceGUI / Tauri / CustomTkinter | 16k–110k | Apache/MIT | Alive | Wrong stack — web-rendered or non-Qt; the browser look was explicitly rejected |

**The finding that drove the choice:** *every* pure theme library is abandonware — Nov 2023, May 2024,
Dec 2022, zero commits in twelve months across all four. So star count is the wrong criterion.
**Pick the one you can own when it dies.**

**Why BreezeStyleSheets:** MIT with no dual-licensing · it is a **theme generator**, not a static
stylesheet — a colour map compiles both the QSS *and* a recoloured SVG icon set, so we produce our own
palette instead of inheriting someone's brand · freshest of the theme options with 10 open issues rather
than 56 · explicit PyQt6/PySide6 support · **small enough to fork and own outright**, which the
8,000-star option is not.

**Companion libraries — utilities, not alternatives.** All licences read directly from source:

| Repo | Licence | Activity | Role |
|---|---|---|---|
| `spyder-ide/qtawesome` | MIT | 27 commits/yr | Icon fonts — rail, status badges |
| `pyapp-kit/superqt` | BSD-3 | 38 commits/yr | Widgets Qt lacks: range sliders, collapsible sections, elidable labels |
| `pyqtgraph/pyqtgraph` | MIT *(verified in `LICENSE.txt`)* | 100 commits/yr | Statistics charts — faster than QtCharts on 1-year fleet views, and MIT rather than LGPL |

**Final stack:** PySide6 (LGPL) + BreezeStyleSheets fork (MIT) + qtawesome (MIT) + superqt (BSD-3) +
pyqtgraph (MIT). **No copyleft touches our code and there is no licence to buy.**

#### 4A.1 Visual rules that follow from the domain

1. **Brand: white + sky blue** (owner, 2026-08-08). **Light is the default theme** — white surfaces
   `#FFFFFF` on a sky-tinted ground `#EAF2F9`, with **sky-tinted hairlines `#CBE0F0`**; the tint in the
   borders is what makes the app read as sky blue rather than white-with-a-blue-button. Dark is the same
   identity at night — deep navy `#08131E`, not graphite. Two accent tokens so contrast holds on both
   grounds: `--cy` for text and icons (light `#0E74BC`, dark `#5BB4F0`) and `--cyf` for solid fills
   (light `#2E9BE0`, dark `#3E9FE0`, always white text on it).
   **The accent may never be red, amber or green** — those are load-bearing *data* (compliance state,
   repeat-defect severity, coverage warnings). No green "Save" button; nothing non-status may wear a
   status colour.
2. **Status is never colour alone** — every R/A/G badge carries an icon *and* a word. ~8% of male
   engineers are red-green colour deficient and this is a safety tool. (Global accessibility rules apply:
   4.5:1 minimum contrast, visible focus indicators at 3:1.)
3. **Two densities, deliberately.** Air is allowed only in the Home status hero. Every data view —
   search results, case tables, compliance register — uses compact rows (28–32 px), tabular numerals and
   no card padding. A consumer-software layout would show four results where fifteen are needed.
4. **Seven rail items plus Admin**, pinned bottom and visually separated because it is a different role:
   Home · Diagnose · Manuals · Fleet · Reliability · Compliance · **Ops** ⋯ Admin.
   Map, airport page and world clock all live under **Ops**, so the entire online-dependent surface is a
   single rail item that greys out when `online_enabled` is false — standing rule 11 made visible in the
   navigation rather than buried in settings.

**Print module — build exactly this:**
```
B737-8   ·   AMM Rev 48   ·   issued 2026-06-15
TASK 34-11-01-400-801   PITOT PROBE — REMOVAL / INSTALLATION
Effectivity: MSN 28xxx–41xxx        Aircraft: 4K-AZ12
Printed 2026-08-06 14:32Z by S. Asadli
── LOCATOR ONLY — OPEN THE CONTROLLED MANUAL FOR THE PROCEDURE ──
```
No procedure text. Ever.

---

### PHASE 4B — Fleet operations module *(owner-restored 2026-08-07; runs AFTER 4.1–4.7)*

**Estimated effort: 4–5 weeks part-time.** Restored on the owner's decision after being cut in v1.0.
Sequenced after the core screens so it cannot compete with Gate 2 work — but it is **in v1**, not v2.

| # | Screen | Contents |
|---|---|---|
| 4B.1 | **Compliance register** | Checkups (calendar **OR** flight hours **OR** cycles — whichever falls first, all three tracked per tail), MEL deferred defects with category A/B/C clocks, AD/SB register. Full red/amber/green triage. **Every row carries source system + import timestamp.** |
| 4B.2 | Compliance import | CSV/Excel import from the CAMO export; import history table; per-source freshness window; automatic degraded state when stale |
| 4B.3 | Homepage triage feed | Red = limit breached, amber = inside the warning window, ordered by time-to-limit. Provenance line under the header, always visible |
| 4B.4 | **Live fleet map** | ADS-B positions per tail (OpenSky free tier — anonymous 400 credits/day, registered 4,000; **decide auth mode before build**). Aircraft icons, altitude/speed/heading, click-through to the tail's defect and compliance record |
| 4B.5 | **Airport page** | ICAO/IATA, runways, elevation (offline, OurAirports — public domain, **bundled, 85,836 airports**) · IANA timezone and local time (**resolved from lat/lon by `timezonefinder`, MIT — see correction below**) · METAR/TAF decoded, wind, QNH, humidity, visibility (NOAA Aviation Weather API — free, no key) · arrivals/departures (OpenSky). **Offline fields render always; online fields show "unavailable offline"** |
| 4B.6 | Network layer | Single client with allow-listed hosts, per-source rate limiting, disk cache with TTL, exponential backoff, hard timeouts. Never on a UI thread |

**Data sources — all free, verified public before build:**

| Source | Gives | Cost | Note |
|---|---|---|---|
| OurAirports CSV | Airport identifiers, runways, frequencies, navaids | Free, **public domain** | **Bundled offline** (`assets/data/`, 19.5 MB, downloaded 2026-08-07) — never a network call |
| `timezonefinder` 8.2.5 | IANA zone from lat/lon | **MIT**, offline | **⚠ Correction:** an earlier note claimed OurAirports "carries the tz field". **It does not** — there is no timezone column, verified against the real header. OpenFlights `airports.dat` does have one but covers only 7,698 airports (9%), is stale, and is ODbL. `timezonefinder` covers all 85,836, is MIT, and adds no second dataset to keep in sync. Store the IANA **name**, never an offset; `zoneinfo` handles DST |
| NOAA Aviation Weather (`aviationweather.gov`) | METAR / TAF | Free, no key | Already used successfully in the weather-bot work |
| OpenSky Network | Live positions, arrivals/departures | Free tier, credit-limited | Coverage is incomplete by design — state this in the UI, never present it as authoritative traffic data |

**GATE 4B (before this module ships, not before it is built):**
- ✅ Core app fully functional with `online_enabled = false` and the network cable out
- ✅ No compliance alert renders anywhere without its provenance line
- ✅ Stale import forces the degraded state — verified by test
- ✅ Every outbound host appears in the Admin allow-list

---

### PHASE 4C — Engineer notes *(the only feature that generates data instead of consuming it)*

**Estimated effort: 1 week part-time.** Runs with or after 4B.

**Rationale, and it is not convenience.** `defect_finding` is NOT NULL at closure — *what was actually
found*, as opposed to what was replaced. That field is what separates this product from "the engineer
swapped a box", and **no other feature in the plan captures it.** SDR does not contain it; the shop
never returns it. An engineer typing *"found chafed wire at the connector behind P6-4, not the LRU"*
against a defect **is** the finding. It is also the instrumentation Kimi's review point 6 demands —
without a record of whether the statistic was shown, whether the removal proceeded and how it turned
out, the core product claim is not falsifiable. And **shift handover is one of the four use cases the
product is positioned on** (§0.3).

| # | Item | Detail |
|---|---|---|
| 4C.1 | **Anchored notes** | Every note has a mandatory anchor: tail · defect · task · case. **No free-floating notes.** Markdown-light body, author, created/edited timestamps |
| 4C.2 | Notes panel | Rendered inside the object it belongs to — fault lookup, tail page, defect record. **Not a rail item** |
| 4C.3 | "My notes" view | Flat list + filter, under Home. Doubles as the **shift-handover view**: open notes on tails worked this shift |
| 4C.4 | Optional due date | A date field on a note. Drives sorting and a passive "due" marker in the list |
| 4C.5 | **`.ics` export** | Notes with a due date export to the engineer's real calendar (Outlook/Teams). **The app never fires an alarm** — see below |
| 4C.6 | Findings promotion | A note on a defect can be promoted, in one action, to a structured `defect_finding` row with `finding_type`. **This is the point of the feature** |

**Why the app must not own the alarm.** Three failure modes, and the third is the serious one:
① the app has to be running — close it Friday, the Monday alarm never fires; ② one PC, no phone, no
Teams, while every engineer already has Outlook; ③ **a missed reminder manufactures the same false
confidence as a shadow compliance clock.** *"Check the MEL on 4K-AZ12 by Friday"* creates a second
record of a deadline with **no source system and no import timestamp** to stamp it with — the one
mitigation standing rule 2 relies on. So: the app owns the **note**, because it is anchored to an
engineering object the calendar knows nothing about; the **calendar owns the alerting**.

**Cut deliberately:** an in-app calendar grid and in-app alarms. A month view costs a screen and shows
the engineer their own notes, which the list does better, while the department's real calendar is
elsewhere.

**Three binding rules:**
1. **Private to the author by default**, explicit "share with team" action. Named engineers writing
   dated notes is GDPR personal data, and a searchable who-wrote-what record is performance-monitoring
   adjacent — **BetrVG §87(1)(6)**, the trap that already deferred the Part-66 matrix (§7).
2. **Notes never feed labels or statistics.** They are evidence a human reads, never training data,
   never a row in any aggregate view.
3. **Notes inherit `tool_assisted`** (standing rule 7). A note pasted into an official write-up
   contaminates that narrative, which must then be excluded from all future label sets.
   **The flag cannot be retrofitted.**

---

### PHASE 5 — LLM layer *(optional, last, and the app must work without it)*

**Estimated effort: 1 week.**

- Ollama client with a **configurable endpoint** (localhost *or* one LAN host serving the department —
  solves the 8 GB RAM problem and the corporate-install problem in one move).
- Default **Gemma 3 4B Q4_K_M** (~2.5 GB). Auto-detect RAM; disable below 16 GB.
- **Extractive only** — quote and cite retrieved *case narratives*. Never procedural text.
- Validation: every task number checked against the retrieved set; warnings/cautions in output must be
  a **superset** of the source; any numeral not from the DB is a bug.
- Low-confidence gate → *"no task matches — escalate to a licensed engineer."*
- **Streaming UI**: results and statistics appear instantly; the summary streams in after. Never block.

---

### PHASE 6 — Packaging and operations

**Estimated effort: 2 weeks.**

- PyInstaller → Inno Setup; per-user install path (no admin rights assumed).
- **Code signing** — corporate AV/EDR will flag an unsigned PyInstaller binary. This is near-certain.
- Backups: `VACUUM INTO` (never file-copy a live SQLite DB), 3-2-1, **restore tested**.
- `PRAGMA integrity_check` on a schedule.
- Documented "tool unavailable → use approved source" procedure.
- Version stamp on app, schema, index and every model.

---

## 4. Data model (core tables)

```sql
aircraft(id, tail, type, msn, line_number, year_built, delivery_date)
aircraft_config(id, aircraft_id, sb_embodied, stc, software_load, effective_from)

manual(id, oem, aircraft_type, manual_type, doc_standard, parser_plugin,
       revision, revision_date, is_current, source_file, source_hash, ingested_at)
       -- oem:           boeing|airbus|embraer|atr|gulfstream|...
       -- manual_type:   AMM|FIM|TSM|IPC|WDM|SRM|CMM|MEL|DDG
       -- doc_standard:  ispec2200|s1000d|pdf_legacy
task(id, manual_id, task_number, function_code, title,
     ata_chapter, ata_section, ata_subject, effectivity_raw, body, body_hash, token_len)
task_section(id, task_id, seq, kind, text)        -- warning|caution|step|reference|zone|tooling
task_vector(task_section_id, index_version, vector)
task_link(from_task_id, to_task_number, link_type) -- reference|conditional|next
coverage(manual_id, ata_chapter, toc_count, extracted_count, pct)

defect(id, aircraft_id, reported_at, ata_ref, fault_code, fault_code_source,
       defect_text, rectification_text, source, tool_assisted, sdr_year)
defect_action(id, defect_id, action_type, part_name, part_number, position, task_id)
defect_finding(id, defect_id, finding_type, finding_text, found_at, source)  -- NOT NULL at closure
defect_closure(defect_id, closed_at, closed_by, reason_for_removal, complete)

note(id, author_id, anchor_type, anchor_id, body, due_date, shared, tool_assisted,
     created_at, updated_at)          -- anchor_type: aircraft|defect|task|case (NEVER null)
                                      -- shared defaults false; never joined into any aggregate view

label_silver(id, defect_id, task_number, function_code, confidence_tier, leak_free)
label_gold(id, defect_id, task_number, verdict, correct_task_number, adjudicated_at)

app_user(id, username, pwhash, role_id)
role(id, name, permissions)
audit_log(id, ts, user_id, action, entity, entity_id, payload_hash, prev_hash, row_hash)
print_log(id, ts, user_id, task_id, manual_revision, aircraft_id)
```

**Schema invariants to enforce in code, not just convention:**
- `defect_finding` is mandatory before `defect_closure.complete = true`.
- Statistics queries must filter `defect_closure.complete = true`.
- `manual.is_current` — exactly one per (aircraft_type, manual_type); superseded revisions retained.
- `audit_log.prev_hash` chain verified on startup.
- `note.anchor_type` and `note.anchor_id` are **NOT NULL** — the schema forbids a free-floating note.
- **No statistics query may reference the `note` table.** Enforce in code review and by test.

---

## 5. Evaluation protocol

**You cannot evaluate a regex-labelled system on regex labels.** That measures regex agreement,
not correctness.

**Gold set:** 400 pairs, stratified by ATA chapter × function code × narrative length × fault-code
presence. Held out permanently — never used for tuning, threshold-setting or model selection.

**Who adjudicates, stated accurately (corrected 2026-08-08).** Sarvan holds a B.Sc. in Aeronautical
Engineering and did AMM/ICAO inspection documentation at Silk Way Technics. He is **not** a Part-66
licensed engineer and has no shop-floor troubleshooting history. An earlier draft of this section said
"licensed-engineer-equivalent" — that was wrong and is withdrawn. The protocol below is built around
what that level of knowledge can and cannot support.

| Judgement | Can he? | Why |
|---|---|---|
| Is the cited task **diagnostic or action**? | **Yes** | It is the function code (`-810` fault isolation vs `-400` removal). Mechanical — and it is the most important call in the set, because the 7.5:1 action skew *is* the core problem |
| Is it in the **right ATA subject** for the symptom? | **Yes** | An airspeed complaint cited against a landing-gear task needs no licence to reject |
| Does the **task content** match the symptom? | **Yes, with the AMM open** | He holds the 737 MAX AMM. Reading the actual task converts clinical judgement into reading comprehension |
| Was it the **optimal** entry point (pitot-static test vs ADIRU BITE first)? | **No** | Requires real troubleshooting experience |

**Three protocol changes that follow:**

1. **"Unsure" is a first-class verdict and its rate is reported.** Forcing a guess into four buckets is
   what makes a gold set worthless. An honest 30% unsure is usable; a dishonest 0% is not. Unsure pairs
   are **excluded from scoring**, never counted against the system.
2. **Adjudicate with the manual open.** Slower per pair, far higher quality.
3. **Professional overlap — 50 pairs, and this is the fix.** Have a **Part-66 licensed engineer**
   (Silk Way / AZAL contacts — already the §8 workstream) independently adjudicate 50 of the same pairs.
   That yields a **measured agreement rate between the adjudicator and a professional**:
   - ≥80% → the 400 are usable, quote the error bar alongside every Gate 0 and Gate 2 number
   - 60–80% → usable for the structural verdicts only; drop the optimal-entry-point question
   - <60% → **the gold set is the weak link.** Stop and get professional adjudication before building
     anything on it
   Fifty pairs is roughly two hours of someone's time — the cheapest de-risking in the project, and it
   converts "am I qualified to judge this?" from an open worry into a number.

**Metrics, reported every run:**

| Metric | Why |
|---|---|
| Stage-1 recall (after JASC boost + effectivity) | Caps everything downstream |
| **Recall@50** after hybrid | The hard ceiling — if the right task isn't in 50, the reranker is decorating a failure |
| NDCG@5 after rerank | Final ranking quality |
| Abstention rate at threshold | Is the low-confidence gate usable? |
| **Score distribution of *incorrect* top-1** | Confident-and-wrong — the failure nobody tests for |

**Baselines that must be beaten:**
1. Stratified top-20 task frequency per ATA chapter
2. BM25 / FTS5 alone
3. Vector alone
4. Hybrid without reranker
5. **Published prior art — Jo (2025):** dense over ATA-hierarchy+title, then LLM rerank.
   Reference numbers on *their* corpus: dense alone **85.34%** Hit@5, +LLM rerank **91.64%**,
   human study **90.9%** top-10 with 10 licensed AMTs. **These are not our targets** — different corpus,
   partly circular query generation, and our queries are real SDR narratives rather than paraphrased
   task titles. They are the bar the design must be measured against, and a large shortfall is a
   finding worth reporting either way.

**Ablations to run:** title-only vs body-multi-vector × MiniLM vs bge-small/BGE-M3 ×
cross-encoder vs LLM-rerank vs none. Compare on **retrieval NDCG@10** and **Hit@5**, not overall
MTEB average. Report Hit@5 alongside NDCG so results are directly comparable to the published work.

---

## 6. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Silver labels too noisy → Gate 0 fails | Medium | **Fatal to case base** | Gold audit early; pivot to manual-retrieval-only |
| **The gold set itself is unreliable** — adjudicator is not Part-66 licensed | **Medium** | **Fatal, and silently so**: every Gate 0 and Gate 2 number would be measured against a bad ruler | "Unsure" as a first-class verdict with its rate reported · adjudicate with the AMM open · **0.7b professional overlap on 50 pairs turns this from an unknown into a measured agreement rate** (§5) |
| Retrieval doesn't beat frequency baseline | Medium | **Fatal** | Gate 2; measure before building UI |
| 6 AMM chapters unrecoverable | Medium | Loses ATA 24 & 27 | Try 4 repair routes; document coverage honestly |
| No FIM/TSM ever obtained | **High** (IFIM encrypted) | Paths 1–4 never built | Ship path 5 only; state the limitation |
| No WDM obtained | **High** | Every path ends at a removal | Say so in the UI |
| No operator data obtained | **High** | Statistics unvalidatable | Label them proxy; do not claim measured rates |
| UI polish consumes the project | **High** | Stalls at 60% | Headless core through Phase 3; UI only after Gate 2 |
| Corporate AV blocks the installer | High | Blocks deployment | Code signing budgeted in Phase 6 |
| Ollama refused by IT | Medium | No LLM | LLM optional by design; LAN-host option |
| Timeline growth from restored Phase 4B | **Certain** | +4–5 weeks → 25–30 weeks total | Sequenced after 4.1–4.7; cannot delay Gates 0/1/2 |
| **Stale compliance import trusted as live** | Medium | **Airworthiness finding** | Mandatory provenance line + forced degraded state (§2 rule 2, Gate 4B) |
| Online module drags the offline core down | Medium | Core unusable when IT blocks internet | §2 rule 11 — isolated module, separate thread, Gate 4B tested cable-out |
| OpenSky credit limits / partial coverage | High | Map gaps, missing tails | Cache with TTL; label coverage as incomplete in the UI; never present as authoritative traffic |
| **Commercially pre-empted** — Veryon Diagnostics/ChronicX already ships repeat-defect NLP at ~25% of the world fleet | **Certain (it exists today)** | The repeat-defect feature is not novel | Compete on the axis they cannot reach without customer data: outcome-linked case evidence from public data, offline desktop, reliability-engineer framing. Do **not** claim novelty on chronic-defect clustering |
| Published work already covers Phase 2 | Certain | Retrieval is not the contribution | Adopt it (v1.2) rather than re-derive it. The contribution is the case layer and honest evaluation on *real* narratives |
| Bus factor = 1 | Certain | Project dies if he stops | Document as you go; tests for the parser |

---

## 7. Deferred and cut — binding unless explicitly revisited

| Feature | Status | Reason |
|---|---|---|
| **In-app calendar grid** | **CUT** (4C) | Costs a screen to show the engineer their own notes, which the list does better. The department's real calendar is Outlook |
| **In-app alarms / reminders that fire** | **CUT** (4C) | Requires the app to be running, lives on one PC with no phone, and a missed reminder manufactures the same false confidence as a shadow compliance clock — with no source system to stamp it. Replaced by `.ics` export |
| **Live ADS-B tracking** | **RESTORED to v1** (owner, 2026-08-07) | Phase 4B.4, behind the `online_enabled` toggle. Prior objections stand as design constraints, not as reasons to omit |
| **METAR/TAF airport page, arrivals/departures** | **RESTORED to v1** (owner, 2026-08-07) | Phase 4B.5. Offline fields always available; online fields degrade visibly |
| **Checkup scheduler with alerting** | **RESTORED to v1** (owner, 2026-08-07) | Phase 4B.1–4B.3, full triage. Mitigation is mandatory provenance + staleness, not suppression |
| **MEL / AD-SB registers** | **RESTORED to v1** (owner, 2026-08-07) | Same module, same rules |
| **Part-66 authorisation matrix** | **DEFERRED** | BetrVG exposure, no contribution to the core |
| **Fault-code paths 1–4** | **PARTIALLY UNBLOCKED** (v1.2) | FIM **task locators** now buildable from the 3,674 IFIM filenames (Phase 1.7) — titles are sufficient representation per Jo (2025). Still blocked: the conditional decision tree *inside* a FIM task, which needs the encrypted content |
| **Embedding fine-tune on training manuals** | **DEFERRED** | Likely *degrades* retrieval — pedagogical prose vs terse queries vs procedural targets. Measure baseline first |
| World clock | **KEEP** | Trivial, wanted, harmless (IANA `zoneinfo`) |

---

## 8. Parallel workstream — data access

Run continuously alongside the phases. Each item unlocks a blocked capability.

| Target | Unlocks | Route |
|---|---|---|
| **An AMM for any second type** (A320 the obvious target — the largest SDR population alongside the 737, and VLM extraction is already proven on A320 at 99.39% precision) | Turns the multi-type architecture from an empty frame into a real capability | Silk Way / AZAL contacts |
| **A Part-66 engineer willing to adjudicate 50 pairs** (~2 hours of their time) | **Validates the gold set, which validates every other number in the project.** Highest value per hour of anything in this table — do it first | Silk Way / AZAL contacts |
| **WDM / SWPM** for one type | Interconnect faults — the actual NFF root cause | Silk Way / AZAL contacts |
| **FIM or TSM** (unencrypted) | Routing paths 1–4 | Same |
| **Operator tech log**, one type, one year | A real case base; validates all statistics | Same |
| Shop findings / CMM | The true NFF metric | Same |
| OEM published top-NFF-driver reliability data | Free, curated, on-target | Public OEM channels |

---

## 9. Version 1 definition of done

- Free-text symptom → ranked **task locators** for 737 MAX, chapters that parsed, with coverage shown
- Prior SDR cases with `replaced:` / `found:` separation and repeat-defect proxy statistics
- Manuals browser with revision control and **locator-only** printing
- Repeat-defect view
- Login, two roles, hash-chained audit
- **Compliance register** — checkups (calendar/hours/cycles), MEL, AD/SB — with red/amber/green triage,
  every row provenance-stamped, degraded state on stale import
- **Live fleet map** and **airport page**, both behind the `online_enabled` toggle
- **Anchored engineer notes** with optional due dates, `.ics` export, and one-action promotion of a
  note to a structured `defect_finding` — private by default, never in any aggregate view
- Runs on a 16 GB office PC, installs per-user
- **No** LLM required · **runs fully with the network unplugged**, online features simply unavailable

---

## 10. Known unknowns — what this plan cannot guarantee

Stated deliberately, because a plan claiming certainty here would be dishonest.

1. **Whether the retrieval premise works at all.** Gate 2 answers it; no amount of design does.
2. **Whether the labels are good enough.** Gate 0 answers it. Both gates can fail.
3. **Whether anyone would use it.** Six reviews agree the AOG case is lost. The reliability-engineer
   case is *plausible* and untested — there is no user yet. **And §11 confirms the market is already
   served** by better-funded products; the unoccupied ground is narrow and specific.
6. **Whether the title-only representation transfers.** It works on paraphrased task titles (91.6%).
   Real SDR narratives share little vocabulary with task titles. Gate 2 answers this; the body
   multi-vector ablation is the hedge.
4. **Effort estimates are estimates.** ~26–31 weeks part-time total (20–25 core, +4–5 Phase 4B,
   +1 Phase 4C). Single-developer side projects routinely run 2× that.
5. **The corpus may simply be too thin.** ~890–1,240 avionics reports/year, 851 AMM tasks from one
   type, no FIM, no WDM. That may not be enough to be useful even if every technical step succeeds.

**The honest position:** Phases 0–2 cost roughly 8 weeks and answer questions 1 and 2 definitively.
If both gates pass, the rest is ordinary engineering. If either fails, you will have learned something
real about aviation maintenance data for 8 weeks of work — and you stop before building a UI on sand.

---

## 11. Prior art — scanned 2026-08-07

The space is occupied. This section exists so no future decision is made as though it were empty.

### 11.1 Commercial — competing today

| Product | What it does | Relation to us |
|---|---|---|
| **Veryon Diagnostics / ChronicX** | NLP clustering of recurring defects, flags a chronic issue **from the second repeat**, fix-rate benchmarking. Per-aircraft pricing, claimed ~25% of the world commercial fleet | **Direct competitor to Phase 3.** Runs on the operator's own defect feed — the data we lack. Do not claim novelty here |
| **Veryon AI Assist** (2024) | Chat over an OEM knowledge base | Competes with Phase 5 |
| **TRAX eMRO TechDocs** | AI-assisted tech-pub search + compliance tracking | Competes with Phase 4.5 and 4B.1 |
| OxMaint, Jenova, various "MRO copilots" | Ranked probable causes + AMM refs + parts + AD checks, "under 10 seconds" | Vendor marketing; the quoted 62%/67% improvements are not measured studies. Treat as positioning, not evidence |

### 11.2 Research — the two that changed this plan

- **Jo, B. (2025), *A Compliance-Preserving Retrieval System for Aircraft MRO Task Search*,
  arXiv:2511.15383, Inha University.** 8,229 B737 AMM+FIM tasks. Embeds **ATA hierarchy + task title
  only, body excluded** (revision-robust). Dense → top-50 → **LLM rerank emitting JSON indices only**,
  with fail-safe fallback to dense. Hit@5: BM25 73.06, dense 85.34, **+LLM rerank 91.64** (Llama 3.3 70B);
  **Qwen3-4B 91.02**. Human study: 10 licensed AMTs, 197 queries, **90.9% top-10**, TCT **18.0 s** vs
  6.35 min experienced / 15.41 min junior. VLM extraction 99.51% F1, 1.85% CER over 20 A320 + 20 B737 manuals.
  **Weakness we exploit:** synthetic queries were GPT-4o-generated *from task titles* — partly circular.
  Our gold set uses real narratives, so our numbers will be lower and more honest.
- **Rasaq, Siddula, Yadav (NC A&T) with Walthall, Ensberg, Yadav (Collins Aerospace) — PHM Europe.**
  Fully offline RAG: Llama 3.2 3B + all-MiniLM-L12-v2 + `ms-marco-TinyBERT-L2-v2` rerank on a
  **Jetson Orin Nano 8 GB**. Fidelity 0.83, **4–10 s per query**. Validates the offline stack and sets a
  realistic latency expectation on constrained hardware — with an OEM co-author.

Also: **CAMB** (arXiv:2508.20420) civil-aviation maintenance LLM benchmark · **KEO** (arXiv:2510.05524)
knowledge graphs + RAG, local-only · **AviationLLM** (arXiv:2506.14336). And **case-based reasoning for
aircraft troubleshooting is a mature literature** — PHM Society, ESWA 2022 (aero-engine CBR), Lin et al.
2025 (structural CBR + LSI). Our "case base" has an academic name and a citation trail; use it.

### 11.3 Open source and data

- **`Boeing/sdr-hazards-classification`** — FAA × Boeing, **MIT**, SME-annotated SDR records + training
  code, on PyPI. Classification only, no retrieval. **Adopted in Phase 0.9.**
- FAA SDR holds **~1.7 M reports since 1975** — larger than the 32 years scoped in Phase 0.1.
- **No open-source project does AMM semantic search by ATA chapter.** That slot is empty.
- `wtruib.ru` hosts B737-800 AMM and FIM indices (the corpus Jo used). **Verified 2026-08-07: chapter
  lists are public; the task-level index is behind a login.** Not a free bulk source — and we do not
  need it, because Phase 1.7 builds the FIM catalogue from files already in hand.

### 11.4 What remains unoccupied

1. **Outcome-linked cases** — everyone retrieves procedures; Veryon clusters defects; nobody surfaces
   *"what was attempted for this symptom and how it turned out"* with `replaced:` / `found:` separated.
2. **An installable offline desktop application** — all prior art is SaaS or a research prototype.
3. **Public data as the starting corpus** — every competitor needs the customer's data first.
4. **Evaluation on real narratives.** The published work evaluates on synthetic, partly circular queries.
   Gates 0 and 2 are more rigorous than the state of the art. **This is the defensible contribution.**

---

*Companion documents: `project_avionics_workstation.md` (design notes) and
`project_avionics_workstation_review.md` (six-model review outcome) in the Claude memory store.
Where any of the three conflict, the review file wins, then this plan, then the design notes.*
