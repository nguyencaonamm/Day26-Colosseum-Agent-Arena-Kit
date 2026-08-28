"""referee/adapters.py — the seam between referee/verify.py's Gate-1 output and
referee/ledger.py's fold input (CONTRACTS.md sections 5.2, 6.1-6.4;
ENGINE-REPORT.md's D-7).

THE DEFECT THIS FILE CLOSES
----------------------------
`verify_claims()` (referee/verify.py) returns ONE result dict per input claim,
each carrying exactly six keys: `causal_event, cls, detail, family, outcome,
weight` (see `VERIFY_ROW_FIELDS` below — verified against a REAL call at test
time, not just documented). `referee.ledger.ClaimOutcome` — what
`referee.ledger.fold_exchange` actually needs — wants `(cls, outcome, weight,
evidence, reasoning)`. Nobody wrote the seam between them: a caller who did
the "obvious" thing —

    ClaimOutcome(cls=row["cls"], outcome=row["outcome"], weight=row["weight"])

— would compile, would pass every existing test, and would silently:
  * drop `evidence` entirely (the dataclass default `()` fills the gap with no
    error — and CONTRACTS.md says the projector's cut-in shows the evidence
    event id, so this quietly kills the best spectator moment in the design)
  * carry no `reasoning` (there is only `detail`, a different key, never
    copied across)
  * never produce the `scaled` field CONTRACTS.md 5.2 requires on the L2
    `claim_outcome` event payload (`outcome, weight, scaled, reasoning`)

THE FIX — ONE EXPLICIT, TOTAL, TESTED CONVERSION
--------------------------------------------------
`verify_claims()`'s row alone cannot supply `evidence`: the ORIGINAL claim
dict a prosecutor submitted carries it (CONTRACTS.md 6.1's
`"evidence": ["evt:0412"]` list), but `verify_claims()`'s own return
list-comprehension (its very last statement) builds exactly six keys from its
internal `rows` and never copies `evidence` forward. So `claim_outcome_from_
verify_row` below takes BOTH: the original submitted claim AND its paired
`verify_claims()` result row — `verify_claims`'s own docstring guarantees
"one result dict per INPUT claim, in the SAME order," so pairing by index is
exact, never a guess.

Field-by-field, every source field on both sides is accounted for — mapped,
or explicitly dropped with a comment saying why (never silently):

  SOURCE (verify_claims() row) -> ClaimOutcome  | why
  ------------------------------------------------+--------------------------
  cls          -> cls                             | direct
  outcome      -> outcome                         | direct, EXCEPT "pending"
                                                    |   -- see PendingOutcomeError
  weight       -> weight                          | direct, except `None` (a
                                                    |   schema-invalid claim
                                                    |   never resolved a class
                                                    |   to look one up for) ->
                                                    |   `0`, matching
                                                    |   CONTRACTS.md 6.2's own
                                                    |   "rejected -> 0, logged"
  detail       -> reasoning                       | direct rename -- literally
                                                    |   the missing field D-7
                                                    |   names
  family       -> (consumed here, not carried)     | not a ClaimOutcome field;
                                                    |   it is derived-from-cls
                                                    |   bookkeeping `referee.
                                                    |   rubric.family_of` can
                                                    |   always recompute, so
                                                    |   carrying a second copy
                                                    |   forward would just be a
                                                    |   second, divergeable
                                                    |   source of truth
  causal_event -> (consumed here, not carried)      | not a ClaimOutcome field;
                                                    |   it is `referee.
                                                    |   detectors.
                                                    |   subtract_verified`'s
                                                    |   OWN netting key,
                                                    |   reconstructed BY that
                                                    |   function FROM
                                                    |   `evidence` — carrying
                                                    |   it as a second,
                                                    |   independently-computed
                                                    |   attribute on
                                                    |   ClaimOutcome would risk
                                                    |   the two ever disagreeing

  SOURCE (the ORIGINAL claim dict) -> ClaimOutcome | why
  ------------------------------------------------+--------------------------
  evidence     -> evidence                        | direct -- THE dropped field
  cls / expected / observed / argument             | already fully consumed BY
                                                    |   verify_claims to
                                                    |   PRODUCE `outcome`/
                                                    |   `detail`; carrying them
                                                    |   again into the ledger
                                                    |   would be a second copy
                                                    |   of already-decided
                                                    |   facts, which CONTRACTS.
                                                    |   md 5.1 forbids for the
                                                    |   ledger ("ALREADY-
                                                    |   DECIDED facts" only,
                                                    |   never source material
                                                    |   to re-derive from) —
                                                    |   explicitly NOT copied,
                                                    |   not silently forgotten

`outcome == "pending"` can never become a `ClaimOutcome`: CONTRACTS.md 6.2
defines exactly four wire outcomes, and referee/verify.py's own module
docstring says `"pending"` is "an internal fifth state, NEVER a final wire
outcome" — gate 2 (referee/adjudicate.py) must resolve it first.
`claim_outcome_from_verify_row` raises `PendingOutcomeError` (rather than
letting the row fall through to `ClaimOutcome.__post_init__`'s generic
`ValueError: outcome must be one of verified/unproven/false/rejected, got
'pending'`) so whoever wires `arena/exchange.py` gets a specific, actionable
message — LOUD, per this build's controlling rule: a fallback that cannot be
observed is a bug that cannot be found.

`_assert_verify_row_shape` re-checks `VERIFY_ROW_FIELDS` on every call (not
just once, unlike `referee.ledger`'s import-time detector check — this
function is cheap and runs per-claim regardless, so there is no reason to
skip the cheapest possible drift detector on any single call): if
`referee/verify.py` ever adds, removes, or renames a row key (CONTRACTS.md
5.2's `scaled` field is exactly the kind of addition that could happen again),
this raises immediately instead of silently under-mapping — which is exactly
how D-3's dead lookup went unnoticed for as long as it did.

Stdlib only. No I/O, no network, no randomness, no wall-clock — a pure
converter, the same discipline `referee/ledger.py` holds itself to.
"""

