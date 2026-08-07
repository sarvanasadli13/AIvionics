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

# Embedding / index versioning. Changing the model invalidates every vector:
# bump INDEX_VERSION whenever EMBED_MODEL changes (standing rule 9).
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384
INDEX_VERSION = "v1-bge-small-en-1.5"

# Avionics-relevant JASC/ATA chapters (per design sessions)
AVIONICS_CHAPTERS = {"22", "23", "31", "34", "42", "44", "45", "46", "77"}

SDR_YEARS = list(range(1995, 2027))
