"""tests/test_hardmode.py — the eight FINAL-PLAN.md 4.2 hard-mode mechanics
(kit/mcp/hardmode.py). Every mechanic gets a test proving it FIRES and a
test proving it does not fire spuriously, per the task brief.

pytest only (permitted in tests/ per the workspace's hard rules). No
network, no unseeded randomness — every seeded assertion here either pins
an exact `(world_id, duel_id, round, call_index)` combination computed
ahead of time via the module's own `_seeded_int`, or brute-force-searches
for one using a throwaway probe (never the instance under test) so the
probing itself has no side effects on the state being asserted against.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the repo root importable when pytest is invoked from anywhere.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from kit.mcp.errors import ErrorCode
from kit.mcp.hardmode import (
    DEGRADED_REPLICA_DENOMINATOR,
    LEASE_SUBSEQUENT_CALLS,
    OPAQUE_DENOMINATOR,
    PARTIAL_ROW_THRESHOLD,
    HardMode,
    _seeded_int,
)
from kit.mcp.specs import TOOL_SPECS, cost as spec_cost
from kit.mcp.types import ToolCall, ToolResult

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _call(server: str, tool: str, **kw: object) -> ToolCall:
    kw.setdefault("args", {})
    return ToolCall(server=server, tool=tool, **kw)  # type: ignore[arg-type]


def _hm(**kw: object) -> HardMode:
    """A HardMode with both seeded mechanics OFF by default, so a test that
    is not specifically about mechanic 4's degraded-replica path or
    mechanic 6's opaque path is not incidentally flaky on whichever
    `(world_id, duel_id, round, call_index)` it happens to use. Tests for
    those two mechanics explicitly pass `opaque_enabled=True` /
    `degraded_replica_enabled=True`."""
    kw.setdefault("opaque_enabled", False)
    kw.setdefault("degraded_replica_enabled", False)
    return HardMode(**kw)  # type: ignore[arg-type]


def _first_seeded_hit(
    salt: str, server: str, tool: str, *, world_id: str, duel_id: str, round_no: int,
    modulus: int, want_hit: bool, start: int = 0, tries: int = 500,
) -> int:
    """Brute-force the first `call_index` in `[start, start+tries)` for
    which the module's OWN seed formula does (`want_hit=True`) or does not
    (`want_hit=False`) land on 0 mod `modulus` — computed directly, with NO
    HardMode instance involved, so this search has no state side effects on
    whatever instance a test goes on to build."""
    for idx in range(start, start + tries):
        hit = _seeded_int(world_id, duel_id, round_no, salt, server, tool, idx, modulus=modulus) == 0
        if hit is want_hit:
            return idx
    raise AssertionError(
        f"no call_index in [{start}, {start + tries}) gives want_hit={want_hit} "
        f"for salt={salt!r} {server}.{tool} round={round_no}"
    )


# ---------------------------------------------------------------------------
# Lifecycle — CONTRACTS.md 4.3: reset explicitly at duel start; state
# persists across rounds; leases specifically do not.
# ---------------------------------------------------------------------------


def test_check_before_requires_reset_first() -> None:
    hm = _hm()
    with pytest.raises(RuntimeError):
        hm.check_before(_call("slides", "query"))


def test_record_after_requires_reset_first() -> None:
    hm = _hm()
    with pytest.raises(RuntimeError):
        hm.record_after(_call("slides", "query"), ToolResult(ok=True, cost=1))


def test_begin_round_requires_reset_first() -> None:
    hm = _hm()
    with pytest.raises(RuntimeError):
        hm.begin_round(2)


def test_reset_rejects_empty_duel_id() -> None:
    hm = _hm()
    with pytest.raises(ValueError):
        hm.reset("")


@pytest.mark.parametrize("bad_round", [0, 11, -1])
def test_reset_rejects_out_of_range_starting_round(bad_round: int) -> None:
    hm = _hm()
    with pytest.raises(ValueError):
        hm.reset("duel-x", starting_round=bad_round)


@pytest.mark.parametrize("bad_round", [0, 11])
def test_begin_round_rejects_out_of_range(bad_round: int) -> None:
    hm = _hm()
    hm.reset("duel-x")
    with pytest.raises(ValueError):
        hm.begin_round(bad_round)


def test_reset_clears_everything_including_rate_windows_and_etags() -> None:
    hm = _hm()
    hm.reset("duel-a")
    ls_call = _call("registry", "list_servers")
    assert hm.check_before(ls_call) is None  # consumes the 1-per-duel slot
    assert hm.check_before(ls_call) == {"code": "rate_limited"}

    hm.reset("duel-b")  # a brand new duel: the slot must be back
    assert hm.check_before(ls_call) is None


def test_begin_round_clears_leases_but_not_rate_windows_or_idempotency() -> None:
    hm = _hm()
    hm.reset("duel-c")
    search_call = _call("slides", "search", call_index=0)
    assert hm.check_before(search_call) is None
    minted = hm.record_after(
        search_call, ToolResult(ok=True, rows=({"anchor": "Frame:deadbeef/w/001", "title": "t"},), cost=2)
    )
    assert minted.lease_id is not None

    # a rate-limited tool's usage this round should still be visible after
    # begin_round advances (not reset) the duel
    ls_call = _call("registry", "list_servers")
    assert hm.check_before(ls_call) is None

    hm.begin_round(2)

    # the lease from round 1 is gone
    gf = _call(
        "slides", "get_frame", args={"anchor": "Frame:deadbeef/w/001"},
        lease_id=minted.lease_id, call_index=1,
    )
    assert hm.check_before(gf) == {"code": "lease_required"}

    # but registry.list_servers' duel-lifetime "1 per duel" budget persists
    assert hm.check_before(ls_call) == {"code": "rate_limited"}


# ---------------------------------------------------------------------------
# Mechanic 1 — dynamic cost: pure delegation to kit.mcp.specs.cost_of
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "server,tool,fields,n_rows",
    [
        ("slides", "query", ("title", "body"), 1),
        ("slides", "get_frame", ("*",), 0),
        ("registry", "list_servers", ("name",), 1),
        ("glossary", "define", (), 1),
    ],
)
def test_cost_of_matches_specs_cost_of_exactly(server, tool, fields, n_rows) -> None:
    hm = _hm()
    call = _call(server, tool, fields=fields)
    assert hm.cost_of(call, n_rows) == spec_cost(server, tool, fields=fields, n_rows=n_rows)


def test_deny_result_charges_the_n_rows_zero_cost() -> None:
    hm = _hm()
    hm.reset("duel-cost")
    call = _call("slides", "get_frame", args={"anchor": "Frame:deadbeef/w/001"}, fields=("*",))
    err = {"code": "lease_required"}
    denied = hm.deny_result(call, err)
    assert denied.ok is False
    assert denied.error == err
    assert denied.cost == spec_cost("slides", "get_frame", fields=("*",), n_rows=0)
    assert denied.cost > 0  # CONTRACTS.md 3.3: "Cost is still charged"


def test_check_before_unknown_tool_returns_bad_request_not_a_crash() -> None:
    hm = _hm()
    hm.reset("duel-unknown")
    err = hm.check_before(_call("nope", "nope"))
    assert err["code"] == "bad_request"


def test_check_before_then_deny_result_composes_for_an_unknown_tool() -> None:
    """The exact composition the module's own docstring recommends:
    `err = check_before(call); if err: return deny_result(call, err)`.
    `deny_result` must not crash by calling straight back into
    `TOOL_SPECS[(call.server, call.tool)]` for the one call `check_before`
    was deliberately defensive about."""
    hm = _hm()
    hm.reset("duel-unknown-2")
    call = _call("nope", "nope")
    err = hm.check_before(call)
    assert err is not None
    result = hm.deny_result(call, err)
    assert result.ok is False
    assert result.error == err
    assert result.cost == 0


# ---------------------------------------------------------------------------
# Mechanic 2 — leases (fires / does not fire)
# ---------------------------------------------------------------------------


def test_get_frame_with_no_lease_id_fires_lease_required() -> None:
    hm = _hm()
    hm.reset("duel-lease-1")
    err = hm.check_before(
        _call("slides", "get_frame", args={"anchor": "Frame:deadbeef/w/001"}, call_index=0)
    )
    assert err == {"code": "lease_required"}


def test_get_frame_with_a_forged_lease_id_fires_lease_required() -> None:
    """Mechanic 2: "Leases cannot be minted by the caller." A self-invented
    id that was never returned by search/query must fail exactly like no
    id at all — never treated as "expired" (that would imply it was once
    real)."""
    hm = _hm()
    hm.reset("duel-lease-2")
    err = hm.check_before(
        _call(
            "slides", "get_frame", args={"anchor": "Frame:deadbeef/w/001"},
            lease_id="lse_i_made_this_up", call_index=1,
        )
    )
    assert err == {"code": "lease_required"}


def test_search_mints_a_lease_only_on_success() -> None:
    hm = _hm()
    hm.reset("duel-lease-3")
    call = _call("slides", "search", call_index=0)
    assert hm.check_before(call) is None
    result = hm.record_after(
        call, ToolResult(ok=True, rows=({"anchor": "Frame:deadbeef/w/001", "title": "t"},), cost=2)
    )
    assert result.lease_id is not None
    assert result.lease_id.startswith("lse_")


def test_query_also_mints_a_lease() -> None:
    hm = _hm()
    hm.reset("duel-lease-3b")
    call = _call("slides", "query", args={"q": "x"}, call_index=0)
    assert hm.check_before(call) is None
    result = hm.record_after(call, ToolResult(ok=True, rows=({"title": "t"},), cost=2))
    assert result.lease_id is not None


@pytest.mark.parametrize("offset", [1, 2, LEASE_SUBSEQUENT_CALLS])
def test_lease_is_live_for_exactly_three_subsequent_calls(offset: int) -> None:
    """CONTRACTS.md 4.2 mechanic 2 / kit/world/fixture.py's own glossary
    text: a lease minted at call_index K is usable at K+1..K+3."""
    hm = _hm()
    hm.reset("duel-lease-4")
    search = _call("slides", "search", call_index=0)
    hm.check_before(search)
    minted = hm.record_after(
        search, ToolResult(ok=True, rows=({"anchor": "Frame:deadbeef/w/001", "title": "t"},), cost=2)
    )
    err = hm.check_before(
        _call(
            "slides", "get_frame", args={"anchor": "Frame:deadbeef/w/001"},
            lease_id=minted.lease_id, call_index=offset,
        )
    )
    assert err is None


def test_lease_expires_on_the_fourth_subsequent_call() -> None:
    """Pinned to `kit/world/fixture.py`'s own text: "gọi lần thứ tư trả về
    lease_expired" — the 4th subsequent call, i.e. mint_index + 4."""
    hm = _hm()
    hm.reset("duel-lease-5")
    search = _call("slides", "search", call_index=0)
    hm.check_before(search)
    minted = hm.record_after(
        search, ToolResult(ok=True, rows=({"anchor": "Frame:deadbeef/w/001", "title": "t"},), cost=2)
    )
    err = hm.check_before(
        _call(
            "slides", "get_frame", args={"anchor": "Frame:deadbeef/w/001"},
            lease_id=minted.lease_id, call_index=LEASE_SUBSEQUENT_CALLS + 1,
        )
    )
    assert err == {"code": "lease_expired"}