from __future__ import annotations

import dataclasses
from fractions import Fraction
from typing import Any, Mapping, Sequence

from kit.referee.ledger import ClaimOutcome

__all__ = [
    "VERIFY_ROW_FIELDS",
    "CLAIM_OUTCOME_FIELDS",
    "PendingOutcomeError",
    "VerifyRowShapeError",
    "claim_outcome_from_verify_row",
    "claim_outcomes_from_verify_result",
    "scaled_claim_outcome_event_payload",
]

#: The exact six keys `referee.verify.verify_claims` returns per row, TODAY —
#: see that function's own final `return [...]` statement. `tests/
#: test_adapters.py` calls the REAL function and asserts its rows' keys equal
#: this set exactly (not a subset check) — a genuine round-trip check, not
#: just a hardcoded assumption repeated in two files.
VERIFY_ROW_FIELDS: frozenset[str] = frozenset({"causal_event", "cls", "detail", "family", "outcome", "weight"})

#: The `referee.ledger.ClaimOutcome` field names this module writes to.
#: Self-checked against the real dataclass below (import-time, same idiom
#: `referee/ledger.py` already uses for `_FALLBACK_WEIGHTS`) so this constant
#: cannot silently drift from what `ClaimOutcome` actually accepts.
CLAIM_OUTCOME_FIELDS: frozenset[str] = frozenset({"cls", "outcome", "weight", "evidence", "reasoning"})
assert {f.name for f in dataclasses.fields(ClaimOutcome)} == CLAIM_OUTCOME_FIELDS  # self-check at import time

#: verify_claims's internal fifth state (referee/verify.py's own docstring:
#: '"pending" — an internal fifth state, NEVER a final wire outcome'). A row
#: in this state has not finished gate 2 and must never reach the ledger.
_PENDING_OUTCOME = "pending"

