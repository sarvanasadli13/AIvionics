"""The tools a model may call, and the boundary that keeps them honest.

A tool-calling model does not get to touch the database. It gets to ask one of
the functions below a question with named arguments, and every one of them:

* **validates its own input** and raises rather than guessing what was meant;
* **checks the caller's permission** against the `role` table the application
  already authenticates against — the same `ROLES` string, not a second copy
  that can drift from it;
* **returns a bounded page** with a documented maximum, and *rejects* a limit
  above it rather than quietly clamping. Silently returning 25 rows to a
  caller that asked for 1,000 hands the model a sample it will reason about as
  though it were the whole population;
* **carries `source` and `freshness`**, because a row with no provenance is a
  row that will eventually be read as current when it is four years old;
* **returns stable ids** — task numbers, defect ids, tails, manual ids — so
  the grounding check in `schemas.py` is a set-membership test rather than a
  string-similarity guess;
* **fails explicitly.** The rule that shapes most of this file: an empty
  success is indistinguishable from "nothing exists", so a tool whose backing
  data is missing returns `ok=False` and names what is missing, and a tool
  that genuinely matched nothing returns `matched: 0` *and* a description of
  the population it searched.

Six of the declared tools have no data behind them. They are implemented as
contracts that say so — `get_page_metadata` (no page index has been built),
`check_document_authorization` (no authorization records exist),
`get_compliance_context` (`compliance_item` is empty),
`get_live_aircraft_position` and `get_airport_movements` (the online module,
standing rule 12 — nothing in this file opens a socket) and
`record_engineer_feedback` (no feedback table). `get_open_defects` is written
against the real `defect_closure` table and reports unavailable while that
table is empty, which it is in every database built so far: SDR records what
was reported, never whether it was closed.

Standing rule 1 is enforced structurally: no tool returns `task.body`. What
comes back is a locator — number, title, manual, revision, effectivity — and
the engineer goes to the controlled source for the procedure.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .. import config
from ..retrieval.search import (JASC_BOOST, TASK_BM25_WEIGHTS, Effectivity,
                                build_fts_match, fts_search,
                                resolve_applicability)
from ..stats import metrics
from ..ui.auth import ROLES, User
from .schemas import (EFFECTIVITY_NEEDS_CLOSING, NOT_CHECKED, UNRESOLVED_NOTICE,
                      evidence_id)


class ToolError(ValueError):
    """A tool refused its input.

    All three failures below are catchable as `ToolError` so a dispatcher can
    fail closed on any of them in one place, but they are separate classes
    because they mean different things and deserve different answers: an
    unknown name is a bug in the caller, a denied call is a policy decision
    about a real person, and a bad argument is something the model can fix and
    retry.
    """


class UnknownTool(ToolError):
    """No tool by that name is registered."""


class PermissionDenied(ToolError):
    """The signed-in user's role does not carry the required permission."""


# How stale a source may be before the phrase stops meaning anything. Kept as
# a literal because it is what the brief for a tool result asks for: a
# timestamp, or the fact that there is no timestamp because the data shipped
# with the application.
BUNDLED = "bundled/offline"
NO_TIMESTAMP = "unknown — the source records no import timestamp"

# Case narratives are the one thing a model is allowed to read (standing rule
# 3), and 300 characters is the same budget `retrieval.search` already uses.
SNIPPET = 300

# The caveat that belongs on every case-base answer. SDR is a
# reportable-occurrence sample under 14 CFR 121.703/145.221 and systematically
# excludes the low-drama removals, so "no cases" here never means "no defects"
# (PLAN §1.3). Attached to the data rather than left for the caller to
# remember, because the caller that forgets is the one that matters.
SDR_CAVEAT = ("the case base is the FAA SDR reportable-occurrence sample, "
              "which excludes routine removals — absence of a case here is "
              "not evidence that nothing happened")

# A maintenance message code as `mmsg.code` spells it: 33-11002.
FAULT_CODE = re.compile(r"\b\d{2}-\d{5}\b")

_JSON_TYPES: dict[str, dict] = {
    "text": {"type": "string"},
    "integer": {"type": "integer"},
    "boolean": {"type": "boolean"},
    "text_list": {"type": "array", "items": {"type": "string"}},
    "integer_list": {"type": "array", "items": {"type": "integer"}},
}


@dataclass(frozen=True)
class ToolResult:
    """One tool call's outcome, with everything needed to audit it.

    `missing` is what separates "this tool cannot answer" from "this tool
    answered and the answer was nothing". Both have `ok=False` only in the
    first case; the second is `ok=True` with `data['matched'] == 0` and a
    description of what was searched.
    """

    ok: bool
    tool: str
    data: dict = field(default_factory=dict)
    source: str = ""
    freshness: str = ""
    error: str = ""
    missing: tuple[str, ...] = ()
    truncated: bool = False

    @classmethod
    def success(cls, tool: str, data: dict, *, source: str, freshness: str,
                truncated: bool = False) -> "ToolResult":
        return cls(ok=True, tool=tool, data=data, source=source,
                   freshness=freshness, truncated=truncated)

    @classmethod
    def unavailable(cls, tool: str, *, missing: Sequence[str], detail: str,
                    source: str = "", freshness: str = BUNDLED) -> "ToolResult":
        """No data behind this tool, and the answer says exactly which data."""
        return cls(ok=False, tool=tool, source=source, freshness=freshness,
                   missing=tuple(missing),
                   error=f"unavailable — {detail}",
                   data={"unavailable": True, "missing": list(missing),
                         "detail": detail})

    @classmethod
    def failure(cls, tool: str, error: str, *, source: str = "",
                freshness: str = "") -> "ToolResult":
        return cls(ok=False, tool=tool, error=error, source=source,
                   freshness=freshness)

    def describe(self) -> str:
        if self.ok:
            return f"{self.tool}: ok · {self.source} · {self.freshness}"
        return f"{self.tool}: {self.error}"