def test_lease_does_not_survive_a_round_boundary() -> None:
    hm = _hm()
    hm.reset("duel-lease-6")
    search = _call("slides", "search", call_index=0)
    hm.check_before(search)
    minted = hm.record_after(
        search, ToolResult(ok=True, rows=({"anchor": "Frame:deadbeef/w/001", "title": "t"},), cost=2)
    )
    hm.begin_round(2)
    err = hm.check_before(
        _call(
            "slides", "get_frame", args={"anchor": "Frame:deadbeef/w/001"},
            lease_id=minted.lease_id, call_index=1,
        )
    )
    assert err == {"code": "lease_required"}


def test_a_tool_that_does_not_need_a_lease_never_fires_this_mechanic() -> None:
    """Does-not-fire-spuriously: slides.query itself has no `needs_lease`
    flag (only get_frame does) — calling it with no lease_id at all must
    never be gated on this mechanic."""
    hm = _hm()
    hm.reset("duel-lease-7")
    assert TOOL_SPECS[("slides", "query")].needs_lease is False
    err = hm.check_before(_call("slides", "query", args={"q": "x"}, call_index=0))
    assert err is None


# ---------------------------------------------------------------------------
# Mechanic 3 — preconditions (fires / does not fire)
# ---------------------------------------------------------------------------