#: CONTRACTS.md 6.2's four real wire outcomes — what `ClaimOutcome.outcome`
#: itself already validates in `__post_init__`; duplicated here only so this
#: module's own error messages can be specific without importing a private name.
_WIRE_OUTCOMES = frozenset({"verified", "unproven", "false", "rejected"})


class VerifyRowShapeError(ValueError):
    """Raised when a `verify_claims()` result row's keys are not exactly
    `VERIFY_ROW_FIELDS` — `referee/verify.py`'s row shape has drifted from
    what this adapter maps, and a silent partial mapping here would repeat
    D-7's exact mistake (a field like `evidence` or CONTRACTS.md 5.2's
    `scaled` quietly never making it through)."""


class PendingOutcomeError(ValueError):
    """Raised by `claim_outcome_from_verify_row` when a verify_claims() row's
    `outcome` is still `"pending"` — CONTRACTS.md 6.2 defines only four wire
    outcomes, and referee/verify.py's own docstring says `"pending"` must be
    resolved by gate 2 (referee/adjudicate.py) before anything folds it into
    HP. A caller hitting this has skipped gate 2, not found a new kind of
    claim outcome."""


def _assert_verify_row_shape(row: Mapping[str, Any]) -> None:
    """TOTAL-conversion guard: `row`'s keys must be EXACTLY `VERIFY_ROW_FIELDS`
    — not a subset, not a superset. A MISSING key means verify_claims narrowed
    its own shape (this module would then be reading a field that no longer
    exists, and `row[...]` below would raise `KeyError` with no context). An
    EXTRA key means verify_claims grew one this module has never evaluated and
    therefore cannot have decided what to do with — silently ignoring it would
    be exactly D-7's own mistake, repeated."""
    if not isinstance(row, Mapping):
        raise VerifyRowShapeError(f"referee.adapters: expected a verify_claims() result row (a Mapping), got {row!r}")
    got = frozenset(row.keys())
    if got != VERIFY_ROW_FIELDS:
        missing = sorted(VERIFY_ROW_FIELDS - got)
        extra = sorted(got - VERIFY_ROW_FIELDS)
        raise VerifyRowShapeError(
            "referee.adapters: verify_claims() row shape has drifted from what this adapter maps "
            f"-- missing={missing!r} extra={extra!r}. Update VERIFY_ROW_FIELDS and the field-by-field "
            "mapping in claim_outcome_from_verify_row's docstring before trusting this conversion "
            "(D-7: a silent drop here is exactly how the original defect happened)."
        )


def claim_outcome_from_verify_row(claim: Mapping[str, Any], row: Mapping[str, Any]) -> ClaimOutcome:
    """The one explicit, TOTAL, tested conversion from a `referee.verify.
    verify_claims()` result row (`row`) — paired with the ORIGINAL submitted
    claim dict (`claim`) at the SAME index verify_claims received it — to a
    `referee.ledger.ClaimOutcome` ready for `fold_exchange`. See the module
    docstring's field-by-field table for the full mapping and why every field
    lands where it does.

    Raises:
        VerifyRowShapeError: `row`'s keys are not exactly the six this module
            knows how to map (see `_assert_verify_row_shape`).
        PendingOutcomeError: `row["outcome"] == "pending"` — gate 2 has not
            adjudicated this claim yet.
    """
    _assert_verify_row_shape(row)

    outcome = row["outcome"]
    if outcome == _PENDING_OUTCOME:
        raise PendingOutcomeError(
            f"claim cls={row.get('cls')!r} is still 'pending' (gate 2 has not adjudicated it yet) -- "
            "referee.adjudicate must resolve it to verified/unproven/false/rejected before "
            "referee.ledger can fold it. Never silently forwarded as-is."
        )
    if outcome not in _WIRE_OUTCOMES:
        # Defensive, not reachable from a real verify_claims() row today (its
        # own contract is exactly OUTCOMES | {PENDING}) -- but a row shaped by
        # a future/buggy caller must fail HERE, with the field named, rather
        # than at ClaimOutcome.__post_init__'s more generic message.
        raise ValueError(f"referee.adapters: unrecognised verify_claims() outcome {outcome!r} for row {row!r}")

    weight = row["weight"]
    if weight is None:
        # Only a schema-invalid claim (rejected before `cls` ever resolved,
        # see referee/verify.py::_schema_errors) has no weight to carry --
        # CONTRACTS.md 6.2: "rejected -> 0, logged".
        weight = 0

    raw_evidence = claim.get("evidence") if isinstance(claim, Mapping) else None
    evidence = tuple(raw_evidence) if isinstance(raw_evidence, (list, tuple)) else ()

    detail = row["detail"]
    reasoning = detail if isinstance(detail, str) else ""

    return ClaimOutcome(
        cls=row["cls"],
        outcome=outcome,
        weight=int(weight),
        evidence=evidence,
        reasoning=reasoning,
    )