@dataclass(frozen=True)
class Param:
    """One named argument, and everything needed to validate and publish it."""

    name: str
    kind: str
    description: str
    required: bool = False
    default: Any = None
    minimum: int | None = None
    maximum: int | None = None
    max_items: int | None = None

    def json_schema(self) -> dict:
        schema = dict(_JSON_TYPES[self.kind])
        schema["description"] = self.description
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        if self.max_items is not None:
            schema["maxItems"] = self.max_items
        return schema


@dataclass(frozen=True)
class ToolSpec:
    """A tool as the registry knows it: contract, permission and handler."""

    name: str
    description: str
    permission: str
    params: tuple[Param, ...]
    handler: Callable[..., ToolResult]
    availability: str = "backed by the local database"

    def definition(self) -> dict:
        """A tool-calling definition.

        `permission` and `availability` are for the caller's own filtering and
        should be dropped before these go to a provider's API — a stray key in
        a tool definition is a 400 from some endpoints.
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": {p.name: p.json_schema() for p in self.params},
                "required": [p.name for p in self.params if p.required],
                "additionalProperties": False,
            },
            "permission": self.permission,
            "availability": self.availability,
        }


# ── argument validation ─────────────────────────────────────────────────

def _validate(spec: ToolSpec, arguments: Mapping | None) -> dict:
    """Check the arguments against the spec, or raise `ToolError`.

    Nothing is coerced. `"5"` is not `5`, an unknown argument is not ignored,
    and a limit above the documented maximum is refused rather than reduced.
    """
    args = arguments if arguments is not None else {}
    if not isinstance(args, Mapping):
        raise ToolError(f"{spec.name}: arguments must be an object, got "
                        f"{type(args).__name__}")
    known = {p.name: p for p in spec.params}
    unknown = sorted(set(args) - set(known))
    if unknown:
        raise ToolError(
            f"{spec.name}: unknown argument(s) {', '.join(unknown)}; "
            f"accepts {', '.join(sorted(known))}")

    out: dict = {}
    for param in spec.params:
        if param.name not in args or args[param.name] is None:
            if param.required:
                raise ToolError(f"{spec.name}: {param.name} is required")
            out[param.name] = param.default
            continue
        out[param.name] = _check(spec.name, param, args[param.name])
    return out


def _check(tool: str, param: Param, value: Any) -> Any:
    name = param.name
    if param.kind == "text":
        if not isinstance(value, str):
            raise ToolError(f"{tool}: {name} must be text, got "
                            f"{type(value).__name__}")
        value = value.strip()
        if param.required and not value:
            raise ToolError(f"{tool}: {name} is empty")
        return value
    if param.kind == "integer":
        # `bool` is an `int` in Python; a caller passing True for a limit has
        # made a mistake worth telling them about.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError(f"{tool}: {name} must be a whole number, got "
                            f"{type(value).__name__}")
        if param.minimum is not None and value < param.minimum:
            raise ToolError(f"{tool}: {name}={value} is below the minimum "
                            f"of {param.minimum}")
        if param.maximum is not None and value > param.maximum:
            raise ToolError(
                f"{tool}: {name}={value} is above the maximum of "
                f"{param.maximum}. Ask for {param.maximum} or fewer — the "
                f"result is not silently truncated to hide the difference")
        return value
    if param.kind == "boolean":
        if not isinstance(value, bool):
            raise ToolError(f"{tool}: {name} must be true or false, got "
                            f"{type(value).__name__}")
        return value

    item_kind = int if param.kind == "integer_list" else str
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ToolError(f"{tool}: {name} must be a list, got "
                        f"{type(value).__name__}")
    items = list(value)
    if param.required and not items:
        raise ToolError(f"{tool}: {name} is an empty list")
    if param.max_items is not None and len(items) > param.max_items:
        raise ToolError(f"{tool}: {name} has {len(items)} entries; at most "
                        f"{param.max_items} may be asked for at once")
    for i, item in enumerate(items):
        if item_kind is int and (isinstance(item, bool)
                                 or not isinstance(item, int)):
            raise ToolError(f"{tool}: {name}[{i}] must be a whole number, "
                            f"got {type(item).__name__}")
        if item_kind is str and not isinstance(item, str):
            raise ToolError(f"{tool}: {name}[{i}] must be text, got "
                            f"{type(item).__name__}")
    return items


def permissions_of(user: User | None) -> frozenset[str]:
    """What this user may do, read off the same `ROLES` table login uses.

    An unknown role yields nothing rather than everything. Failing closed here
    matters more than anywhere else in the file: this is the function that
    decides whether a model gets to read a record.
    """
    if user is None:
        return frozenset()
    role = getattr(user, "role", "") or ""
    return frozenset(p.strip() for p in ROLES.get(role, "").split(",")
                     if p.strip())


# ── the registry ────────────────────────────────────────────────────────

class ToolRegistry:
    """Every tool a model may call, over one read-only connection.

    The connection is opened `mode=ro` with `query_only=ON`. That is the
    contract, not a precaution — `data/aivionics.db` is the department's case
    base and nothing in the model path has any business writing to it.
    """

    def __init__(self, con: sqlite3.Connection | None = None, *,
                 db_path: Path | str | None = None,
                 searcher: Any = None) -> None:
        self._con = con
        self._db_path = Path(db_path) if db_path else Path(config.DB_PATH)
        # The Phase 2 hybrid engine, when the caller has one. Without it the
        # task tool runs FTS5/BM25 only — a real retrieval path over real
        # rows, just without the dense channel, and it says so in `source`.
        self.searcher = searcher
        self._airplanes: dict[str, dict] | None = None
        self._manuals: list[dict] | None = None
        self._specs: dict[str, ToolSpec] = {s.name: s for s in self._build()}

    # ── connection ──────────────────────────────────────────────────────
    def connection(self) -> sqlite3.Connection | None:
        if self._con is None:
            if not self._db_path.exists():
                return None
            try:
                con = sqlite3.connect(
                    f"file:{self._db_path.as_posix()}?mode=ro", uri=True,
                    timeout=30.0, check_same_thread=False)
                con.execute("PRAGMA query_only=ON")
                self._con = con
            except sqlite3.Error:
                return None
        return self._con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    # ── dispatch ────────────────────────────────────────────────────────
    def describe(self, user: User | None = None) -> list[dict]:
        """Tool definitions, optionally narrowed to what `user` may call.

        Offering a model a tool it will be refused on wastes a turn and
        teaches it nothing, so a caller with a user in hand should pass it.
        """
        allowed = permissions_of(user) if user is not None else None
        return [spec.definition() for spec in self._specs.values()
                if allowed is None or spec.permission in allowed]

    def call(self, name: str, arguments: Mapping | None,
             user: User | None) -> ToolResult:
        """Dispatch one call. Raises on an unknown name, a denied permission
        or a bad argument; every other failure comes back as a `ToolResult`
        with `ok=False`, because a missing table is a state and not a crash."""
        spec = self._specs.get(name)
        if spec is None:
            raise UnknownTool(
                f"no tool named {name!r}; registered: "
                f"{', '.join(sorted(self._specs))}")
        held = permissions_of(user)
        if spec.permission not in held:
            role = getattr(user, "role", None) or "no user"
            raise PermissionDenied(
                f"{name} requires the {spec.permission!r} permission; role "
                f"{role!r} holds {', '.join(sorted(held)) or 'nothing'}")
        kwargs = _validate(spec, arguments)
        try:
            return spec.handler(**kwargs)
        except sqlite3.Error as exc:
            return ToolResult.failure(name, f"database error — {exc}")

    # ── shared lookups ──────────────────────────────────────────────────
    def _has_rows(self, table: str) -> bool:
        """Cheap existence probe. Never `COUNT(*)`: `defect` is millions of
        rows in the production database and the answer needed here is only
        whether the table has anything in it at all."""
        con = self.connection()
        if con is None:
            return False
        try:
            return con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
        except sqlite3.Error:
            return False

    def manuals(self) -> list[dict]:
        con = self.connection()
        if con is None:
            return []
        if self._manuals is None:
            keys = ("manual_id", "oem", "aircraft_type", "manual_type",
                    "doc_standard", "revision", "revision_date", "ingested_at")
            self._manuals = [
                dict(zip(keys, row)) for row in con.execute(
                    "SELECT id, oem, aircraft_type, manual_type, doc_standard,"
                    " revision, revision_date, ingested_at FROM manual"
                    " WHERE is_current=1 ORDER BY id")]
        return self._manuals

    def _manual_line(self) -> str:
        rows = self.manuals()
        if not rows:
            return "no manual is ingested"
        return "; ".join(f"{m['manual_type']} rev {m['revision'] or 'unknown'} "
                         f"({m['aircraft_type']}, manual_id {m['manual_id']})"
                         for m in rows)

    def _manual_freshness(self) -> str:
        stamps = [m["ingested_at"] for m in self.manuals() if m["ingested_at"]]
        return max(stamps) if stamps else NO_TIMESTAMP

    def _case_freshness(self) -> str:
        """The newest report in the case base, not the wall clock.

        The corpus is a static download; `latest_report_date` is the same
        anchor the reliability statistics measure their windows back from, and
        using today's date here would claim a currency the data does not have.
        """
        con = self.connection()
        if con is None:
            return NO_TIMESTAMP
        newest = metrics.latest_report_date(con)
        return f"newest report {newest}" if newest else NO_TIMESTAMP

    def _airplane_map(self) -> dict[str, dict]:
        con = self.connection()
        if con is None:
            return {}
        if self._airplanes is None:
            self._airplanes = {
                r[0]: {"model": r[1], "msn": r[2], "line_no": r[3], "tail": r[4]}
                for r in con.execute(
                    "SELECT eff_ref, model, msn, line_no, tail"
                    "  FROM effectivity_airplane")}
        return self._airplanes

    def _task_rows(self, ids: Sequence[int], eff: Effectivity | None,
                   scores: Mapping[int, float],
                   channels: Mapping[int, str]) -> list[dict]:
        """Locators for the given task ids. **Never `task.body`** — standing
        rule 1: this tool indexes into controlled data, it does not reproduce
        it, and a body that reached a model would be a body that reached a
        screen outside the controlled source."""
        con = self.connection()
        if con is None or not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        airplanes = self._airplane_map()
        rows = []
        for row in con.execute(
            f"SELECT t.id, t.task_number, t.title, t.function_code,"
            f"       t.ata_chapter, t.ata_section, t.embed_text,"
            f"       t.effectivity_raw, t.applic_refs, t.catalogue_only,"
            f"       t.has_warning, t.has_caution,"
            f"       m.id, m.manual_type, m.revision, m.aircraft_type"
            f"  FROM task t JOIN manual m ON m.id = t.manual_id"
            f" WHERE t.id IN ({placeholders})", list(ids)
        ):
            applic = resolve_applicability(
                {"applic_refs": row[8], "effectivity_raw": row[7]},
                eff, airplanes)
            rows.append({
                "task_id": row[0],
                "task_number": row[1],
                "evidence_id": evidence_id("task", row[1]),
                "title": row[2],
                "function_code": row[3],
                "ata_chapter": row[4],
                "ata_section": row[5],
                "hierarchy": row[6],
                "catalogue_only": bool(row[9]),
                "has_warning": bool(row[10]),
                "has_caution": bool(row[11]),
                "manual_id": row[12],
                "manual_type": row[13],
                "revision": row[14],
                "aircraft_type": row[15],
                "effectivity_result": applic,
                "effectivity_note": (UNRESOLVED_NOTICE
                                     if applic in EFFECTIVITY_NEEDS_CLOSING
                                     else ""),
                "retrieval_evidence": {
                    "channel": channels.get(row[0], "fts"),
                    "score": round(float(scores.get(row[0], 0.0)), 4),
                },
            })
        rows.sort(key=lambda r: -r["retrieval_evidence"]["score"])
        return rows

    def _case_rows(self, ids: Sequence[int]) -> dict[int, dict]:
        """One row per defect id, with its actions and its finding.

        Case narratives are the one text a model may read (standing rule 3),
        and they are truncated to `SNIPPET` so a tool result cannot become a
        way of streaming the corpus into a context window.
        """
        con = self.connection()
        if con is None or not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        out: dict[int, dict] = {}
        for row in con.execute(
            f"SELECT d.id, d.aircraft_tail, d.reported_at, d.ata_ref,"
            f"       d.fault_code, substr(COALESCE(d.defect_text,''),1,{SNIPPET}),"
            f"       substr(COALESCE(d.rectification_text,''),1,{SNIPPET}),"
            f"       d.source, d.sdr_year, d.tool_assisted"
            f"  FROM defect d WHERE d.id IN ({placeholders})", list(ids)
        ):
            out[row[0]] = {
                "defect_id": row[0],
                "evidence_id": evidence_id("defect", row[0]),
                "tail": row[1] or "unknown",
                "reported_at": row[2],
                "ata_chapter": row[3],
                "fault_code": row[4],
                "symptom": " ".join((row[5] or "").split()),
                "action_text": " ".join((row[6] or "").split()),
                "source": row[7],
                "sdr_year": row[8],
                # Standing rule 7: a narrative written with tool assistance is
                # flagged at write time, and anything reading it downstream has
                # to be able to see the flag or tomorrow's labels are the
                # system's own echo.
                "tool_assisted": bool(row[9]),
                "actions": [],
                "finding": "not recorded",
            }
        for row in con.execute(
            f"SELECT defect_id, action_type, part_name, part_number, task_number"
            f"  FROM defect_action WHERE defect_id IN ({placeholders})", list(ids)
        ):
            case = out.get(row[0])
            if case is not None:
                case["actions"].append({
                    "action_type": row[1], "part_name": row[2],
                    "part_number": row[3], "task_number": row[4]})
        for row in con.execute(
            f"SELECT defect_id, finding_type, finding_text"
            f"  FROM defect_finding WHERE defect_id IN ({placeholders})", list(ids)
        ):
            case = out.get(row[0])
            if case is not None:
                # `found:` is absent, not empty. SDR carries what was reported
                # and what was replaced; the shop teardown finding is not in
                # this data, and a blank would read as "nothing was found".
                case["finding"] = row[1]
                case["finding_text"] = " ".join((row[2] or "").split())
        return out

    # ── tools backed by real data ───────────────────────────────────────
    def search_manual_tasks(self, query: str, ata_chapter: str | None = None,
                            manual_type: str | None = None,
                            tail: str | None = None,
                            limit: int = 5) -> ToolResult:
        name = "search_manual_tasks"
        con = self.connection()
        if con is None:
            return ToolResult.unavailable(
                name, missing=["aivionics.db"],
                detail=f"no database at {self._db_path}")
        if not self._has_rows("task"):
            return ToolResult.unavailable(
                name, missing=["task"],
                detail="the task index is empty — run the Phase 0/1 ingest")

        scores: dict[int, float] = {}
        channels: dict[int, str] = {}

        # A maintenance message code routes to its FIM tasks directly. This is
        # the one exact path in the corpus: `mmsg_task` is OEM metadata, not a
        # retrieval guess, so it leads rather than competes.
        for code in FAULT_CODE.findall(query or ""):
            routed = [r[0] for r in con.execute(
                "SELECT t.id FROM mmsg_task mt JOIN task t"
                "  ON t.task_number = mt.task_number"
                " WHERE mt.code = ? ORDER BY mt.seq", (code,))]
            for task_id in routed:
                scores[task_id] = max(scores.get(task_id, 0.0), 1.0)
                channels[task_id] = f"mmsg:{code}"

        if self.searcher is not None:
            # The Phase 2 hybrid engine: FTS5 and dense cosine, merged with
            # query-dependent weights. `ata_chapter` goes in as the JASC hint,
            # which boosts and never gates — reporter-entered codes are
            # miscoded at the 22/24/27/31/34 boundaries and a hard filter puts
            # the right task permanently out of reach (PLAN 2.5).
            run = self.searcher.search(query, kind="task", jasc=ata_chapter,
                                       top_k=limit * 4, rerank=False)
            for result in run.ranked:
                if result.id is None:
                    continue
                scores.setdefault(result.id, result.score)
                channels.setdefault(
                    result.id, str(result.provenance.get("channels", "hybrid")))
            engine = "hybrid retrieval (FTS5 + dense)"
        else:
            match = build_fts_match(query)
            hits = fts_search(con, "task_fts", match, limit * 8,
                              TASK_BM25_WEIGHTS) if match else {}
            top = max(hits.values()) if hits else 0.0
            for task_id, score in hits.items():
                scores.setdefault(task_id, (score / top) if top else 0.0)
                channels.setdefault(task_id, "fts")
            engine = "FTS5/BM25 only — no vector index was supplied"

        eff = Effectivity(tail=tail) if tail else None
        rows = self._task_rows(sorted(scores), eff, scores, channels)

        # The JASC hint as a boost, applied here for the FTS-only path so both
        # paths honour PLAN 2.5 the same way.
        if ata_chapter and self.searcher is None:
            hint = ata_chapter.strip()[:2]
            for row in rows:
                if row["ata_chapter"] == hint:
                    row["retrieval_evidence"]["score"] = round(
                        row["retrieval_evidence"]["score"] + JASC_BOOST, 4)
                    row["retrieval_evidence"]["jasc_boost"] = JASC_BOOST
            rows.sort(key=lambda r: -r["retrieval_evidence"]["score"])

        if manual_type:
            rows = [r for r in rows
                    if (r["manual_type"] or "").upper() == manual_type.upper()]

        page = rows[:limit]
        return ToolResult.success(
            name,
            {"matched": len(rows), "returned": len(page), "limit": limit,
             "results": page,
             "searched": f"task index — {self._manual_line()}",
             "engine": engine,
             "jasc_hint": ata_chapter or None,
             "note": ("no task matched this query in the index described "
                      "under 'searched'" if not rows else "")},
            source=f"task index ({self._manual_line()})",
            freshness=self._manual_freshness(),
            truncated=len(rows) > len(page))

    def search_similar_cases(self, symptom: str, ata_chapter: str | None = None,
                             tail: str | None = None,
                             limit: int = 5) -> ToolResult:
        name = "search_similar_cases"
        con = self.connection()
        if con is None or not self._has_rows("defect"):
            return ToolResult.unavailable(
                name, missing=["defect"],
                detail="the case base is empty — run the Phase 0 SDR ingest")

        match = build_fts_match(symptom)
        hits = fts_search(con, "case_fts", match, limit * 8) if match else {}
        rows = list(self._case_rows(sorted(hits)).values())
        top = max(hits.values()) if hits else 0.0
        hint = (ata_chapter or "").strip()[:2] or None
        for row in rows:
            score = (hits.get(row["defect_id"], 0.0) / top) if top else 0.0
            if hint and row["ata_chapter"] == hint:
                score += JASC_BOOST          # a boost, never a gate (PLAN 2.5)
            row["score"] = round(score, 4)
        if tail:
            rows = [r for r in rows if (r["tail"] or "").upper() == tail.upper()]
        rows.sort(key=lambda r: -r["score"])

        page = rows[:limit]
        return ToolResult.success(
            name,
            {"matched": len(rows), "returned": len(page), "limit": limit,
             "results": page, "caveat": SDR_CAVEAT,
             "searched": "case_fts over defect narratives",
             "jasc_hint": hint,
             "note": ("no prior case matched this symptom in the case base "
                      "described under 'searched'" if not rows else "")},
            source="FAA SDR mined case base (defect)",
            freshness=self._case_freshness(),
            truncated=len(rows) > len(page))

    def get_case_evidence(self, case_ids: list[int]) -> ToolResult:
        name = "get_case_evidence"
        if self.connection() is None or not self._has_rows("defect"):
            return ToolResult.unavailable(
                name, missing=["defect"], detail="the case base is empty")
        found = self._case_rows(case_ids)
        # Requested ids that do not exist are named rather than dropped. A
        # caller that asked for five cases and silently got three would draw a
        # conclusion from a set it never knew was incomplete.
        absent = [i for i in case_ids if i not in found]
        return ToolResult.success(
            name,
            {"matched": len(found), "requested": len(case_ids),
             "results": [found[i] for i in case_ids if i in found],
             "not_found": absent, "caveat": SDR_CAVEAT},
            source="FAA SDR mined case base (defect, defect_action, "
                   "defect_finding)",
            freshness=self._case_freshness())

    def get_aircraft_history(self, tail: str, since: str | None = None,
                             limit: int = 10) -> ToolResult:
        name = "get_aircraft_history"
        con = self.connection()
        if con is None or not self._has_rows("defect"):
            return ToolResult.unavailable(
                name, missing=["defect"], detail="the case base is empty")
        clauses, args = ["d.aircraft_tail = ?"], [tail]
        if since:
            clauses.append("d.reported_at >= ?")
            args.append(since)
        ids = [r[0] for r in con.execute(
            f"SELECT d.id FROM defect d WHERE {' AND '.join(clauses)}"
            f" ORDER BY d.reported_at DESC LIMIT ?", (*args, limit + 1))]
        rows = self._case_rows(ids[:limit])
        register = con.execute(
            "SELECT tail, type, msn, line_number, year_built, total_time_hrs,"
            "       total_cycles FROM aircraft WHERE tail = ?", (tail,)).fetchone()
        return ToolResult.success(
            name,
            {"tail": tail,
             "returned": len(rows), "limit": limit,
             "results": [rows[i] for i in ids[:limit] if i in rows],
             "register": (dict(zip(
                 ("tail", "type", "msn", "line_number", "year_built",
                  "total_time_hrs", "total_cycles"), register))
                 if register else None),
             "register_note": ("" if register else
                               f"tail {tail} is not in the aircraft register; "
                               f"history below comes from reported defects only"),
             "caveat": SDR_CAVEAT,
             "note": (f"no reported defect for tail {tail}"
                      + (f" since {since}" if since else "") if not ids else "")},
            source="FAA SDR mined case base (defect) + aircraft register",
            freshness=self._case_freshness(),
            truncated=len(ids) > limit)

    def get_open_defects(self, tail: str | None = None,
                         limit: int = 10) -> ToolResult:
        """Defects with no completed closure record.

        Written against the real `defect_closure` table, and unavailable while
        that table is empty — which it is in every database built so far. SDR
        records what was reported and what was replaced; whether the item was
        ever closed comes from an operator tech log, and no operator data has
        been imported (PLAN §1.3). Returning every defect as "open" because
        nothing says otherwise would be a fabrication with the shape of a fact.
        """
        name = "get_open_defects"
        if self.connection() is None or not self._has_rows("defect"):
            return ToolResult.unavailable(
                name, missing=["defect"], detail="the case base is empty")
        if not self._has_rows("defect_closure"):
            return ToolResult.unavailable(
                name, missing=["defect_closure"],
                detail="no closure state has been imported, so open and closed "
                       "cannot be told apart — this needs an operator tech-log "
                       "import, which SDR does not provide",
                source="defect_closure", freshness=self._case_freshness())
        con = self.connection()
        clauses = ["NOT EXISTS(SELECT 1 FROM defect_closure c"
                   " WHERE c.defect_id = d.id AND c.complete = 1)"]
        args: list = []
        if tail:
            clauses.append("d.aircraft_tail = ?")
            args.append(tail)
        ids = [r[0] for r in con.execute(
            f"SELECT d.id FROM defect d WHERE {' AND '.join(clauses)}"
            f" ORDER BY d.reported_at DESC LIMIT ?", (*args, limit + 1))]
        rows = self._case_rows(ids[:limit])
        return ToolResult.success(
            name,
            {"returned": len(rows), "limit": limit, "tail": tail,
             "results": [rows[i] for i in ids[:limit] if i in rows],
             "caveat": SDR_CAVEAT,
             "note": "no defect without a completed closure record" if not ids else ""},
            source="defect + defect_closure",
            freshness=self._case_freshness(),
            truncated=len(ids) > limit)

    def get_repeat_defects(self, tail: str | None = None,
                           window_days: int = metrics.DEFAULT_WINDOW_DAYS,
                           limit: int = 10) -> ToolResult:
        """Repeat pairs from `repeat_norm` — the evidence behind every rate.

        Delegates to `stats.metrics.repeat_events`, which routes through
        `db.stats_guard`, so this path cannot become a way around the rule
        that aggregates never touch the note table (standing rule 6).
        """
        name = "get_repeat_defects"
        if self.connection() is None or not self._has_rows("repeat_norm"):
            return ToolResult.unavailable(
                name, missing=["repeat_norm"],
                detail="normalised repeat linkage has not been built — run "
                       "scripts/phase3.py")
        events = metrics.repeat_events(self.connection(), tail=tail,
                                       window_days=window_days, limit=limit + 1)
        for event in events:
            event["symptom"] = " ".join((event.get("symptom") or "")
                                        .split())[:SNIPPET]
            event["evidence_id"] = evidence_id("repeat", event["defect_id"])
        page = events[:limit]
        return ToolResult.success(
            name,
            {"returned": len(page), "limit": limit, "window_days": window_days,
             "tail": tail, "results": page, "caveat": SDR_CAVEAT,
             "metric": metrics.METRIC_SHORT.format(days=window_days),
             "note": (f"no repeat inside {window_days} days"
                      + (f" for tail {tail}" if tail else "")
                      if not events else "")},
            source="repeat_norm (normalised Phase 3.2 linkage)",
            freshness=self._case_freshness(),
            truncated=len(events) > limit)

    def get_manual_metadata(self, manual_type: str | None = None) -> ToolResult:
        name = "get_manual_metadata"
        rows = [m for m in self.manuals()
                if not manual_type
                or (m["manual_type"] or "").upper() == manual_type.upper()]
        if not self.manuals():
            return ToolResult.unavailable(
                name, missing=["manual"],
                detail="no manual has been ingested — run the Phase 1 catalogue")
        con = self.connection()
        for row in rows:
            row["coverage"] = [
                {"ata_chapter": c[0], "toc_count": c[1],
                 "extracted_count": c[2], "pct": c[3]}
                for c in con.execute(
                    "SELECT ata_chapter, toc_count, extracted_count, pct"
                    "  FROM coverage WHERE manual_id = ?"
                    " ORDER BY ata_chapter", (row["manual_id"],))]
            row["evidence_id"] = evidence_id("manual", row["manual_id"])
        return ToolResult.success(
            name,
            {"matched": len(rows), "results": rows,
             "note": (f"no current manual of type {manual_type}"
                      if manual_type and not rows else "")},
            source="manual + coverage",
            freshness=self._manual_freshness())

    def check_effectivity(self, task_number: str, tail: str | None = None,
                          msn: str | None = None,
                          line_number: str | None = None) -> ToolResult:
        """Whether a task applies to an airframe. Fails closed (standing rule 8).

        Anything that cannot be positively resolved comes back as `unresolved`
        or `not_checked` carrying the same sentence the print path uses, never
        as a clean answer.
        """
        name = "check_effectivity"
        con = self.connection()
        if con is None or not self._has_rows("task"):
            return ToolResult.unavailable(
                name, missing=["task"], detail="the task index is empty")
        rows = con.execute(
            "SELECT t.id, t.effectivity_raw, t.applic_refs, m.id, m.manual_type,"
            "       m.revision FROM task t JOIN manual m ON m.id = t.manual_id"
            " WHERE t.task_number = ?", (task_number,)).fetchall()
        if not rows:
            return ToolResult.failure(
                name, f"task {task_number} is not in the index — it cannot be "
                      f"checked, and it must not be recommended",
                source="task", freshness=self._manual_freshness())
        eff = (Effectivity(msn=msn, tail=tail, line_number=line_number)
               if (msn or tail or line_number) else None)
        airplanes = self._airplane_map()
        results = []
        for row in rows:
            state = resolve_applicability(
                {"applic_refs": row[2], "effectivity_raw": row[1]},
                eff, airplanes)
            results.append({
                "task_number": task_number,
                "evidence_id": evidence_id("task", task_number),
                "manual_id": row[3], "manual_type": row[4], "revision": row[5],
                "effectivity_result": state,
                "effectivity_raw": (row[1] or "")[:SNIPPET] or None,
                "notice": (UNRESOLVED_NOTICE
                           if state in EFFECTIVITY_NEEDS_CLOSING else ""),
            })
        return ToolResult.success(
            name,
            {"asked_for": {"tail": tail, "msn": msn,
                           "line_number": line_number},
             "results": results,
             "airframes_known": len(airplanes),
             "note": ("no airframe was given, so applicability was not checked"
                      if eff is None else "")},
            source="task.effectivity_raw + effectivity_airplane",
            freshness=self._manual_freshness())

    # ── declared contracts with no data behind them ─────────────────────
    #
    # Each returns `ok=False` and names the missing thing. None of them
    # fabricates a plausible answer, and none returns an empty success that
    # could be read as "nothing exists".

    def get_page_metadata(self, manual_id: int, source_page: int) -> ToolResult:
        return ToolResult.unavailable(
            "get_page_metadata",
            missing=["page index (source page ↔ printed page label ↔ task)"],
            detail="no page index has been built. Task bodies were extracted "
                   "by TASK…END OF TASK pairing, which records no page "
                   "boundaries, so there is nothing that can map a task to a "
                   "printed page — and a page number that cannot be verified "
                   "against a revision sends an engineer to the wrong sheet")

    def check_document_authorization(self, manual_id: int,
                                     task_number: str) -> ToolResult:
        return ToolResult.unavailable(
            "check_document_authorization",
            missing=["authorization records"],
            detail="the database holds no record of which documents are "
                   "approved for use. Part-145 145.A.45 requires data in use "
                   "to be current and from the approved source; a local "
                   "extraction is uncontrolled by definition, so this must "
                   "stay unanswerable rather than answer 'authorized'")

    def get_compliance_context(self, tail: str, limit: int = 10) -> ToolResult:
        name = "get_compliance_context"
        if not self._has_rows("compliance_item"):
            return ToolResult.unavailable(
                name, missing=["compliance_item"],
                detail="no compliance export has been imported. The CAMO is "
                       "the legal record and this register is a mirror of it "
                       "(standing rule 2); an empty mirror must read as "
                       "'not imported', never as 'nothing is due'")
        con = self.connection()
        keys = ("id", "kind", "ref", "description", "mel_category", "due_date",
                "due_hours", "due_cycles", "source_system", "imported_at")
        rows = [dict(zip(keys, r)) for r in con.execute(
            "SELECT id, kind, ref, description, mel_category, due_date,"
            "       due_hours, due_cycles, source_system, imported_at"
            "  FROM compliance_item WHERE aircraft_tail = ?"
            " ORDER BY due_date LIMIT ?", (tail, limit + 1))]
        page = rows[:limit]
        stamps = [r["imported_at"] for r in page if r["imported_at"]]
        return ToolResult.success(
            name,
            {"tail": tail, "returned": len(page), "limit": limit,
             "results": page,
             "advisory": "this register mirrors the CAMO and is never the "
                         "authoritative record — verify before acting",
             "note": f"no compliance item on file for tail {tail}" if not rows else ""},
            source="compliance_item (imported mirror)",
            freshness=max(stamps) if stamps else NO_TIMESTAMP,
            truncated=len(rows) > limit)

    def get_live_aircraft_position(self, tail: str) -> ToolResult:
        return ToolResult.unavailable(
            "get_live_aircraft_position",
            missing=["the online operations module (aivionics.ops)"],
            detail="live tracking lives behind the `online_enabled` setting "
                   "with its own network layer, cache and failure state "
                   "(standing rule 12). Nothing in the tool registry opens a "
                   "socket, so this tool declares the contract and defers")

    def get_airport_movements(self, icao: str) -> ToolResult:
        return ToolResult.unavailable(
            "get_airport_movements",
            missing=["the online operations module (aivionics.ops)"],
            detail="airport movements come from the allow-listed online "
                   "client, which is a separate module behind the "
                   "`online_enabled` setting (standing rule 12)")

    def record_engineer_feedback(self, defect_id: int, verdict: str,
                                 comment: str | None = None) -> ToolResult:
        return ToolResult.unavailable(
            "record_engineer_feedback",
            missing=["an engineer feedback table"],
            detail="there is nowhere to write this. The registry's connection "
                   "is opened read-only by design, and feedback that becomes "
                   "label data has to be flagged as tool-assisted at write "
                   "time (standing rule 7) — which is a schema decision, not "
                   "something this tool may improvise")

    # ── the catalogue ───────────────────────────────────────────────────
    def _build(self) -> list[ToolSpec]:
        limit = lambda mx, dflt: Param(                       # noqa: E731
            "limit", "integer", f"How many rows to return. Maximum {mx}; a "
            f"larger value is refused, not truncated.", default=dflt,
            minimum=1, maximum=mx)
        offline = "declared contract — no data behind it yet"
        return [
            ToolSpec(
                "search_manual_tasks",
                "Find manual task locators (number, title, manual, revision) "
                "for a symptom, fault code or task number. Returns locators "
                "only — never procedure text.",
                "read",
                (Param("query", "text", "The symptom, fault code or task "
                       "number to search for.", required=True),
                 Param("ata_chapter", "text", "Two-digit ATA chapter used as a "
                       "hint. It boosts matching tasks; it never filters them "
                       "out, because reporter-entered codes are miscoded at "
                       "chapter boundaries."),
                 Param("manual_type", "text", "Restrict to one manual type, "
                       "e.g. AMM or FIM."),
                 Param("tail", "text", "Aircraft registration, so each result "
                       "carries its effectivity state."),
                 limit(25, 5)),
                self.search_manual_tasks),
            ToolSpec(
                "search_similar_cases",
                "Find prior defect cases whose reported symptom resembles this "
                "one. Returns what was reported and what was done, never a "
                "conclusion about what was correct.",
                "read",
                (Param("symptom", "text", "The reported symptom.", required=True),
                 Param("ata_chapter", "text", "Two-digit ATA chapter hint; "
                       "boosts, never filters."),
                 Param("tail", "text", "Restrict to one aircraft."),
                 limit(25, 5)),
                self.search_similar_cases),
            ToolSpec(
                "get_case_evidence",
                "Fetch the full evidence for specific defect ids: narrative, "
                "actions taken, and what was found if it was recorded.",
                "read",
                (Param("case_ids", "integer_list", "Defect ids returned by a "
                       "previous search. At most 10 at a time.",
                       required=True, max_items=10),),
                self.get_case_evidence),
            ToolSpec(
                "get_aircraft_history",
                "Reported defects for one tail, newest first, with the "
                "aircraft register entry when there is one.",
                "read",
                (Param("tail", "text", "Aircraft registration.", required=True),
                 Param("since", "text", "ISO date; only defects reported on or "
                       "after it."),
                 limit(50, 10)),
                self.get_aircraft_history),
            ToolSpec(
                "get_open_defects",
                "Defects with no completed closure record. Reports unavailable "
                "while no closure state has been imported.",
                "read",
                (Param("tail", "text", "Restrict to one aircraft."),
                 limit(50, 10)),
                self.get_open_defects),
            ToolSpec(
                "get_repeat_defects",
                "Repeat pairs: the same tail reporting the same ATA chapter "
                "again inside the window, with the similarity that linked them.",
                "read",
                (Param("tail", "text", "Restrict to one aircraft."),
                 Param("window_days", "integer", "Elapsed days within which a "
                       "second report counts as a repeat.",
                       default=metrics.DEFAULT_WINDOW_DAYS, minimum=1,
                       maximum=365),
                 limit(50, 10)),
                self.get_repeat_defects),
            ToolSpec(
                "get_manual_metadata",
                "Which manuals are ingested, at which revision, with per-"
                "chapter extraction coverage.",
                "read",
                (Param("manual_type", "text", "Restrict to one manual type."),),
                self.get_manual_metadata),
            ToolSpec(
                "check_effectivity",
                "Whether a task applies to a given airframe. Fails closed: "
                "anything not positively resolved comes back unresolved.",
                "read",
                (Param("task_number", "text", "The task to check.",
                       required=True),
                 Param("tail", "text", "Aircraft registration."),
                 Param("msn", "text", "Manufacturer serial number."),
                 Param("line_number", "text", "Production line number.")),
                self.check_effectivity),
            ToolSpec(
                "get_page_metadata",
                "Which printed page of which revision carries a task, and "
                "which pages must be read with it. No page index exists yet.",
                "read",
                (Param("manual_id", "integer", "The manual.", required=True),
                 Param("source_page", "integer", "The page in the source file.",
                       required=True, minimum=1)),
                self.get_page_metadata, offline),
            ToolSpec(
                "check_document_authorization",
                "Whether a document is approved for use. No authorization "
                "records exist.",
                "read",
                (Param("manual_id", "integer", "The manual.", required=True),
                 Param("task_number", "text", "The task.", required=True)),
                self.check_document_authorization, offline),
            ToolSpec(
                "get_compliance_context",
                "Open checkups, MEL deferrals and AD/SB items for a tail, from "
                "the imported CAMO mirror. Never the authoritative record.",
                "read",
                (Param("tail", "text", "Aircraft registration.", required=True),
                 limit(50, 10)),
                self.get_compliance_context),
            ToolSpec(
                "get_live_aircraft_position",
                "Where an aircraft is now. Belongs to the online module and is "
                "not reachable from here.",
                "read",
                (Param("tail", "text", "Aircraft registration.", required=True),),
                self.get_live_aircraft_position, offline),
            ToolSpec(
                "get_airport_movements",
                "Recent movements at an airport. Belongs to the online module "
                "and is not reachable from here.",
                "read",
                (Param("icao", "text", "ICAO airport code.", required=True),),
                self.get_airport_movements, offline),
            ToolSpec(
                "record_engineer_feedback",
                "Record an engineer's verdict on a suggestion. There is no "
                "feedback table and this connection is read-only.",
                # `notes` rather than `read`: writing engineering judgement
                # that becomes label data is an engineer's act, and the `admin`
                # role in the ROLES table deliberately does not carry it.
                "notes",
                (Param("defect_id", "integer", "The case being judged.",
                       required=True),
                 Param("verdict", "text", "yes, no, partial or unsure.",
                       required=True),
                 Param("comment", "text", "Optional free text.")),
                self.record_engineer_feedback, offline),
        ]