_WRITE_ANCHOR = "Frame:deadbeef/w/001"


def _write_call(**headers: str) -> ToolCall:
    return _call(
        "progress", "record_mastery",
        args={"anchor": _WRITE_ANCHOR, "learner": "learner:sv-0417"},
        headers=headers,
    )


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"if-match": "sha256:aaaaaaaaaaaaaaaa"},  # missing idempotency-key
        {"idempotency-key": "idem-1"},  # missing if-match
    ],
)
def test_write_missing_either_header_fires_precondition_missing(headers: dict) -> None:
    hm = _hm()
    hm.reset("duel-write-1")
    err = hm.check_before(_write_call(**headers))
    assert err == {"code": "precondition_missing"}


def test_write_with_a_never_issued_etag_fires_conflict() -> None:
    hm = _hm()
    hm.reset("duel-write-2")
    err = hm.check_before(
        _write_call(**{"if-match": "sha256:aaaaaaaaaaaaaaaa", "idempotency-key": "idem-1"})
    )
    assert err == {"code": "conflict"}


def test_write_with_a_stale_etag_fires_conflict() -> None:
    hm = _hm()
    hm.reset("duel-write-3")
    prov = _call("registry", "provenance", args={"anchor": _WRITE_ANCHOR})
    hm.check_before(prov)
    hm.record_after(prov, ToolResult(ok=True, rows=({"etag": "sha256:aaaaaaaaaaaaaaaa"},), cost=1))

    err = hm.check_before(
        _write_call(**{"if-match": "sha256:bbbbbbbbbbbbbbbb", "idempotency-key": "idem-1"})
    )
    assert err == {"code": "conflict"}


