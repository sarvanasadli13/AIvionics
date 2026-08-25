"""Read-only evaluation against the frozen human gold set.

Deliberately a separate command from the silver evaluation, and deliberately
not a flag on it. The silver numbers measure agreement with a regex over
26,000 pairs; these measure agreement with a person over 400. Merging them
into one entry point is how a silver number ends up quoted as a gold one.

Three rules are enforced here rather than trusted:

* **The database is opened `mode=ro`.** This command cannot write, so it
  cannot fit a threshold, save a calibration or mutate a table by accident.
* **Only a frozen release counts.** An unfrozen set is still being edited,
  and a metric computed over a moving reference is not a measurement.
* **The 400 cases are permanently held out.** They may not be used for
  training, prompt tuning, embedding selection, reranker selection,
  threshold fitting or calibration — see PLAN §5 and the freeze warning in
  the questionnaire.

    python scripts/evaluate_gold.py
    python scripts/evaluate_gold.py --db data/aivionics.db --json out.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(ROOT / "src"))

from aivionics import config, goldreview as G                 # noqa: E402


class NotReleased(RuntimeError):
    """No frozen release, or one that no longer describes this database."""


def open_readonly(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def released(con: sqlite3.Connection) -> sqlite3.Row:
    """The current frozen release, or a refusal explaining what is missing."""
    try:
        row = con.execute("SELECT * FROM gold_set_release ORDER BY version DESC "
                          "LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        raise NotReleased(f"this database has no gold_set_release table ({exc})")
    if row is None:
        raise NotReleased(
            "no gold release exists. Complete the 400 cases in AI Validation "
            "and freeze the set before quoting a gold number.")
    if row["status"] != "frozen":
        raise NotReleased(
            f"release v{row['version']} was reopened for editing"
            f"{': ' + row['unlock_reason'] if row['unlock_reason'] else ''}. "
            f"Freeze it again before evaluating.")
    return row


def verify_fingerprints(con: sqlite3.Connection, release: sqlite3.Row) -> None:
    """The release names a specific dataset. Prove it is still that one."""
    svc = G.GoldReviewService(con, None)
    qf, rf = svc.fingerprints()
    if qf != release["queue_fingerprint"]:
        raise NotReleased(
            f"the queue has changed since release v{release['version']} was "
            f"frozen — its fingerprint no longer matches. This release does "
            f"not describe the current database.")
    if rf != release["response_fingerprint"]:
        raise NotReleased(
            f"the answers have changed since release v{release['version']} "
            f"was frozen. Freeze a new version rather than quoting this one.")


def report(con: sqlite3.Connection, release: sqlite3.Row) -> dict:
    labels = G.load_gold_labels(con)
    score = G.score_gold(labels)
    corrected = [lab for lab in labels if lab.correct_task_number]
    unavailable = [lab for lab in labels if lab.correct_task_unknown]
    by_verdict = {v: sum(1 for lab in labels if lab.verdict == v)
                  for v in G.VERDICTS}
    return {
        "release_version": release["version"],
        "finalized_at": release["finalized_at"],
        "case_count": release["case_count"],
        "queue_fingerprint": release["queue_fingerprint"],
        "response_fingerprint": release["response_fingerprint"],
        "labels": "gold — agreement with a human reviewer",
        "counts": by_verdict,
        "scored": score.scored,
        "correct": score.correct,
        "incorrect": score.incorrect,
        "partial_reported_separately": score.partial,
        "excluded_unsure": score.excluded,
        "unsure_rate": round(score.unsure_rate, 4),
        "strict_precision": (None if score.precision is None
                             else round(score.precision, 4)),
        "cases_corrected_to_another_task": len(corrected),
        "cases_where_correct_task_unavailable": len(unavailable),
        "held_out": ("permanently — not for training, prompt tuning, embedding "
                     "or reranker selection, threshold fitting or calibration"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Read-only evaluation against the frozen gold set")
    ap.add_argument("--db", type=Path, default=config.DB_PATH)
    ap.add_argument("--json", type=Path, help="also write the report as JSON")
    args = ap.parse_args()

    con = open_readonly(args.db)
    try:
        release = released(con)
        verify_fingerprints(con, release)
    except NotReleased as exc:
        print(f"refused: {exc}")
        return 2

    data = report(con, release)
    print(f"Gold release v{data['release_version']}  ·  frozen "
          f"{data['finalized_at'][:19].replace('T', ' ')}")
    print(f"  queue fingerprint    {data['queue_fingerprint'][:16]}…")
    print(f"  response fingerprint {data['response_fingerprint'][:16]}…")
    print()
    c = data["counts"]
    print(f"  yes {c['yes']}   no {c['no']}   partial {c['partial']}   "
          f"unsure {c['unsure']}")
    print(f"  scored {data['scored']} of {data['case_count']}  "
          f"({data['excluded_unsure']} unsure excluded, "
          f"{data['unsure_rate'] * 100:.1f}%)")
    p = data["strict_precision"]
    print(f"  strict precision     {'—' if p is None else f'{p:.3f}'}  "
          f"(partial counts as incorrect; {data['partial_reported_separately']} "
          f"partial)")
    print(f"  corrected elsewhere  {data['cases_corrected_to_another_task']}")
    print(f"  no task in corpus    {data['cases_where_correct_task_unavailable']}")
    print()
    print(f"  {data['held_out']}")

    if args.json:
        args.json.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