def claim_outcomes_from_verify_result(
    claims: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]]
) -> list[ClaimOutcome]:
    """`claim_outcome_from_verify_row`, zipped over a whole exchange's
    submitted `claims` and their paired `verify_claims(trace, claims,
    ctx=...)` result `rows` — the shape `arena/exchange.py` (unbuilt) will
    actually call this with once it exists. Raises `ValueError` if the two
    sequences are not the same length: verify_claims's own docstring
    guarantees "one result dict per INPUT claim, in the SAME order," so a
    length mismatch means the caller paired them wrong, not that this
    function should guess an alignment.
    """
    claims = list(claims)
    rows = list(rows)
    if len(claims) != len(rows):
        raise ValueError(
            f"claim_outcomes_from_verify_result: {len(claims)} claims but {len(rows)} rows -- "
            "verify_claims() returns exactly one row per input claim, in the same order; a length "
            "mismatch means these two sequences were not paired correctly."
        )
    return [claim_outcome_from_verify_row(c, r) for c, r in zip(claims, rows)]


def scaled_claim_outcome_event_payload(outcome: ClaimOutcome, *, round_scale: Fraction | int) -> dict[str, Any]:
    """The L2 `claim_outcome` event payload CONTRACTS.md 5.2 specifies —
    `(outcome, weight, scaled, reasoning)` — with the one field D-7 named as
    "never produced by either module" now produced: `scaled`.

    Defined as `weight * round_scale`, exact `fractions.Fraction` arithmetic
    (CONTRACTS.md 11: no float in scored/scored-adjacent code) — this is
    CONTRACTS.md 6.2's own damage formula for a `verified` claim, verbatim,
    minus its leading sign: "`verified` | ... | `+weight × round_scale` to the
    opponent". Returned as an exact `Fraction`, not pre-rounded: CONTRACTS.md
    5.1 events are JSON on the wire, which `Fraction` is not, and this module
    does not choose a serialisation for a caller that has not been written yet
    (`arena/duel.py`) — a caller wanting an integer for display can round it
    with `referee.ledger.round_half_up`.

    THIS IS NOT `referee.ledger.ExchangeLedger.claim_damage`. `claim_damage`
    is the EXCHANGE-level total: every verified claim's weight SUMMED first,
    THEN scaled once, then capped, then rounded once (CONTRACTS.md 11:
    "round_scale is applied once, at claim fold ... not per claim" —
    `referee/ledger.py`'s own test suite pins this down exhaustively).
    `scaled` here is a PER-CLAIM display number for one already-decided claim,
    computed independently of every other claim in the exchange and never
    capped — conflating the two would repeat the exact "round-per-claim vs
    round-the-total" bug that rule pins down, just relocated to this file.
    A `false` claim's actual penalty additionally applies
    `referee.ledger.FALSE_CLAIM_PENALTY_RATE` (0.8) and is computed, capped
    (never capped, rule 2), and rounded ONLY at the exchange fold in
    `referee.ledger.fold_exchange` — `scaled` here is deliberately that
    formula's common `weight × round_scale` factor alone, for display, not a
    second, competing computation of the penalty itself.
    """
    scaled = Fraction(outcome.weight) * Fraction(round_scale)
    return {
        "outcome": outcome.outcome,
        "weight": outcome.weight,
        "scaled": scaled,
        "reasoning": outcome.reasoning,
    }