def test_write_after_reading_provenance_with_a_matching_etag_passes() -> None:
    hm = _hm()
    hm.reset("duel-write-4")
    prov = _call("registry", "provenance", args={"anchor": _WRITE_ANCHOR})
    hm.check_before(prov)
    hm.record_after(prov, ToolResult(ok=True, rows=({"etag": "sha256:aaaaaaaaaaaaaaaa"},), cost=1))

    err = hm.check_before(
        _write_call(**{"if-match": "sha256:aaaaaaaaaaaaaaaa", "idempotency-key": "idem-1"})
    )
    assert err is None


def test_retrying_a_write_with_the_same_idempotency_key_fires_conflict() -> None:
    """Mechanic 3: "Retrying a write without re-reading is a write_violation"
    — HardMode's mechanical enforcement of that is `conflict` on a reused
    key, forcing a fresh `registry.provenance` read before the next try."""
    hm = _hm()
    hm.reset("duel-write-5")
    prov = _call("registry", "provenance", args={"anchor": _WRITE_ANCHOR})
    hm.check_before(prov)
    hm.record_after(prov, ToolResult(ok=True, rows=({"etag": "sha256:aaaaaaaaaaaaaaaa"},), cost=1))

    good = _write_call(**{"if-match": "sha256:aaaaaaaaaaaaaaaa", "idempotency-key": "idem-reused"})
    assert hm.check_before(good) is None

    replay = _write_call(**{"if-match": "sha256:aaaaaaaaaaaaaaaa", "idempotency-key": "idem-reused"})
    assert hm.check_before(replay) == {"code": "conflict"}


def test_a_read_only_tool_never_fires_this_mechanic() -> None:
    hm = _hm()
    hm.reset("duel-write-6")
    assert TOOL_SPECS[("slides", "query")].is_write is False
    err = hm.check_before(_call("slides", "query", args={"q": "x"}))
    assert err is None


# ---------------------------------------------------------------------------
# Mechanic 5 — rate windows, cross-ROUND (fires / does not fire)
# ---------------------------------------------------------------------------


def test_verify_source_allows_two_then_blocks_the_third_within_the_window() -> None:
    hm = _hm()
    hm.reset("duel-rate-1")
    call = _call("citation-checker", "verify_source", args={"url": "https://fixture.example/x"})

    hm.begin_round(1)
    assert hm.check_before(call) is None
    assert hm.check_before(call) is None
    hm.begin_round(2)
    assert hm.check_before(call) == {"code": "rate_limited"}
    hm.begin_round(3)
    assert hm.check_before(call) == {"code": "rate_limited"}


def test_verify_source_window_slides_and_reopens() -> None:
    """The exact sequence the advisor pinned: 2 used in round 1 (window
    {1,1}), blocked in rounds 2 and 3 (window still contains both round-1
    calls), then ALLOWED again in round 4 once the window slides to
    {2,3,4} and only 0 calls remain inside it."""
    hm = _hm()
    hm.reset("duel-rate-2")
    call = _call("citation-checker", "verify_source", args={"url": "https://fixture.example/x"})
    outcomes = []
    for round_no in (1, 1, 2, 3, 4):
        hm.begin_round(round_no)
        outcomes.append(hm.check_before(call))
    assert outcomes == [None, None, {"code": "rate_limited"}, {"code": "rate_limited"}, None]


def test_list_servers_allows_exactly_one_per_duel() -> None:
    hm = _hm()
    hm.reset("duel-rate-3")
    call = _call("registry", "list_servers")
    hm.begin_round(1)
    assert hm.check_before(call) is None
    hm.begin_round(7)  # a totally different round — still the same duel
    assert hm.check_before(call) == {"code": "rate_limited"}


