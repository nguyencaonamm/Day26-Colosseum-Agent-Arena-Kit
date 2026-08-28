"""kit/mcp/hardmode.py — the eight hard-mode contract mechanics (FINAL-PLAN.md
section 4.2, CONTRACTS.md sections 3.3/3.4/4.2). This is the STATE that makes
"which tool" become "which tool, with what mask, in what order, holding what
ticket, within which window" (FINAL-PLAN.md 4.2's own framing). Enforcement,
not decoration: every mechanic below either produces one of the closed nine
MCP error codes (kit/mcp/errors.py) before a call runs, or reshapes the
result a call actually returns — never a tenth code, never a softened
`unavailable`, never a silent no-op where the plan calls for a fire.

    1. Dynamic cost          -> HardMode.cost_of() delegates to kit.mcp.specs.cost_of
    2. Leases                -> get_frame needs a live lease minted by search/query
    3. Preconditions         -> writes need an If-Match etag from registry.provenance
    4. Partial results       -> a seeded rule marks + truncates over-long result sets
    5. Rate windows          -> per-tool caps that span ROUNDS, not just one exchange
    6. Opaque errors         -> a seeded subset of otherwise-valid calls goes dark
    7. Language negotiation  -> a missing/wrong glossary.define lang gets the other one
    8. Deprecation           -> every result is stamped from TOOL_SPECS truth

class HardMode holds all of this as **per-duel** state (CONTRACTS.md 4.3:
"Gateway is instantiated once per duel... Instance state persists across the
10 rounds"; this module is not a Gateway, but the same lifetime rule governs
it — it is instantiated once when a duel starts and lives for all 10 rounds,
not re-created per exchange). Two hooks bracket every tool call:

    err = hardmode.check_before(call)      # -> error dict | None
    if err is not None:
        return hardmode.deny_result(call, err)   # nothing ran; no data to shape
    raw = the_real_tool(call)              # whatever a tool server would compute
    result = hardmode.record_after(call, raw)    # -> the FINAL ToolResult

**`record_after` returns a new `ToolResult` — callers MUST use the return
value.** `ToolResult` is frozen (kit/mcp/types.py), and three of the eight
mechanics (partial-result truncation, language negotiation, deprecation
stamping) only make sense as a transform of the tool's raw result, not as a
side-effecting bookkeeping call that returns nothing. This is the module's
one deliberate reading of a two-hook description that must, by construction,
also reshape data — see "AMBIGUITY RESOLVED" below for the full argument.

`check_before` never touches `result` (it hasn't happened yet) and only ever
returns one of six codes: `lease_required`, `lease_expired`,
`precondition_missing`, `conflict`, `rate_limited`, `unavailable`. The other
three closed codes (`unauthorized`, `bad_request`, `not_found`) belong to the
gateway/authority layer and the world/data layer respectively — outside
these eight mechanics, so this module never raises them except as the one
defensive `bad_request` fallback documented on :meth:`HardMode.check_before`
for a `(server, tool)` pair `kit.mcp.specs` does not know at all.

Stdlib only. No network, no unseeded randomness, no wall-clock — every
seeded decision in this module is a pure function of
`(world_id, duel_id, round, call.server, call.tool, call.call_index)`
(CONTRACTS.md section 11's own seeding formula, "world_id + duel_id +
round", extended with the call's own identity so two different calls in the
same round do not share a coin flip).
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from kit.mcp.errors import make_error
from kit.mcp.specs import ROUNDS_PER_DUEL, TOOL_SPECS, cost_of as _spec_cost_of
from kit.mcp.types import ToolCall, ToolResult

__all__ = [
    "PARTIAL_ROW_THRESHOLD",
    "OPAQUE_DENOMINATOR",
    "DEGRADED_REPLICA_DENOMINATOR",
    "LEASE_SUBSEQUENT_CALLS",
    "WorldLike",
    "HardMode",
]


# ---------------------------------------------------------------------------
# Tunable constants — the "seeded rule" thresholds. Every one is a plain
# int, so retuning never touches the logic that reads it (same spirit as
# kit/mcp/specs.py keeping TOOL_SPECS as data).
# ---------------------------------------------------------------------------

# Mechanic 4: a list-shaped tool's result becomes partial once it exceeds
# this many rows (FINAL-PLAN.md 4.2: "result set > N rows").
PARTIAL_ROW_THRESHOLD = 3

# Mechanic 6: 1-in-this-many otherwise-valid calls goes opaquely
# `unavailable` (a stateless seeded coin flip per call — see
# `HardMode._seeded_int` and the module docstring's ambiguity note).
OPAQUE_DENOMINATOR = 6

# Mechanic 4's second trigger path (FINAL-PLAN.md 4.2: "or a degraded
# replica"): 1-in-this-many (world_id, duel_id, round, server, tool)
# combinations are a degraded replica this round, regardless of row count.
DEGRADED_REPLICA_DENOMINATOR = 4

# Mechanic 2: a lease minted at call_index K is live for exactly this many
# SUBSEQUENT calls (K+1 .. K+LEASE_SUBSEQUENT_CALLS); dead from
# K+LEASE_SUBSEQUENT_CALLS+1 onward. CONTRACTS.md 4.2 mechanic 2 and the
# fixture glossary entry (`kit/world/fixture.py`: "gọi lần thứ tư trả về
# lease_expired") both say 3.
LEASE_SUBSEQUENT_CALLS = 3


@runtime_checkable
class WorldLike(Protocol):
    """Structural type for mechanic 7's optional data source. Duck-typed
    (only `.terms()` / `.page()` are used) rather than importing
    `kit.world.loader.World` directly — the same reasoning `kit/mcp/specs.py`
    gives for taking `call` duck-typed: a real `World` instance satisfies
    this without hardmode.py ever importing kit.world, so this module keeps
    working file-for-file regardless of that sibling's shape drifting, and a
    caller with no `World` yet can simply pass `world=None`."""

    def terms(self, term: str, lang: str | None = None) -> list: ...

    def page(self, anchor: "Any") -> "Any": ...


def _seeded_int(*parts: object, modulus: int) -> int:
    """A pure, deterministic `[0, modulus)` value from `parts` — CONTRACTS.md
    section 11's seeding rule ("No random without an explicit seed threaded
    from world_id + duel_id + round") generalised to also fold in the call's
    own identity, so two different calls in the same round never share a
    coin flip. sha256 over a stable string join; never `random`, never
    `hash()` (which is salted per-process in CPython and would break
    G-REPRO's replay-determinism guarantee)."""
    if modulus <= 0:
        raise ValueError(f"modulus must be positive, got {modulus!r}")
    joined = "\x1f".join(str(p) for p in parts)
    digest = hashlib.sha256(joined.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % modulus


@dataclass(slots=True)
class _LeaseState:
    """One minted lease, live for `LEASE_SUBSEQUENT_CALLS` calls after
    `minted_at_call_index` (mechanic 2). Tracked per-round — mechanic 2's
    "cannot be... cached across ROUNDS" is enforced by `HardMode.begin_round`
    discarding every entry here, not by any expiry math within a round."""

    minted_at_call_index: int


@dataclass(slots=True)
class _RateWindow:
    """Rounds (not call indices — mechanic 5 is explicitly cross-ROUND) in
    which an ALLOWED call to this `(server, tool)` was made this duel, kept
    as a deque so pruning outside the sliding window is O(1) amortised."""

    rounds: deque = field(default_factory=deque)


class HardMode:
    """Per-duel hard-mode enforcement state (CONTRACTS.md 4.3's "instantiated
    once per duel... persists across the 10 rounds" lifetime rule, applied
    here to the tool layer rather than the gateway).

    AMBIGUITY RESOLVED — `record_after`'s return value: the task brief names
    exactly two hooks, `check_before(call) -> error|None` and
    `record_after(call, result)`, without spelling out `record_after`'s
    return type. Three of the eight mechanics (partial-result truncation,
    language negotiation, deprecation stamping) are properties of the RESULT
    a call produces, not of the call alone, and `ToolResult` is frozen
    (kit/mcp/types.py) — there is no way to mutate one in place. A
    `record_after` that only updates internal bookkeeping and returns
    nothing would have no way to deliver those three mechanics to the
    caller at all. The one reading that makes all eight mechanics
    real is `record_after(call, result: ToolResult) -> ToolResult`:
    it both updates state (lease minting, etag caching, rate-window
    consumption stays where `check_before` already consumed it) AND returns
    the transformed result the caller must actually send back to the
    student. This is documented loudly on the method itself, not just here.

    AMBIGUITY RESOLVED — slot-consumption timing: `check_before` consumes a
    rate-window slot only at the moment it decides to ALLOW a call through
    (immediately before returning `None`), never when a call is rejected for
    lease/precondition reasons, and never when the opaque-unavailable coin
    flip kills an otherwise-valid call. An opaque `unavailable` still burns
    the caller's credits (ERROR_SPECS: charged=True) but must NOT also burn
    one of `citation-checker.verify_source`'s 2-per-3-rounds slots — that
    would let bad luck silently shrink a budget the plan defines in terms of
    calls that actually happened. Idempotency-key consumption is the
    deliberate asymmetry: a write's key is marked seen at the SAME
    allow-time as the rate slot (not deferred to `record_after`), because an
    opaque-killed write burning its key IS the intended trap (FINAL-PLAN.md
    4.2 mechanic 6: "retrying a write is write_violation" — the student
    cannot know from an opaque error whether the write landed, and the only
    safe move is a fresh `registry.provenance` read, never a blind retry
    with the same key).

    AMBIGUITY RESOLVED — precondition failure codes: CONTRACTS.md 3.3 has no
    tenth code named for "this Idempotency-Key was already used this duel"
    or "no etag was ever issued for this anchor". Both map to `conflict`
    (the 409 shape: "an etag/token that no longer matches what the server
    has on file"), never a fabricated code and never silently treated as
    `precondition_missing` (that code is reserved for the header being
    ABSENT, per CONTRACTS.md 3.3's own row: "write with no If-Match or no
    Idempotency-Key" — a header that is PRESENT but stale or reused is a
    different failure, and collapsing the two would hide from a student
    which fix actually applies: attach a header at all, vs. go re-read
    provenance first).

    AMBIGUITY RESOLVED — mechanic 6's stateless seed: `unavailable`'s
    ERROR_SPECS `retry_note` is `"once"` — read here as a strategic HINT to
    the student (retry once rather than looping forever; looping is
    `wasteful`), not as an engine-enforced guarantee that a second identical
    attempt always succeeds. `check_before` re-seeds independently per call
    (folding in `call.call_index`), so a genuine retry is a fresh coin flip,
    not a guaranteed pass — the same honest uncertainty a real opaque 503
    carries. This keeps the mechanic simple (no extra "already burned this
    duel" bookkeeping) and reproducible (CONTRACTS.md section 11 G-REPRO):
    the same `(world_id, duel_id, round, server, tool, call_index)` always
    flips the same way.

    AMBIGUITY RESOLVED — lease decay: "valid for exactly 3 SUBSEQUENT
    calls" is read as 3 subsequent calls of ANY tool (not only 3 subsequent
    `get_frame` calls), because `Command.call_index` (CONTRACTS.md 4.1) is a
    single "0-based within the exchange" counter shared by every tool a side
    calls — there is no separate per-tool counter a lease could decay
    against. A lease minted at `call_index=K` is checked against
    `call.call_index` at use time: live for `K+1 <= call_index <=
    K+LEASE_SUBSEQUENT_CALLS`, `lease_expired` from
    `K+LEASE_SUBSEQUENT_CALLS+1` onward — pinned exactly to
    `kit/world/fixture.py`'s own glossary text ("gọi lần thứ tư trả về
    lease_expired": the 4th subsequent call, i.e. `K+4`, expires it).
    """

    def __init__(
        self,
        *,
        world: "WorldLike | None" = None,
        opaque_enabled: bool = True,
        degraded_replica_enabled: bool = True,
    ) -> None:
        self._world = world
        # Both seeded mechanics default ON — a real duel always runs with
        # the full eight mechanics live. The toggles exist so a test (or
        # this module's own __main__ demo) proving one mechanic fires can
        # hold the OTHER seeded mechanic still, rather than that test
        # becoming flaky on whichever `(world_id, duel_id, round, server,
        # tool, call_index)` tuple it happens to pick. Neither toggle
        # changes anything about the seed formula itself (still pure,
        # still `world_id+duel_id+round`-threaded per CONTRACTS.md 11) —
        # it only gates whether that already-computed coin flip is acted on.
        self._opaque_enabled = opaque_enabled
        self._degraded_replica_enabled = degraded_replica_enabled
        # `reset()` is REQUIRED before first use (CONTRACTS.md 4.3 / the task
        # brief: "Reset explicitly at duel start") — `_duel_id` stays `None`
        # until then, and every check/record call asserts it is not None so
        # a forgotten reset fails loudly instead of silently running with an
        # empty, wrongly-attributed duel.
        self._duel_id: str | None = None
        self._world_id: str = ""
        self._round: int = 1
        self._live_leases: dict[str, _LeaseState] = {}
        self._rate_windows: dict[tuple[str, str], _RateWindow] = {}
        self._idempotency_keys_seen: set[str] = set()
        self._issued_etags: dict[str, str] = {}  # anchor -> last-issued etag
        self._lease_mint_counter: int = 0
        self._continuation_counter: int = 0

    # -----------------------------------------------------------------
    # Lifecycle — CONTRACTS.md 4.3: state lifetime is the DUEL, reset
    # explicitly at duel start; leases additionally do not survive a round.
    # -----------------------------------------------------------------

    def reset(self, duel_id: str, *, world_id: str = "", starting_round: int = 1) -> None:
        """Wipe EVERY piece of per-duel state and start a fresh duel. Must
        be called once, explicitly, before the first `check_before` /
        `record_after` of a duel — instance state otherwise persists across
        all 10 rounds by design (CONTRACTS.md 4.3), so nothing here is
        cleared implicitly by round or exchange boundaries except leases via
        `begin_round`."""
        if not isinstance(duel_id, str) or not duel_id:
            raise ValueError(f"HardMode.reset(): duel_id must be a non-empty str, got {duel_id!r}")
        if not isinstance(starting_round, int) or isinstance(starting_round, bool) or not (
            1 <= starting_round <= ROUNDS_PER_DUEL
        ):
            raise ValueError(
                f"HardMode.reset(): starting_round must be within [1, {ROUNDS_PER_DUEL}], "
                f"got {starting_round!r}"
            )
        self._duel_id = duel_id
        self._world_id = world_id
        self._round = starting_round
        self._live_leases = {}
        self._rate_windows = {}
        self._idempotency_keys_seen = set()
        self._issued_etags = {}
        self._lease_mint_counter = 0
        self._continuation_counter = 0

    def begin_round(self, round_no: int) -> None:
        """Advance to a new round WITHIN the current duel. Clears every live
        lease (mechanic 2: "cannot be... cached across ROUNDS") and leaves
        everything else — rate windows, idempotency keys seen, issued etags
        — untouched, because those three are explicitly duel-lifetime
        (mechanic 5 is cross-round by definition; preconditions and replay
        protection would be trivially bypassable by a round boundary if
        they reset every round)."""
        self._require_duel()
        if not isinstance(round_no, int) or isinstance(round_no, bool) or not (
            1 <= round_no <= ROUNDS_PER_DUEL
        ):
            raise ValueError(f"HardMode.begin_round(): round_no must be within [1, {ROUNDS_PER_DUEL}], got {round_no!r}")
        self._round = round_no
        self._live_leases = {}

    def _require_duel(self) -> None:
        if self._duel_id is None:
            raise RuntimeError(
                "HardMode.reset(duel_id) must be called before check_before/record_after "
                "(CONTRACTS.md 4.3: state lifetime is the duel, reset explicitly at duel start)"
            )

    # -----------------------------------------------------------------
    # Mechanic 1 — dynamic cost. Pure delegation to kit.mcp.specs.cost_of;
    # HardMode adds no pricing logic of its own, per the task brief.
    # -----------------------------------------------------------------

    def cost_of(self, call: ToolCall, n_rows: int) -> int:
        """`kit.mcp.specs.cost_of(call, n_rows)`, unchanged. Exposed here so
        a caller can price a `check_before` denial (`n_rows=0`, nothing ran)
        without importing `kit.mcp.specs` a second time."""
        return _spec_cost_of(call, n_rows)

    def deny_result(self, call: ToolCall, error: dict) -> ToolResult:
        """Build the `ToolResult` for a `check_before` denial: the error is
        still charged (CONTRACTS.md 3.3: "Cost is still charged except where
        noted" — none of `check_before`'s six codes are the noted
        exception), priced at `n_rows=0` since nothing executed.

        Deliberately tolerant of the one case `check_before` itself is
        tolerant of: a `(server, tool)` pair `kit.mcp.specs.TOOL_SPECS` does
        not know at all (`check_before`'s `bad_request` fallback). Pricing
        that case via `cost_of` would call straight back into
        `TOOL_SPECS[(call.server, call.tool)]` and raise `KeyError` — the
        exact composition `check_before(call); if err: deny_result(call,
        err)` this module's own docstring recommends would then crash on
        the call it was MOST defensive about. There is nothing in the
        economy to charge an unknown tool against, so that one case is
        priced at 0 rather than propagating the KeyError."""
        if (call.server, call.tool) not in TOOL_SPECS:
            return ToolResult(ok=False, error=error, cost=0)
        return ToolResult(ok=False, error=error, cost=self.cost_of(call, n_rows=0))

    # -----------------------------------------------------------------
    # check_before — the four error-producing mechanics (2, 3, 5, 6), in a
    # fixed priority order: lease -> precondition -> rate -> opaque. Opaque
    # is checked LAST and only for a call that would otherwise have been
    # allowed — infra flakiness is not supposed to be the REASON a
    # structurally-invalid call fails, only the reason a valid one does.
    # -----------------------------------------------------------------

    def check_before(self, call: ToolCall) -> dict | None:
        """Returns a `kit.mcp.errors.make_error(...)`-shaped dict if `call`
        must be rejected before the underlying tool runs, else `None`
        (proceed). Never raises for a call this module recognises; a
        `(server, tool)` pair `kit.mcp.specs.TOOL_SPECS` does not know at
        all is the one case this returns `bad_request` for, since the real
        gate for that is the arena's RPC allowlist (CONTRACTS.md 12.2:
        `ALLOWED_METHODS = set(TOOL_SPECS)`) — this module should not crash
        on a call that allowlist would already have rejected."""
        self._require_duel()
        spec = TOOL_SPECS.get((call.server, call.tool))
        if spec is None:
            return make_error("bad_request", reason=f"unknown tool {call.server}.{call.tool}")

        lease_err = self._check_lease(call, spec)
        if lease_err is not None:
            return lease_err

        precondition_err = self._check_precondition(call, spec)
        if precondition_err is not None:
            return precondition_err

        rate_err = self._check_rate_limit(call, spec)
        if rate_err is not None:
            return rate_err

        # Mechanic 6 — opaque errors. Reached only once every earlier,
        # deterministic-for-a-reason check has already passed: this call
        # was otherwise good.
        if self._opaque_enabled and self._seeded_int(
            "opaque", call.server, call.tool, call.call_index, modulus=OPAQUE_DENOMINATOR
        ) == 0:
            return make_error("unavailable")

        # Allowed. Consume whatever this call's allowance is spent AT
        # allow-time (see the class docstring's slot-consumption note) —
        # never earlier, so a denied or opaque-killed call spends nothing
        # from a rate window, and a write's idempotency key is claimed the
        # moment it is actually let through.
        if spec.rate_limit is not None:
            self._rate_window_for(call.server, call.tool).rounds.append(self._round)
        if spec.is_write:
            self._idempotency_keys_seen.add(call.headers["idempotency-key"])
        return None

    # -- mechanic 2: leases -------------------------------------------------

    def _check_lease(self, call: ToolCall, spec) -> dict | None:
        if not spec.needs_lease:
            return None
        if call.lease_id is None:
            return make_error("lease_required")
        lease = self._live_leases.get(call.lease_id)
        if lease is None:
            # Either never minted by this HardMode (mechanic 2: "cannot be
            # minted by the caller" — a self-invented id looks identical to
            # one that never existed), or minted in an earlier round and
            # already discarded by `begin_round` (mechanic 2: "cannot be...
            # cached across ROUNDS"). Both collapse to the same code the
            # closed taxonomy offers for "there is no live lease": you never
            # had a valid one in hand, as opposed to having had one that
            # aged out within this same round.
            return make_error("lease_required")
        calls_since_mint = call.call_index - lease.minted_at_call_index
        if calls_since_mint > LEASE_SUBSEQUENT_CALLS:
            del self._live_leases[call.lease_id]
            return make_error("lease_expired")
        return None

    # -- mechanic 3: preconditions -------------------------------------------

    def _check_precondition(self, call: ToolCall, spec) -> dict | None:
        if not spec.is_write:
            return None
        if_match = call.headers.get("if-match")
        idem_key = call.headers.get("idempotency-key")
        if not if_match or not idem_key:
            return make_error("precondition_missing")
        if idem_key in self._idempotency_keys_seen:
            # A replayed key without a fresh registry.provenance read —
            # mechanic 3's "retrying a write without re-reading" case.
            return make_error("conflict")
        anchor = call.args.get("anchor")
        issued = self._issued_etags.get(anchor) if anchor is not None else None
        if issued is None or if_match != issued:
            return make_error("conflict")
        return None

    # -- mechanic 5: rate windows --------------------------------------------

    def _rate_window_for(self, server: str, tool: str) -> _RateWindow:
        key = (server, tool)
        window = self._rate_windows.get(key)
        if window is None:
            window = _RateWindow()
            self._rate_windows[key] = window
        return window

    def _check_rate_limit(self, call: ToolCall, spec) -> dict | None:
        if spec.rate_limit is None:
            return None
        calls_allowed, per_rounds = spec.rate_limit
        window = self._rate_window_for(call.server, call.tool)
        floor = self._round - per_rounds + 1
        while window.rounds and window.rounds[0] < floor:
            window.rounds.popleft()
        if len(window.rounds) >= calls_allowed:
            return make_error("rate_limited")
        return None

    # -- mechanic 6: opaque errors' seed -------------------------------------

    def _seeded_int(self, salt: str, server: str, tool: str, call_index: int, *, modulus: int) -> int:
        return _seeded_int(
            self._world_id, self._duel_id, self._round, salt, server, tool, call_index,
            modulus=modulus,
        )

    # -----------------------------------------------------------------
    # record_after — the four result-shaping mechanics (1's cost restamp,
    # 4, 7, 8), plus the state updates that depend on what the call
    # actually returned (lease minting, etag caching).
    # -----------------------------------------------------------------

    def record_after(self, call: ToolCall, result: ToolResult) -> ToolResult:
        """Given the tool's raw `result` (as if hard mode did not exist),
        return the FINAL `ToolResult` to hand back to the caller — see the
        class docstring's ambiguity note: **the return value is the
        contract, not a side effect.** A non-`ok` `result` (the underlying
        tool itself failed for a reason outside these eight mechanics, e.g.
        `not_found`) is returned unchanged: none of the eight mechanics
        reshape an error the tool produced on its own, only successes."""
        self._require_duel()
        if not result.ok:
            return result
        spec = TOOL_SPECS.get((call.server, call.tool))
        if spec is None:
            return result  # unknown tool: check_before would already have
            # denied it; nothing left for record_after to reshape.

        rows = list(result.rows)
        anchors = list(result.anchors)
        lease_id = result.lease_id
        etag = result.etag

        # Mechanic 2 — mint a fresh lease on a successful search/query.
        # "Cannot be minted by the caller": the id is generated here, from
        # HardMode's own counter, never echoed from anything the caller sent.
        if call.server == "slides" and call.tool in ("search", "query"):
            lease_id = self._mint_lease(call.call_index)

        # Mechanic 3 bookkeeping — cache the etag registry.provenance just
        # handed out, keyed by the anchor it describes, so a LATER write's
        # If-Match has something honest to be checked against. CONTRACTS.md
        # 3.2 marks the envelope's TOP-LEVEL `etag` as "provenance only" —
        # preferred here (`etag`, already `result.etag` at this point) —
        # with a fallback to a `rows[0]["etag"]` field mask, since a real
        # `registry` server (a collaborator's file, unwritten as of this
        # module) may put it in either place depending on how `provenance`'s
        # own field mask was requested.
        if call.server == "registry" and call.tool == "provenance":
            anchor = call.args.get("anchor")
            if anchor is not None:
                provenance_etag = etag if etag is not None else (
                    rows[0].get("etag") if rows else None
                )
                if provenance_etag is not None:
                    self._issued_etags[anchor] = provenance_etag

        # Mechanic 7 — language negotiation for glossary.define.
        if call.server == "glossary" and call.tool == "define":
            rows, anchors, etag = self._negotiate_lang(call, rows, anchors, etag)

        # Mechanic 4 — partial results: seeded, deterministic, reproducible.
        # Two independent triggers (FINAL-PLAN.md 4.2's own two examples):
        # too many rows, or a degraded replica this round for this tool.
        degraded = self._degraded_replica_enabled and self._seeded_int(
            "degraded_replica", call.server, call.tool, call.call_index,
            modulus=DEGRADED_REPLICA_DENOMINATOR,
        ) == 0
        over_threshold = len(rows) > PARTIAL_ROW_THRESHOLD
        partial = over_threshold or degraded
        continuation = None
        if partial:
            if over_threshold:
                rows = rows[:PARTIAL_ROW_THRESHOLD]
                if len(anchors) > PARTIAL_ROW_THRESHOLD:
                    anchors = anchors[:PARTIAL_ROW_THRESHOLD]
            continuation = self._mint_continuation(call)

        # Mechanic 8 — deprecation is stamped from TOOL_SPECS truth on every
        # successful result, unconditionally, so it can never drift from
        # whatever a tool implementation happened to set.
        deprecated = spec.deprecated
        successor = spec.successor

        final_cost = self.cost_of(call, n_rows=len(rows))

        return ToolResult(
            ok=True,
            rows=tuple(rows),
            anchors=tuple(anchors),
            cost=final_cost,
            partial=partial,
            continuation=continuation,
            lease_id=lease_id,
            etag=etag,
            replica=result.replica,
            ttl=result.ttl,
            deprecated=deprecated,
            successor=successor,
        )

    # -- mechanic 2 helper ----------------------------------------------------

    def _mint_lease(self, call_index: int) -> str:
        self._lease_mint_counter += 1
        token = _seeded_int(
            self._world_id, self._duel_id, self._round, "lease", self._lease_mint_counter,
            modulus=16**8,
        )
        lease_id = f"lse_{token:08x}"
        self._live_leases[lease_id] = _LeaseState(minted_at_call_index=call_index)
        return lease_id

    # -- mechanic 4 helper ----------------------------------------------------

    def _mint_continuation(self, call: ToolCall) -> str:
        self._continuation_counter += 1
        return f"cont_{call.server}.{call.tool}_{self._round}_{self._continuation_counter:04d}"

    # -- mechanic 7 helper ----------------------------------------------------

    def _negotiate_lang(
        self, call: ToolCall, rows: list, anchors: list, etag: str | None
    ) -> tuple[list, list, str | None]:
        """A missing or wrong `lang` in `call.args` silently returns the
        OTHER language's entry — no error, a wrong answer with a
        valid-looking anchor (FINAL-PLAN.md 4.2 mechanic 7). Only fires when
        this HardMode was built with a `world` (see the class docstring's
        `WorldLike` note); without one this is a documented no-op, since
        there is no bilingual data to substitute from. When the requested
        `lang` genuinely resolved (the honest `rows`/`anchors` already
        passed in already answer it), this returns them UNCHANGED —
        byte-identical passthrough is the "does not fire spuriously" half
        of this mechanic's test."""
        if self._world is None:
            return rows, anchors, etag
        term = call.args.get("term")
        if not isinstance(term, str) or not term.strip():
            return rows, anchors, etag
        requested_lang = call.args.get("lang")
        if requested_lang in ("vi", "en"):
            honest = self._world.terms(term, lang=requested_lang)
            if honest:
                return rows, anchors, etag  # the caller already got it right
        # Missing / invalid / unsatisfiable lang: fall back to whatever
        # sense terms.json lists first for this term, regardless of match —
        # the silent, wrong-on-purpose substitution.
        every_sense = self._world.terms(term)
        if not every_sense:
            return rows, anchors, etag  # term does not exist at all; not this mechanic's job
        chosen_anchor = every_sense[0]
        page = self._world.page(chosen_anchor)
        if page is None:
            return rows, anchors, etag
        row = {
            "definition": page.body,
            "sense": page.path_id,
            "source_term": term,
        }
        return [row], [str(chosen_anchor)], page.etag


if __name__ == "__main__":
    import sys
    import tempfile
    from pathlib import Path

    print("=== kit.mcp.hardmode: the eight FINAL-PLAN.md 4.2 mechanics ===\n")

    def _call(server: str, tool: str, **kw: object) -> ToolCall:
        kw.setdefault("args", {})
        return ToolCall(server=server, tool=tool, **kw)  # type: ignore[arg-type]

    # -- lifecycle: reset is required -------------------------------------
    # `opaque_enabled=False` here: this `hm` instance demos mechanics 1-5,
    # 7, 8 below, and mechanic 6's seeded coin flip is orthogonal to all of
    # them (see the class docstring's toggle note) — a separate instance
    # with the default `opaque_enabled=True` demos mechanic 6 on its own.
    print("--- lifecycle ---")
    hm = HardMode(opaque_enabled=False)
    try:
        hm.check_before(_call("slides", "query"))
    except RuntimeError as exc:
        print(f"  check_before before reset() raises RuntimeError: {exc}")
    else:
        raise AssertionError("expected RuntimeError before reset()")
    hm.reset("duel-demo", world_id="world-demo")
    print("  hm.reset('duel-demo') ok")

    # -- mechanic 1: dynamic cost -------------------------------------------
    print("\n--- mechanic 1: dynamic cost (delegates to kit.mcp.specs.cost_of) ---")
    query_call = _call("slides", "query", args={"q": "streamable http"}, fields=("title", "body"))
    from kit.mcp.specs import cost as spec_cost

    expected = spec_cost("slides", "query", fields=("title", "body"), n_rows=1)
    got = hm.cost_of(query_call, n_rows=1)
    print(f"  hm.cost_of(query_call, n_rows=1) = {got} (kit.mcp.specs.cost() = {expected})")
    assert got == expected

    # -- mechanic 2: leases ---------------------------------------------------
    print("\n--- mechanic 2: leases ---")
    hm.reset("duel-leases", world_id="world-demo")
    gf0 = _call("slides", "get_frame", args={"anchor": "Frame:deadbeef/w/001"}, call_index=0)
    err = hm.check_before(gf0)
    print(f"  get_frame with no lease_id -> {err}")
    assert err == {"code": "lease_required"}

    search_call = _call("slides", "search", args={"q": "x"}, call_index=0)
    assert hm.check_before(search_call) is None
    search_result = ToolResult(ok=True, rows=({"anchor": "Frame:deadbeef/w/001", "title": "t"},), cost=2)
    minted = hm.record_after(search_call, search_result)
    print(f"  slides.search mints lease_id={minted.lease_id!r}")
    assert minted.lease_id is not None

    gf_forged = _call(
        "slides", "get_frame", args={"anchor": "Frame:deadbeef/w/001"},
        lease_id="lse_totally_made_up", call_index=1,
    )
    forged_err = hm.check_before(gf_forged)
    print(f"  get_frame with a SELF-INVENTED lease_id -> {forged_err}")
    assert forged_err == {"code": "lease_required"}

    for offset in (1, 2, 3):
        gf_live = _call(
            "slides", "get_frame", args={"anchor": "Frame:deadbeef/w/001"},
            lease_id=minted.lease_id, call_index=offset,
        )
        live_err = hm.check_before(gf_live)
        print(f"  call_index={offset} (mint+{offset}) with the real lease -> {live_err}")
        assert live_err is None

    gf_dead = _call(
        "slides", "get_frame", args={"anchor": "Frame:deadbeef/w/001"},
        lease_id=minted.lease_id, call_index=4,
    )
    dead_err = hm.check_before(gf_dead)
    print(f"  call_index=4 (mint+4, the 4th subsequent call) -> {dead_err}")
    assert dead_err == {"code": "lease_expired"}

    # cross-round: a new round clears every live lease, even an unexpired one
    hm.reset("duel-leases-2", world_id="world-demo")
    assert hm.check_before(search_call) is None
    minted2 = hm.record_after(search_call, search_result)
    hm.begin_round(2)
    gf_stale_round = _call(
        "slides", "get_frame", args={"anchor": "Frame:deadbeef/w/001"},
        lease_id=minted2.lease_id, call_index=1,
    )
    round_err = hm.check_before(gf_stale_round)
    print(f"  a lease minted in round 1, used in round 2 -> {round_err}")
    assert round_err == {"code": "lease_required"}

    # -- mechanic 3: preconditions --------------------------------------------
    print("\n--- mechanic 3: preconditions ---")
    hm.reset("duel-writes", world_id="world-demo")
    write_call_no_headers = _call(
        "progress", "record_mastery",
        args={"anchor": "Frame:deadbeef/w/001", "learner": "learner:sv-0417"},
    )
    err = hm.check_before(write_call_no_headers)
    print(f"  write with NO If-Match / Idempotency-Key -> {err}")
    assert err == {"code": "precondition_missing"}

    write_never_read = _call(
        "progress", "record_mastery",
        args={"anchor": "Frame:deadbeef/w/001", "learner": "learner:sv-0417"},
        headers={"if-match": "sha256:aaaaaaaaaaaaaaaa", "idempotency-key": "idem-001"},
    )
    err = hm.check_before(write_never_read)
    print(f"  write with an etag that was NEVER issued by registry.provenance -> {err}")
    assert err == {"code": "conflict"}

    prov_call = _call("registry", "provenance", args={"anchor": "Frame:deadbeef/w/001"})
    assert hm.check_before(prov_call) is None
    prov_result = ToolResult(ok=True, rows=({"etag": "sha256:aaaaaaaaaaaaaaaa"},), cost=1)
    hm.record_after(prov_call, prov_result)

    write_good = _call(
        "progress", "record_mastery",
        args={"anchor": "Frame:deadbeef/w/001", "learner": "learner:sv-0417"},
        headers={"if-match": "sha256:aaaaaaaaaaaaaaaa", "idempotency-key": "idem-001"},
    )
    err = hm.check_before(write_good)
    print(f"  same write, AFTER reading provenance, fresh idempotency-key -> {err}")
    assert err is None

    write_replay = _call(
        "progress", "record_mastery",
        args={"anchor": "Frame:deadbeef/w/001", "learner": "learner:sv-0417"},
        headers={"if-match": "sha256:aaaaaaaaaaaaaaaa", "idempotency-key": "idem-001"},
    )
    err = hm.check_before(write_replay)
    print(f"  retrying with the SAME idempotency-key, no re-read -> {err}")
    assert err == {"code": "conflict"}

    # -- mechanic 5: rate windows ----------------------------------------------
    print("\n--- mechanic 5: rate windows (cross-ROUND, not per-exchange) ---")
    hm.reset("duel-rates", world_id="world-demo")
    vs_call = _call("citation-checker", "verify_source", args={"url": "https://fixture.example/x"})
    outcomes = []
    for rnd in (1, 1, 2, 3, 4):
        hm.begin_round(rnd)
        outcomes.append((rnd, hm.check_before(vs_call)))
    for rnd, err in outcomes:
        print(f"  round {rnd}: {err}")
    assert outcomes[0][1] is None and outcomes[1][1] is None  # 2 allowed in round 1
    assert outcomes[2][1] == {"code": "rate_limited"}  # window {1,1,2} already has 2
    assert outcomes[3][1] == {"code": "rate_limited"}  # window {1,2,3} still has 2
    assert outcomes[4][1] is None  # round 4: window slides to {2,3,4}, only 1 used

    ls_call = _call("registry", "list_servers", args={})
    hm.reset("duel-rates-2", world_id="world-demo")
    first = hm.check_before(ls_call)
    hm.begin_round(7)
    second = hm.check_before(ls_call)
    print(f"  registry.list_servers: 1st call -> {first}, 2nd call (different round) -> {second}")
    assert first is None and second == {"code": "rate_limited"}

    # -- mechanic 6: opaque errors ----------------------------------------------
    # A DEDICATED instance with the default opaque_enabled=True — this is
    # the one section of the demo actually exercising mechanic 6. Kept as
    # its own variable (not reassigning `hm`) so later sections keep using
    # the opaque-disabled `hm` untouched.
    print("\n--- mechanic 6: opaque errors (seeded, reproducible, never softened) ---")
    hm_opaque = HardMode(opaque_enabled=True)
    hm_opaque.reset("duel-opaque", world_id="world-demo")
    fired_at = None
    not_fired_at = None
    for idx in range(40):
        probe = _call("glossary", "define", args={"term": "field-mask", "lang": "vi"}, call_index=idx)
        err = hm_opaque.check_before(probe)
        if err == {"code": "unavailable"} and fired_at is None:
            fired_at = idx
        if err is None and not_fired_at is None:
            not_fired_at = idx
        if fired_at is not None and not_fired_at is not None:
            break
    print(f"  within 40 probes: first fire at call_index={fired_at}, first pass at call_index={not_fired_at}")
    assert fired_at is not None, "opaque mechanic never fired in 40 probes"
    assert not_fired_at is not None, "opaque mechanic fired on EVERY probe (does not fire spuriously check)"
    fired_err = hm_opaque.check_before(
        _call("glossary", "define", args={"term": "field-mask", "lang": "vi"}, call_index=fired_at)
    )
    assert set(fired_err.keys()) == {"code"}
    replay = hm_opaque.check_before(
        _call("glossary", "define", args={"term": "field-mask", "lang": "vi"}, call_index=fired_at)
    )
    print(f"  same call_index replayed -> identical outcome: {replay}")
    assert replay == {"code": "unavailable"}

    # -- mechanic 4: partial results --------------------------------------------
    print("\n--- mechanic 4: partial results ---")
    hm.reset("duel-partial", world_id="world-demo")
    many_rows_call = _call("slides", "query", args={"q": "x"}, call_index=99)
    many_rows_result = ToolResult(
        ok=True,
        rows=tuple({"anchor": f"Frame:deadbeef/w/{i:03d}", "title": f"t{i}"} for i in range(7)),
        anchors=tuple(f"Frame:deadbeef/w/{i:03d}" for i in range(7)),
        cost=8,
    )
    shaped = hm.record_after(many_rows_call, many_rows_result)
    print(
        f"  7 rows in -> partial={shaped.partial}, {len(shaped.rows)} rows out, "
        f"continuation={shaped.continuation!r}"
    )
    assert shaped.partial is True
    assert len(shaped.rows) == PARTIAL_ROW_THRESHOLD
    assert shaped.continuation is not None

    few_rows_call = _call("slides", "query", args={"q": "y"}, call_index=1)
    few_rows_result = ToolResult(
        ok=True, rows=({"anchor": "Frame:deadbeef/w/001", "title": "t"},), cost=2,
    )
    shaped_few = hm.record_after(few_rows_call, few_rows_result)
    print(f"  1 row in, no degraded-replica seed hit -> partial={shaped_few.partial}")
    # (not asserted deterministically here — the degraded-replica seed can
    # still legitimately fire; tests/test_hardmode.py pins an exact seed.)

    # -- mechanic 7: language negotiation ----------------------------------------
    print("\n--- mechanic 7: language negotiation ---")
    try:
        from kit.world.fixture import build_fixture_world
        from kit.world.loader import World
    except ImportError as exc:  # pragma: no cover - a collaborator's file
        print(f"  kit.world not importable yet ({exc}) — skipping the live demo")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            world_dir = build_fixture_world(Path(tmp) / "w", include_truth=False)
            world = World.load(world_dir)
            hm_lang = HardMode(world=world, opaque_enabled=False)
            hm_lang.reset("duel-lang", world_id="world-demo")

            honest_vi = _call("glossary", "define", args={"term": "endpoint", "lang": "vi"})
            assert hm_lang.check_before(honest_vi) is None
            honest_result = ToolResult(
                ok=True, rows=({"definition": "..."},), anchors=("Glossary:endpoint-mcp",), cost=1,
            )
            passthrough = hm_lang.record_after(honest_vi, honest_result)
            print(f"  lang='vi' (correct, exists) -> unchanged anchors={passthrough.anchors}")
            assert passthrough.anchors == ("Glossary:endpoint-mcp",)

            wrong_lang = _call("glossary", "define", args={"term": "endpoint", "lang": "fr"})
            assert hm_lang.check_before(wrong_lang) is None
            naive_result = ToolResult(ok=True, rows=({"definition": "n/a"},), anchors=(), cost=1)
            negotiated = hm_lang.record_after(wrong_lang, naive_result)
            print(
                f"  lang='fr' (invalid) -> ok={negotiated.ok}, "
                f"anchors={negotiated.anchors}, no error"
            )
            assert negotiated.ok is True
            assert negotiated.anchors == ("Glossary:endpoint-mcp",)

            missing_lang = _call("glossary", "define", args={"term": "endpoint"})
            assert hm_lang.check_before(missing_lang) is None
            negotiated_missing = hm_lang.record_after(missing_lang, naive_result)
            print(f"  lang missing entirely -> anchors={negotiated_missing.anchors}, no error")
            assert negotiated_missing.ok is True
            assert negotiated_missing.anchors == ("Glossary:endpoint-mcp",)

    # -- mechanic 8: deprecation --------------------------------------------------
    print("\n--- mechanic 8: deprecation ---")
    hm.reset("duel-deprecation", world_id="world-demo")
    search_dep = _call("slides", "search", args={"q": "x"})
    assert hm.check_before(search_dep) is None
    dep_raw = ToolResult(ok=True, rows=({"anchor": "Frame:deadbeef/w/001", "title": "t"},), cost=2)
    dep_shaped = hm.record_after(search_dep, dep_raw)
    print(f"  slides.search -> deprecated={dep_shaped.deprecated}, successor={dep_shaped.successor!r}")
    assert dep_shaped.deprecated is True
    assert dep_shaped.successor == "slides.query"

    query_not_dep = _call("slides", "query", args={"q": "x"})
    assert hm.check_before(query_not_dep) is None
    not_dep_shaped = hm.record_after(query_not_dep, ToolResult(ok=True, rows=({"title": "t"},), cost=2))
    print(f"  slides.query    -> deprecated={not_dep_shaped.deprecated}")
    assert not_dep_shaped.deprecated is False

    print("\nAll kit/mcp/hardmode.py demos passed.")
    sys.exit(0)