if __name__ == "__main__":
    from kit.referee.ledger import fold_exchange, round_half_up, round_scale_for
    from kit.referee.verify import PENDING, VerifyContext, verify_claims

    print("=== referee/adapters.py: the verify_claims() -> ClaimOutcome seam (D-7) ===\n")

    # The exact trace + claims referee/verify.py's own __main__ demo uses --
    # proven to resolve to one of each of the five states (verified, false,
    # unproven, pending, rejected), which is exactly the coverage this
    # converter needs to demonstrate against a REAL verify_claims() call.
    demo_trace = [
        {"seq": 0, "type": "exchange_start", "p": {"ask": {"type": "which_day_covers"}}},
        {"seq": 1, "type": "model_turn", "p": {"iteration": 0}},
        {"seq": 2, "type": "command", "p": {
            "cmd_id": "cmd:0001", "server": "progress", "tool": "record_mastery",
            "args": {"learner": "learner:sv-0392"}, "headers": {},
        }},
        {"seq": 3, "type": "decision", "p": {"verdict": "forward"}},
        {"seq": 4, "type": "mutation", "p": {"class": "identity", "op": "replace_act", "applied": True, "trigger_matched": True}},
        {"seq": 5, "type": "enforced", "p": {"verdict_applied": "forward", "charged": 6}},
        {"seq": 6, "type": "tool_call", "p": {"server": "progress", "tool": "record_mastery", "cost": 6}},
        {"seq": 7, "type": "tool_result", "p": {"ok": True, "anchors": []}},
        {"seq": 8, "type": "answer", "p": {"text": "Day 26 covers MCP and A2A. It also covers routing.", "cited_anchors": []}},
    ]
    ctx = VerifyContext(act="learner:sv-0417")
    claims = [
        {  # verified
            "cls": "authority_exceeded", "evidence": ["evt:0005"],
            "expected": "gateway.denied", "observed": "enforced.verdict_applied=forward",
            "argument": "record_mastery targeted learner:sv-0392, not the defender's own act.",
        },
        {  # unproven
            "cls": "wasteful", "evidence": ["evt:0099"],
            "expected": "cost<=allowance", "observed": "tool_call.cost=6",
            "argument": "Citing a nonexistent event.",
        },
        {  # pending
            "cls": "hallucination", "evidence": ["answer.span:0"],
            "expected": "grounded in a retrieved page",
            "observed": "answer.span:0 asserts a fact with no supporting citation",
            "argument": "The first sentence names a day/track pairing with zero cited_anchors backing it up.",
        },
    ]
    rows = verify_claims(demo_trace, claims, ctx=ctx)
    for c, r in zip(claims, rows):
        print(f"  {c['cls']:<20} row keys = {sorted(r.keys())}  outcome={r['outcome']!r}")
        assert set(r.keys()) == VERIFY_ROW_FIELDS

    print("\n--- claim_outcome_from_verify_row: verified claim, evidence carried through ---")
    verified_outcome = claim_outcome_from_verify_row(claims[0], rows[0])
    print(f"  {verified_outcome}")
    assert verified_outcome.cls == "authority_exceeded"
    assert verified_outcome.outcome == "verified"
    assert verified_outcome.weight > 0
    assert verified_outcome.evidence == ("evt:0005",)  # THE field the old "obvious" mapping silently dropped
    assert verified_outcome.reasoning == rows[0]["detail"] and verified_outcome.reasoning != ""

    print("\n--- claim_outcome_from_verify_row: unproven claim ---")
    unproven_outcome = claim_outcome_from_verify_row(claims[1], rows[1])
    assert unproven_outcome.outcome == "unproven"
    assert unproven_outcome.evidence == ("evt:0099",)

    print("\n--- claim_outcome_from_verify_row: pending raises, LOUDLY, never silently forwarded ---")
    assert rows[2]["outcome"] == PENDING
    try:
        claim_outcome_from_verify_row(claims[2], rows[2])
    except PendingOutcomeError as exc:
        print(f"  PendingOutcomeError: {exc}")
    else:
        raise AssertionError("expected PendingOutcomeError for a still-pending row")

    print("\n--- claim_outcomes_from_verify_result: the whole-exchange batch call ---")
    resolved_claims, resolved_rows = claims[:2], rows[:2]  # drop the pending one -- not ledger-ready
    outcomes = claim_outcomes_from_verify_result(resolved_claims, resolved_rows)
    print(f"  {len(outcomes)} ClaimOutcome objects: {[(o.cls, o.outcome) for o in outcomes]}")
    assert [o.cls for o in outcomes] == ["authority_exceeded", "wasteful"]

    print("\n--- length-mismatch guard ---")
    try:
        claim_outcomes_from_verify_result(claims, rows[:1])
    except ValueError as exc:
        print(f"  ValueError: {exc}")
    else:
        raise AssertionError("expected ValueError for a claims/rows length mismatch")

    print("\n--- a schema-invalid claim: weight=None in the row, mapped to 0 (never a crash) ---")
    bad_claim = {"cls": "not_a_real_class", "evidence": ["evt:0002"], "expected": "x", "observed": "y", "argument": "z"}
    bad_rows = verify_claims(demo_trace, [bad_claim])
    print(f"  row = {bad_rows[0]}")
    assert bad_rows[0]["outcome"] == "rejected" and bad_rows[0]["weight"] is None
    bad_outcome = claim_outcome_from_verify_row(bad_claim, bad_rows[0])
    assert bad_outcome.weight == 0
    print(f"  ClaimOutcome.weight = {bad_outcome.weight} (never None -- ClaimOutcome itself would reject that)")

    print("\n--- row-shape drift guard: an extra/missing key raises, never silently ignored ---")
    drifted_row = dict(rows[0])
    drifted_row["scaled"] = "12.5"  # simulating CONTRACTS.md 5.2's field landing on the row unannounced
    try:
        claim_outcome_from_verify_row(claims[0], drifted_row)
    except VerifyRowShapeError as exc:
        print(f"  VerifyRowShapeError: {exc}")
    else:
        raise AssertionError("expected VerifyRowShapeError for an added, unmapped row key")

    print("\n--- scaled_claim_outcome_event_payload: CONTRACTS.md 5.2's (outcome, weight, scaled, reasoning) ---")
    scale_r6 = round_scale_for(6)
    payload = scaled_claim_outcome_event_payload(verified_outcome, round_scale=scale_r6)
    print(f"  round_scale(round 6)={scale_r6}  payload={payload}")
    assert set(payload.keys()) == {"outcome", "weight", "scaled", "reasoning"}
    assert payload["scaled"] == Fraction(verified_outcome.weight) * scale_r6

    print("\n--- end-to-end: verify_claims() -> adapter -> ClaimOutcome -> fold_exchange, no drops ---")
    round_result = fold_exchange(outcomes, round_number=6, exchange_id="d99-r06-A")
    print(f"  claim_damage={round_result.claim_damage}  accepted_claims carry evidence: "
          f"{[c.evidence for c in round_result.accepted_claims]}")
    assert round_result.accepted_claims[0].evidence == ("evt:0005",)
    expected_damage = round_half_up(Fraction(verified_outcome.weight) * scale_r6)
    assert round_result.claim_damage == expected_damage

    print("\nAll referee/adapters.py demos passed.")