def test_a_tool_with_no_rate_limit_is_never_blocked() -> None:
    hm = _hm()
    hm.reset("duel-rate-4")
    assert TOOL_SPECS[("glossary", "define")].rate_limit is None
    call = _call("glossary", "define", args={"term": "field-mask", "lang": "vi"})
    for round_no in range(1, 11):
        hm.begin_round(round_no)
        for _ in range(5):
            assert hm.check_before(call) is None


def test_an_opaque_denial_does_not_consume_a_rate_window_slot() -> None:
    """Advisor correction: slot consumption happens only at final allow, so
    an opaque-killed call must not shrink citation-checker.verify_source's
    2-per-3-rounds budget. Pin an exact call_index where the seed fires
    opaque, then prove two ordinary (non-opaque) calls still both succeed
    afterward in the same round."""
    world_id, duel_id, round_no = "world-x", "duel-rate-opaque", 1
    opaque_idx = _first_seeded_hit(
        "opaque", "citation-checker", "verify_source",
        world_id=world_id, duel_id=duel_id, round_no=round_no,
        modulus=OPAQUE_DENOMINATOR, want_hit=True,
    )
    # find two DIFFERENT non-opaque indices, not equal to opaque_idx, to
    # drive the real 2-per-3-rounds budget afterward
    non_opaque = []
    probe = 0
    while len(non_opaque) < 2:
        if probe != opaque_idx and _seeded_int(
            world_id, duel_id, round_no, "opaque", "citation-checker", "verify_source", probe,
            modulus=OPAQUE_DENOMINATOR,
        ) != 0:
            non_opaque.append(probe)
        probe += 1

    hm = HardMode(opaque_enabled=True, degraded_replica_enabled=False)
    hm.reset(duel_id, world_id=world_id, starting_round=round_no)
    call = _call("citation-checker", "verify_source", args={"url": "https://fixture.example/x"})

    opaque_result = hm.check_before(_call(
        "citation-checker", "verify_source", args={"url": "https://fixture.example/x"},
        call_index=opaque_idx,
    ))
    assert opaque_result == {"code": "unavailable"}

    for idx in non_opaque:
        allowed = hm.check_before(_call(
            "citation-checker", "verify_source", args={"url": "https://fixture.example/x"},
            call_index=idx,
        ))
        assert allowed is None, f"call_index={idx} should still be within the 2-per-3-rounds budget"

    # the THIRD real attempt (opaque one didn't count) must now be blocked
    third = hm.check_before(_call(
        "citation-checker", "verify_source", args={"url": "https://fixture.example/x"},
        call_index=99999,
    ))
    assert third == {"code": "rate_limited"}


# ---------------------------------------------------------------------------
# Mechanic 6 — opaque errors (fires / does not fire spuriously)
# ---------------------------------------------------------------------------


def test_opaque_error_fires_for_some_call_and_not_for_others() -> None:
    hm = HardMode(opaque_enabled=True, degraded_replica_enabled=False)
    hm.reset("duel-opaque-1", world_id="world-x")
    fired = False
    passed = False
    for idx in range(60):
        err = hm.check_before(_call("glossary", "define", args={"term": "field-mask", "lang": "vi"}, call_index=idx))
        if err == {"code": "unavailable"}:
            fired = True
        elif err is None:
            passed = True
        if fired and passed:
            break
    assert fired, "opaque mechanic never fired across 60 probes"
    assert passed, "opaque mechanic fired on every single probe (never lets a good call through)"


def test_opaque_error_shape_is_exactly_code_only() -> None:
    world_id, duel_id, round_no = "world-x", "duel-opaque-2", 1
    idx = _first_seeded_hit(
        "opaque", "glossary", "define",
        world_id=world_id, duel_id=duel_id, round_no=round_no,
        modulus=OPAQUE_DENOMINATOR, want_hit=True,
    )
    hm = HardMode(opaque_enabled=True, degraded_replica_enabled=False)
    hm.reset(duel_id, world_id=world_id, starting_round=round_no)
    err = hm.check_before(_call("glossary", "define", args={"term": "field-mask", "lang": "vi"}, call_index=idx))
    assert err == {"code": "unavailable"}
    assert set(err.keys()) == {"code"}  # CONTRACTS.md 3.3: no reason/detail/message, ever


