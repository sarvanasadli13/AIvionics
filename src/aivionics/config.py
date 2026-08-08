"""Central paths and constants. Everything data-related lives under data/."""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
SDR_DIR = DATA_DIR / "sdr"
DB_PATH = DATA_DIR / "aivionics.db"
ASSETS_DIR = PROJECT_ROOT / "assets"

# 737 MAX training-package corpus (external drive)
CORPUS_ROOT = Path(os.environ.get(
    "AIVIONICS_CORPUS",
    r"D:\Sery\Aviation\Aircraft\Boeing 737 8-max\737MAX_2018-02-27_14-10-00",
))
AMM_DIR = CORPUS_ROOT / "AMM"
IFIM_DIR = CORPUS_ROOT / "IFIM" / "fim"

# Structurally repaired chapter PDFs (scripts/repair_amm.py). Six chapters are
# missing the first MiB of their file and store objects in compressed streams,
# so nothing will open them until they are rebuilt. Derived artefacts:
# regenerable from the corpus, never committed.
AMM_REPAIRED_DIR = DATA_DIR / "amm_repaired"

# Embedding / index versioning. Changing the model invalidates every vector:
# bump INDEX_VERSION whenever EMBED_MODEL changes (standing rule 9).
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384
INDEX_VERSION = "v1-bge-small-en-1.5"

# Scope note (owner, 2026-08-08): the tool covers the WHOLE ATA range —
# mechanical, structural and engine systems as well as avionics. An
# AVIONICS_CHAPTERS constant lived here and was never referenced; it was
# removed rather than left as a filter waiting to be applied by mistake.
# Chapter names live in parsers/ata.py; nothing filters by chapter.

SDR_YEARS = list(range(1995, 2027))
