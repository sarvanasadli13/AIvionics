"""Phase 0 — normalise the SDR year files, then build defects + silver labels.

Run:  python scripts/phase0.py
Requires Phase 1 to have run first: the function-code whitelist that kills the
001/101/201/404/911 false positives is derived from the extracted AMM tasks
and the FIM catalogue, never guessed.

Idempotent: ingest skips years already in sdr_raw, labels.build skips if
defect rows already exist.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from aivionics import config, db
from aivionics.pipeline import ingest_sdr, labels


def check_downloads_complete() -> None:
    """A partially written CSV must never be ingested."""
    log = config.SDR_DIR / "_download_log.csv"
    if not log.exists():
        raise SystemExit(f"no download log at {log} — run scripts/fetch_sdr.ps1")
    text = log.read_text(encoding="utf-8", errors="replace")
    if "DONE" not in text:
        raise SystemExit(
            "download log has no DONE line — the fetcher is still running. "
            "Ingesting now would read a half-written CSV.")
    failed = [ln.split(",")[0] for ln in text.splitlines() if "FAILED" in ln]
    present = sorted(config.SDR_DIR.glob("SDR-*.csv"))
    print(f"  downloads complete: {len(present)}/{len(config.SDR_YEARS)} files"
          + (f" · FAILED: {', '.join(failed)}" if failed else ""))


def main() -> None:
    check_downloads_complete()
    con = db.connect()

    print("== Phase 0.2: SDR ingest ==")
    ingest_sdr.run(con)

    print("== rows per year in sdr_raw ==")
    total = 0
    for year, n in con.execute(
            "SELECT source_year, COUNT(*) FROM sdr_raw "
            "GROUP BY source_year ORDER BY source_year"):
        print(f"  {year}: {n:,}")
        total += n
    print(f"  TOTAL: {total:,}")
    missing = set(config.SDR_YEARS) - {
        y for (y,) in con.execute("SELECT DISTINCT source_year FROM sdr_raw")}
    if missing:
        print(f"  YEARS ABSENT FROM sdr_raw: {sorted(missing)}",
              file=sys.stderr, flush=True)

    print("== Phase 0.3-0.6: split, silver labels, confidence tiers ==")
    summary = labels.build(con)
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