def test_opaque_error_is_reproducible_on_replay() -> None:
    """G-REPRO (CONTRACTS.md section 11): the identical
    (world_id, duel_id, round, server, tool, call_index) tuple must flip
    the same way every time — a fresh HardMode, replayed, agrees."""
    world_id, duel_id, round_no = "world-x", "duel-opaque-3", 1
    idx = _first_seeded_hit(
        "opaque", "glossary", "define",
        world_id=world_id, duel_id=duel_id, round_no=round_no,
        modulus=OPAQUE_DENOMINATOR, want_hit=True,
    )
    outcomes = []
    for _ in range(3):
        hm = HardMode(opaque_enabled=True, degraded_replica_enabled=False)
        hm.reset(duel_id, world_id=world_id, starting_round=round_no)
        outcomes.append(hm.check_before(
            _call("glossary", "define", args={"term": "field-mask", "lang": "vi"}, call_index=idx)
        ))
    assert outcomes == [{"code": "unavailable"}] * 3


def test_opaque_disabled_never_fires() -> None:
    """Does-not-fire-spuriously, the toggle's own guarantee: with
    opaque_enabled=False (this module's default helper `_hm()`), no
    otherwise-valid call is ever rejected by this mechanic, regardless of
    what the seed would have said."""
    hm = _hm()  # opaque_enabled=False
    hm.reset("duel-opaque-4", world_id="world-x")
    for idx in range(60):
        err = hm.check_before(_call("glossary", "define", args={"term": "field-mask", "lang": "vi"}, call_index=idx))
        assert err is None


def test_opaque_never_preempts_a_structurally_invalid_call() -> None:
    """Priority order: lease -> precondition -> rate -> opaque. A call that
    was already going to fail for a real reason must fail for THAT reason,
    never be relabelled `unavailable` by the coin flip."""
    hm = HardMode(opaque_enabled=True, degraded_replica_enabled=False)
    hm.reset("duel-opaque-5", world_id="world-x")
    for idx in range(30):
        err = hm.check_before(
            _call("slides", "get_frame", args={"anchor": "Frame:deadbeef/w/001"}, call_index=idx)
        )
        assert err == {"code": "lease_required"}


# ---------------------------------------------------------------------------
# Mechanic 4 — partial results (fires / does not fire)
# ---------------------------------------------------------------------------


def test_partial_fires_and_truncates_when_over_the_row_threshold() -> None:
    hm = _hm()
    hm.reset("duel-partial-1")
    call = _call("slides", "query", args={"q": "x"}, call_index=0)
    hm.check_before(call)
    n = PARTIAL_ROW_THRESHOLD + 4
    raw = ToolResult(
        ok=True,
        rows=tuple({"title": f"t{i}"} for i in range(n)),
        anchors=tuple(f"Frame:deadbeef/w/{i:03d}" for i in range(n)),
        cost=8,
    )
    shaped = hm.record_after(call, raw)
    assert shaped.partial is True
    assert len(shaped.rows) == PARTIAL_ROW_THRESHOLD
    assert list(shaped.rows) == list(raw.rows[:PARTIAL_ROW_THRESHOLD])  # order preserved
    assert shaped.continuation is not None
    assert isinstance(shaped.continuation, str) and shaped.continuation


def test_partial_does_not_fire_at_or_under_the_threshold() -> None:
    hm = _hm()  # degraded_replica_enabled=False: isolate the row-count rule
    hm.reset("duel-partial-2")
    call = _call("slides", "query", args={"q": "x"}, call_index=0)
    hm.check_before(call)
    raw = ToolResult(
        ok=True,
        rows=tuple({"title": f"t{i}"} for i in range(PARTIAL_ROW_THRESHOLD)),
        cost=4,
    )
    shaped = hm.record_after(call, raw)
    assert shaped.partial is False
    assert shaped.continuation is None
    assert len(shaped.rows) == PARTIAL_ROW_THRESHOLD  # nothing truncated


def test_partial_restamps_cost_after_truncation() -> None:
    """Advisor correction: cost must be recomputed from the FINAL row count,
    not the raw one, so a truncated result is priced at what the student
    actually received."""
    hm = _hm()
    hm.reset("duel-partial-3")
    call = _call("slides", "query", args={"q": "x"}, fields=("title",), call_index=0)
    hm.check_before(call)
    n = PARTIAL_ROW_THRESHOLD + 5
    raw = ToolResult(ok=True, rows=tuple({"title": f"t{i}"} for i in range(n)), cost=99999)
    shaped = hm.record_after(call, raw)
    assert shaped.cost == spec_cost("slides", "query", fields=("title",), n_rows=PARTIAL_ROW_THRESHOLD)


def test_partial_degraded_replica_path_fires_independent_of_row_count() -> None:
    world_id, duel_id, round_no = "world-x", "duel-partial-4", 1
    idx = _first_seeded_hit(
        "degraded_replica", "slides", "query",
        world_id=world_id, duel_id=duel_id, round_no=round_no,
        modulus=DEGRADED_REPLICA_DENOMINATOR, want_hit=True,
    )
    hm = HardMode(opaque_enabled=False, degraded_replica_enabled=True)
    hm.reset(duel_id, world_id=world_id, starting_round=round_no)
    call = _call("slides", "query", args={"q": "x"}, call_index=idx)
    hm.check_before(call)
    raw = ToolResult(ok=True, rows=({"title": "only one row"},), cost=2)
    shaped = hm.record_after(call, raw)
    assert shaped.partial is True
    # under threshold: not truncated, just flagged
    assert len(shaped.rows) == 1


def test_partial_toggle_off_suppresses_the_degraded_replica_path() -> None:
    world_id, duel_id, round_no = "world-x", "duel-partial-5", 1
    idx = _first_seeded_hit(
        "degraded_replica", "slides", "query",
        world_id=world_id, duel_id=duel_id, round_no=round_no,
        modulus=DEGRADED_REPLICA_DENOMINATOR, want_hit=True,
    )
    hm = HardMode(opaque_enabled=False, degraded_replica_enabled=False)
    hm.reset(duel_id, world_id=world_id, starting_round=round_no)
    call = _call("slides", "query", args={"q": "x"}, call_index=idx)
    hm.check_before(call)
    raw = ToolResult(ok=True, rows=({"title": "only one row"},), cost=2)
    shaped = hm.record_after(call, raw)
    assert shaped.partial is False


def test_partial_never_fires_on_an_error_result() -> None:
    hm = _hm()
    hm.reset("duel-partial-6")
    call = _call("slides", "query", args={"q": "x"})
    err_result = ToolResult(ok=False, error={"code": "not_found"}, cost=1)
    shaped = hm.record_after(call, err_result)
    assert shaped == err_result  # untouched — mechanics only reshape successes


# ---------------------------------------------------------------------------
# Mechanic 7 — language negotiation (needs kit.world; skipped gracefully —
# ONLY the tests that need it, never this whole file — if that
# collaborator module is not importable yet, per the workspace's
# degrade-gracefully rule). A module-level `pytest.importorskip` would skip
# every test below it in the file, including leases/preconditions/rate
# windows/opaque/partial/deprecation, which have nothing to do with
# kit.world — that would silently erase 50+ unrelated tests' coverage the
# moment this one collaborator import broke. `skipif` scoped to just the
# lang tests keeps the blast radius to the one mechanic that actually needs
# the dependency.
# ---------------------------------------------------------------------------

try:
    from kit.world import fixture as fixture_mod
    from kit.world import loader as world_mod
    _WORLD_IMPORTABLE = True
except ImportError:
    fixture_mod = None  # type: ignore[assignment]
    world_mod = None  # type: ignore[assignment]
    _WORLD_IMPORTABLE = False

_needs_world = pytest.mark.skipif(not _WORLD_IMPORTABLE, reason="kit.world is a collaborator module")


@pytest.fixture(scope="module")
def endpoint_world(tmp_path_factory):
    dest = tmp_path_factory.mktemp("hardmode-world")
    world_dir = fixture_mod.build_fixture_world(dest, include_truth=False)
    return world_mod.World.load(world_dir)


@_needs_world
def test_lang_negotiation_passes_through_unchanged_when_lang_is_correct(endpoint_world) -> None:
    hm = _hm(world=endpoint_world)
    hm.reset("duel-lang-1")
    call = _call("glossary", "define", args={"term": "endpoint", "lang": "vi"})
    hm.check_before(call)
    naive = ToolResult(ok=True, rows=({"definition": "..."},), anchors=("Glossary:endpoint-mcp",), cost=1)
    result = hm.record_after(call, naive)
    assert result == naive  # byte-identical passthrough: does not fire spuriously


@_needs_world
def test_lang_negotiation_passes_through_for_the_other_correct_lang_too(endpoint_world) -> None:
    hm = _hm(world=endpoint_world)
    hm.reset("duel-lang-1b")
    call = _call("glossary", "define", args={"term": "endpoint", "lang": "en"})
    hm.check_before(call)
    naive = ToolResult(ok=True, rows=({"definition": "..."},), anchors=("Glossary:endpoint-network",), cost=1)
    result = hm.record_after(call, naive)
    assert result == naive


@_needs_world
def test_lang_negotiation_fires_silently_on_a_wrong_lang(endpoint_world) -> None:
    hm = _hm(world=endpoint_world)
    hm.reset("duel-lang-2")
    call = _call("glossary", "define", args={"term": "endpoint", "lang": "fr"})
    hm.check_before(call)
    naive = ToolResult(ok=True, rows=({"definition": "n/a"},), anchors=(), cost=1)
    result = hm.record_after(call, naive)
    assert result.ok is True  # NO error — the whole point of this mechanic
    assert len(result.anchors) == 1
    assert result.anchors[0] in ("Glossary:endpoint-mcp", "Glossary:endpoint-network")
    assert result.rows[0]["definition"]  # a real, valid-looking definition


@_needs_world
def test_lang_negotiation_fires_silently_when_lang_is_missing(endpoint_world) -> None:
    hm = _hm(world=endpoint_world)
    hm.reset("duel-lang-3")
    call = _call("glossary", "define", args={"term": "endpoint"})  # no "lang" key at all
    hm.check_before(call)
    naive = ToolResult(ok=True, rows=({"definition": "n/a"},), anchors=(), cost=1)
    result = hm.record_after(call, naive)
    assert result.ok is True
    assert len(result.anchors) == 1


def test_lang_negotiation_is_a_noop_without_a_world() -> None:
    """world=None is documented as a graceful no-op — no bilingual data to
    substitute from, so the naive result passes through untouched even for
    a wrong lang."""
    hm = _hm()  # world=None
    hm.reset("duel-lang-4")
    call = _call("glossary", "define", args={"term": "endpoint", "lang": "fr"})
    hm.check_before(call)
    naive = ToolResult(ok=True, rows=({"definition": "n/a"},), anchors=(), cost=1)
    result = hm.record_after(call, naive)
    assert result == naive


@_needs_world
def test_lang_negotiation_leaves_non_glossary_tools_untouched(endpoint_world) -> None:
    hm = _hm(world=endpoint_world)
    hm.reset("duel-lang-5")
    call = _call("slides", "query", args={"q": "endpoint"})
    hm.check_before(call)
    naive = ToolResult(ok=True, rows=({"title": "t"},), cost=2)
    result = hm.record_after(call, naive)
    assert result.rows == naive.rows
    assert result.anchors == naive.anchors


# ---------------------------------------------------------------------------
# Mechanic 8 — deprecation stamping (fires / does not fire)
# ---------------------------------------------------------------------------


def test_slides_search_is_stamped_deprecated_with_its_successor() -> None:
    hm = _hm()
    hm.reset("duel-dep-1")
    call = _call("slides", "search", args={"q": "x"})
    hm.check_before(call)
    raw = ToolResult(ok=True, rows=({"anchor": "Frame:deadbeef/w/001", "title": "t"},), cost=2)
    shaped = hm.record_after(call, raw)
    assert shaped.deprecated is True
    assert shaped.successor == "slides.query"


def test_slides_query_is_not_stamped_deprecated() -> None:
    hm = _hm()
    hm.reset("duel-dep-2")
    call = _call("slides", "query", args={"q": "x"})
    hm.check_before(call)
    raw = ToolResult(ok=True, rows=({"title": "t"},), cost=2)
    shaped = hm.record_after(call, raw)
    assert shaped.deprecated is False
    assert shaped.successor is None


def test_deprecation_stamp_overrides_whatever_the_naive_executor_set() -> None:
    """HardMode is the single source of truth for this field — even if a
    (buggy) tool implementation forgot to set it, or set it wrong, the
    stamp is always TOOL_SPECS truth."""
    hm = _hm()
    hm.reset("duel-dep-3")
    call = _call("slides", "search", args={"q": "x"})
    hm.check_before(call)
    raw = ToolResult(
        ok=True, rows=({"anchor": "Frame:deadbeef/w/001", "title": "t"},), cost=2,
        deprecated=False,  # wrong on purpose
    )
    shaped = hm.record_after(call, raw)
    assert shaped.deprecated is True
    assert shaped.successor == "slides.query"


# ---------------------------------------------------------------------------
# Cross-cutting: unknown-tool result passthrough, ordering sanity
# ---------------------------------------------------------------------------


def test_record_after_leaves_an_unknown_tool_result_untouched() -> None:
    hm = _hm()
    hm.reset("duel-unknown-2")
    call = _call("nope", "nope")
    raw = ToolResult(ok=True, rows=({"x": 1},), cost=1)
    assert hm.record_after(call, raw) == raw


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
