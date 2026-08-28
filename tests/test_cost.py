"""tests/test_cost.py — the economy tests for kit/mcp/specs.py.

Covers, in order:
  1. The two field-mask examples CONTRACTS.md 3.4 states must hold exactly.
  2. Every named anchor price from this task's brief / FINAL-PLAN.md section 4.
  3. THE TEST THAT MATTERS: the acceptance arithmetic (FINAL-PLAN.md 4.3) — a
     disciplined round <= 11 cr, a rookie round >= 45 cr — computed from the
     table, never hardcoded.
  4. "No tool is dominated" as an actual, non-vacuous test over the spec table.
  5. Whole-table invariants that keep a future retune honest (ToolSpec's own
     __post_init__ already enforces most of these per-row at import time; a
     handful only make sense checked across the whole table, or are worth
     re-asserting here as documentation of the contract).
  6. cost_of()'s duck-typed CONTRACTS.md 3.1 ToolCall shape, exercised with a
     local stand-in — never importing kit.mcp.types.ToolCall (this file must
     not create a hard dependency on a collaborator's class; see specs.py's
     module docstring for the same resolution).

pytest only (permitted in tests/ per the workspace's hard rules). No network,
no unseeded randomness, no wall-clock.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

# Make the repo root importable when pytest is invoked from anywhere.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from kit.mcp.specs import (
    A2A_PEERS,
    MCP_SERVERS,
    ROUNDS_PER_DUEL,
    TOOL_SPECS,
    WRITE_HEADERS,
    cheapest_owners_of,
    cost,
    cost_of,
)


# ---------------------------------------------------------------------------
# 1. The two field-mask examples CONTRACTS.md 3.4 states verbatim.
# ---------------------------------------------------------------------------


def test_get_frame_field_mask_examples_hold_exactly() -> None:
    assert cost("slides", "get_frame", fields=("title",)) == 2
    assert cost("slides", "get_frame", fields=("*",)) == 9


def test_list_servers_field_mask_examples_hold_exactly() -> None:
    assert cost("registry", "list_servers", fields=("name",)) == 2
    assert cost("registry", "list_servers", fields=("*",)) == 12


# ---------------------------------------------------------------------------
# 2. Every named anchor price (default fields, n_rows=1) from the task brief.
# ---------------------------------------------------------------------------

_ANCHOR_PRICES = [
    ("slides", "search", 2),
    ("slides", "get_frame", 4),
    ("slides", "whatlinkshere", 2),
    ("glossary", "define", 1),
    ("glossary", "list_terms", 10),
    ("registry", "provenance", 1),
    ("registry", "list_servers", 12),
    ("content", "flag_stale_slide", 3),
    ("curriculum-analyst", "which_days_cover", 8),
    ("citation-checker", "verify_source", 6),
]


@pytest.mark.parametrize("server,tool,want", _ANCHOR_PRICES)
def test_anchor_price(server: str, tool: str, want: int) -> None:
    assert cost(server, tool) == want


def test_registry_provenance_is_the_cheapest_call_in_the_table() -> None:
    """FINAL-PLAN.md 4.2 mechanic 3 / this task's brief: provenance is
    "deliberately the cheapest call... the defender's workhorse" — its
    default-fields cost must be <= every other tool's default-fields cost."""
    provenance_cost = cost("registry", "provenance")
    for key in TOOL_SPECS:
        assert provenance_cost <= cost(*key), f"{key} costs less than registry.provenance by default"


# ---------------------------------------------------------------------------
# 3. THE TEST THAT MATTERS: the acceptance arithmetic, computed from the
#    table (FINAL-PLAN.md 4.3 / CONTRACTS.md 3.4), never hardcoded.
#
# D-10 (ENGINE-REPORT.md): under the *previous* table this disciplined round
# priced out at 11 cr, and 11 x 10 rounds = 110 > the 100-credit duel budget.
# FINAL-PLAN.md 4.3's "sustainable across 10 rounds on 100" was argued (lease
# / rate-limit amortization) but never measured — and the argument doesn't
# even reach this specific round, which holds no lease and calls no
# rate-limited tool. kit/mcp/specs.py's slides.query is retuned (see that
# module's D-10 comment) so the claim is actually true: 9 cr/round, 90 of
# 100 over 10 rounds, 10 cr of real headroom. Full derivation, the rookie
# line, and the domination proof are in Day26-Colosseum-Agent-Arena/ECONOMY.md.
# ---------------------------------------------------------------------------


def test_disciplined_round_is_sustainable() -> None:
    """A disciplined round: locate with a narrow mask (slides.query with
    exactly the fields the mockup in FINAL-PLAN.md section 8.2 shows,
    fields=[title,body]), read the frame at its default mask, and pull the
    etag for a future write's If-Match from the cheapest call in the table.
    CONTRACTS.md 3.4 / referee/detectors.py's independently-declared
    ROUND_ALLOWANCE pin the hard ceiling at <= 11 cr. This table's tuned
    value is exactly 9 — 2 cr under that ceiling, not sitting on it."""
    disciplined_total = (
        cost("slides", "query", fields=("title", "body"), n_rows=1)
        + cost("slides", "get_frame")  # default fields
        + cost("registry", "provenance")  # default fields (the etag)
    )
    assert disciplined_total <= 11  # CONTRACTS.md 3.4 / referee ROUND_ALLOWANCE ceiling
    assert disciplined_total == 9  # D-10: this table's tuned value (ECONOMY.md)


def test_disciplined_round_survives_a_full_duel_with_real_headroom() -> None:
    """THE D-10 FIX, stated as the mirror claim FINAL-PLAN.md 4.3 makes and
    the pre-fix table failed outright: disciplined_total * ROUNDS_PER_DUEL
    must fit inside the 100-credit duel budget, computed directly from the
    table rather than argued from amortization. 9 x 10 = 90 cr, leaving 10
    cr (~10% of the budget) of headroom — enough to absorb roughly two
    over-ceiling rounds (each up to 11 cr, i.e. up to +2 cr over the 9 cr
    average) without going bankrupt, but not so much that budget pressure
    stops being real for a team that plays carelessly even occasionally."""
    disciplined_total = (
        cost("slides", "query", fields=("title", "body"), n_rows=1)
        + cost("slides", "get_frame")
        + cost("registry", "provenance")
    )
    total_ten_rounds = disciplined_total * ROUNDS_PER_DUEL
    headroom = 100 - total_ten_rounds
    assert total_ten_rounds <= 100, f"{total_ten_rounds} cr > the 100-credit duel budget"
    assert total_ten_rounds == 90
    assert headroom == 10


def test_rookie_round_is_bankrupt_by_round_three() -> None:
    """FINAL-PLAN.md 4.3, verbatim recipe: "list_servers 12 + list_terms 10
    + three full get_frame 27 costs ~49 cr — bankrupt in round 3." A team
    with 100 credits spending >= 45/round cannot survive 3 rounds (3 x 45 =
    135 > 100), regardless of the exact total — that is the "does not lose
    because we rigged it; loses because it cannot afford itself" property.
    D-10's retune (slides.query only) does not touch any tool this recipe
    calls, so this line is unchanged: still 49."""
    rookie_total = (
        cost("registry", "list_servers", fields=("*",))
        + cost("glossary", "list_terms")  # bare call == full price by design
        + 3 * cost("slides", "get_frame", fields=("*",))
    )
    assert rookie_total >= 45
    assert rookie_total == 49  # unchanged by the D-10 retune (ECONOMY.md)
    assert 3 * rookie_total > 100  # bankrupt before round 3 completes


def test_rookie_round_cannot_survive_a_full_duel() -> None:
    """The point of the whole exercise, stated as arithmetic against the
    100-credit duel budget (CONTRACTS.md section 0 / FINAL-PLAN.md 4.1:
    "100 per duel side, across all 10 rounds"). Unlike the disciplined line
    (test_disciplined_round_survives_a_full_duel_with_real_headroom above,
    added for D-10), this needs no retune and no amortization argument: the
    rookie sequence is undisciplined by construction (full masks, no reuse,
    no lease/rate-limit reuse to amortize), so its bankruptcy is
    unconditional and was already true before D-10."""
    rookie_total = (
        cost("registry", "list_servers", fields=("*",))
        + cost("glossary", "list_terms")
        + 3 * cost("slides", "get_frame", fields=("*",))
    )
    assert rookie_total * ROUNDS_PER_DUEL > 100


# ---------------------------------------------------------------------------
# 4. "No tool is dominated" — as an actual, non-vacuous test.
#
# DEFINITION (this file's resolved reading of the task brief): a tool T is
# NOT dominated iff there exists at least one field f in T.all_fields such
# that T is a member of cheapest_owners_of(f) — i.e. among every tool in the
# WHOLE table that also exposes a field named f (singleton-mask cost,
# n_rows=1 pinned for comparability), T's cost for f is the minimum (ties
# count as a win: "weakly cheapest"). A field unique to one tool is a
# trivial, but real, win for it — nothing else can ever be cheaper at
# something nothing else offers. This is deliberately about literal field
# NAMES shared across (server, tool) pairs, not semantic task-level paths
# (e.g. "three MCP calls chained" vs. "one A2A call") — the latter is the
# *reason* FINAL-PLAN.md 4.1 gives for why curriculum-analyst was dominated
# under flat pricing, but it is not a property this data table can express
# on its own (chaining is the executor's/agent's business, not the spec
# table's).
#
# D-10 NOTE: the retune that fixed the disciplined-round headroom (section 3
# above / ECONOMY.md) dropped slides.query's title weight to 0, which
# incidentally ties slides.query with slides.get_frame on the `body` field
# (both now cost 4 for fields=[body], where get_frame used to win outright
# by 1 cr). That tie is asserted explicitly below, in
# test_field_name_overlaps_used_by_the_domination_test_are_real — it does
# not dominate get_frame (get_frame still ties for cheapest on `body`, which
# is all "not dominated" requires) and it does not touch the search-vs-query
# pairing in the next test (their only shared field is `title`, untouched by
# the body-weight change).
#
# `slides.search` is included in this check, not exempted: even
# though it is `deprecated: true` and flagged `wasteful` unconditionally the
# moment it is used (CONTRACTS.md 6.4's `wasteful` detector fires on the
# flag alone, never on price), the task brief says "for every tool" with no
# carve-out, and the deprecated tool is deliberately priced to still look
# like a bargain — the trap is that it is flagged despite being cheap, not
# that it is expensive. Below, search does hold its own on `anchor`,
# `snippet` and ties `title` against its successor `slides.query`.
# ---------------------------------------------------------------------------


def test_no_tool_dominated() -> None:
    for key, spec in sorted(TOOL_SPECS.items()):
        winning_fields = [f for f in spec.all_fields if key in cheapest_owners_of(f)]
        assert winning_fields, (
            f"{key[0]}.{key[1]} is dominated: no field in {spec.all_fields} is one it "
            "obtains at the table-wide minimum cost"
        )


def test_deprecated_search_is_not_cost_dominated_by_its_successor() -> None:
    """The specific pairing FINAL-PLAN.md 4.1/4.2 calls out by name: search
    vs. its successor query. Search must weakly win at least one field it
    shares with query (here: `title`), even though using it is `wasteful`
    for the unrelated, unconditional reason that it is flagged deprecated."""
    search_spec = TOOL_SPECS[("slides", "search")]
    query_spec = TOOL_SPECS[("slides", "query")]
    shared = set(search_spec.all_fields) & set(query_spec.all_fields)
    assert shared, "search and its successor query must share at least one field name to test domination on"
    assert any(
        cost("slides", "search", fields=(f,)) <= cost("slides", "query", fields=(f,)) for f in shared
    ), "slides.search is strictly cost-dominated by its successor on every shared field"


def test_field_name_overlaps_used_by_the_domination_test_are_real() -> None:
    """Guard against the domination test silently going vacuous (every tool
    trivially "winning" only because nothing else shares any of its field
    names). Assert the specific deliberate overlaps this table was tuned
    around actually exist and resolve the way the module docstring claims."""
    assert set(cheapest_owners_of("etag")) == {("registry", "provenance")}
    assert ("registry", "provenance") in cheapest_owners_of("etag")
    assert cost("registry", "provenance", fields=("etag",)) < cost("slides", "get_frame", fields=("etag",))

    # D-10 retune: get_frame and query are now an EXACT TIE on `body` (both
    # 4 cr), not a strict get_frame win. Before D-10, query's title weight
    # was 1 and this was `get_frame(4) < query(5)`; dropping title to 0 (see
    # kit/mcp/specs.py's D-10 comment) took query's body-only cost to 4 too.
    # get_frame is still a cheapest owner (ties count, per this test file's
    # own "weakly cheapest" definition above test_no_tool_dominated), so
    # get_frame is not dominated on `body` — it just no longer wins it alone.
    assert set(cheapest_owners_of("body")) == {("slides", "get_frame"), ("slides", "query")}
    assert ("slides", "get_frame") in cheapest_owners_of("body")
    assert cost("slides", "get_frame", fields=("body",)) == cost("slides", "query", fields=("body",)) == 4

    title_owners = cheapest_owners_of("title")
    assert ("slides", "search") in title_owners
    assert ("slides", "get_frame") in title_owners  # a genuine tie, not a bug

    assert ("research", "cite_source") in cheapest_owners_of("url")
    assert ("citation-checker", "verify_source") in cheapest_owners_of("url")


# ---------------------------------------------------------------------------
# 5. Whole-table invariants (retune safety net).
# ---------------------------------------------------------------------------


def test_all_seven_mcp_servers_and_three_a2a_peers_have_a_tool() -> None:
    assert MCP_SERVERS == {"slides", "glossary", "research", "labs", "progress", "content", "registry"}
    assert A2A_PEERS == {"curriculum-analyst", "citation-checker", "roster"}
    present_servers = {server for server, _ in TOOL_SPECS}
    assert MCP_SERVERS <= present_servers
    assert A2A_PEERS <= present_servers


def test_all_fields_matches_field_weight_keys_and_is_sorted() -> None:
    for key, spec in TOOL_SPECS.items():
        assert set(spec.all_fields) == set(spec.field_weight), key
        assert spec.all_fields == tuple(sorted(spec.all_fields)), key
        assert "*" not in spec.all_fields, key


def test_default_fields_is_a_sorted_subset_of_all_fields() -> None:
    for key, spec in TOOL_SPECS.items():
        assert set(spec.default_fields) <= set(spec.all_fields), key
        assert spec.default_fields == tuple(sorted(spec.default_fields)), key


def test_successor_resolves_to_a_real_non_deprecated_tool() -> None:
    deprecated_count = 0
    for key, spec in TOOL_SPECS.items():
        if spec.deprecated:
            deprecated_count += 1
            assert spec.successor is not None, key
            succ_server, _, succ_tool = spec.successor.partition(".")
            succ_key = (succ_server, succ_tool)
            assert succ_key in TOOL_SPECS, f"{key}: successor {spec.successor!r} not in TOOL_SPECS"
            assert not TOOL_SPECS[succ_key].deprecated, f"{key}: successor {spec.successor!r} is itself deprecated"
        else:
            assert spec.successor is None, key
    assert deprecated_count == 1  # exactly slides.search, today


def test_write_tools_require_exactly_if_match_and_idempotency_key() -> None:
    assert WRITE_HEADERS == ("idempotency-key", "if-match")
    for key, spec in TOOL_SPECS.items():
        if spec.is_write:
            assert spec.required_headers == WRITE_HEADERS, key
        else:
            assert spec.required_headers == (), key
    write_keys = {key for key, spec in TOOL_SPECS.items() if spec.is_write}
    assert write_keys == {("content", "flag_stale_slide"), ("progress", "record_mastery")}


def test_needs_lease_is_reserved_for_get_frame() -> None:
    lease_keys = {key for key, spec in TOOL_SPECS.items() if spec.needs_lease}
    assert lease_keys == {("slides", "get_frame")}


def test_rate_limits_match_the_named_windows() -> None:
    assert TOOL_SPECS[("citation-checker", "verify_source")].rate_limit == (2, 3)
    assert TOOL_SPECS[("registry", "list_servers")].rate_limit == (1, ROUNDS_PER_DUEL)
    limited = {key for key, spec in TOOL_SPECS.items() if spec.rate_limit is not None}
    assert limited == {("citation-checker", "verify_source"), ("registry", "list_servers")}


def test_all_weights_and_bases_are_nonnegative_ints() -> None:
    for key, spec in TOOL_SPECS.items():
        assert isinstance(spec.base, int) and spec.base >= 0, key
        assert isinstance(spec.row_weight, int) and spec.row_weight >= 0, key
        for field_name, weight in spec.field_weight.items():
            assert isinstance(weight, int) and weight >= 0, (key, field_name)


def test_cost_is_monotonic_in_requested_fields() -> None:
    """Asking for a superset of fields never costs less (weights are all
    >= 0), which is what makes "*" a meaningful upper bound per tool."""
    for key, spec in TOOL_SPECS.items():
        star_cost = cost(*key, fields=("*",))
        for f in spec.all_fields:
            assert cost(*key, fields=(f,)) <= star_cost, (key, f)


def test_bare_call_never_exceeds_full_mask() -> None:
    for key in TOOL_SPECS:
        assert cost(*key) <= cost(*key, fields=("*",)), key


# ---------------------------------------------------------------------------
# 6. cost_of()'s duck-typed CONTRACTS.md 3.1 ToolCall shape.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FakeToolCall:
    """A minimal local stand-in for kit/mcp/types.py's ToolCall — only the
    three attributes cost_of() actually reads. Deliberately NOT an import of
    the real class: this file must exercise the duck-typed contract, not a
    hard dependency on a collaborator's file (see specs.py's module
    docstring for the same resolution, applied here as the test's proof)."""

    server: str
    tool: str
    fields: tuple[str, ...] = ()


def test_cost_of_matches_cost_for_default_fields() -> None:
    call = _FakeToolCall(server="slides", tool="get_frame")
    assert cost_of(call, n_rows=1) == cost("slides", "get_frame", n_rows=1) == 4


def test_cost_of_matches_cost_for_explicit_mask() -> None:
    call = _FakeToolCall(server="slides", tool="get_frame", fields=("title",))
    assert cost_of(call, n_rows=1) == 2


def test_cost_of_expands_wildcard() -> None:
    call = _FakeToolCall(server="registry", tool="list_servers", fields=("*",))
    assert cost_of(call, n_rows=1) == 12


def test_cost_of_charges_row_weight_per_row() -> None:
    call = _FakeToolCall(server="slides", tool="query", fields=("title",))
    one_row = cost_of(call, n_rows=1)
    three_rows = cost_of(call, n_rows=3)
    spec = TOOL_SPECS[("slides", "query")]
    assert three_rows - one_row == 2 * spec.row_weight
    assert spec.row_weight > 0  # the one tool that actually exercises this term


def test_cost_of_never_imports_toolcall() -> None:
    """Regression guard for the duck-typing resolution itself: kit.mcp.specs
    must not IMPORT kit.mcp.types, so it keeps working even if that file is
    mid-edit or briefly broken (hard rule 2's graceful degradation, applied
    in the other direction from kit/mcp/__init__.py's try/except). Checked
    via `ast` against actual import statements, not a source substring —
    specs.py's own module docstring *names* "kit.mcp.types.ToolCall" in
    prose while explaining this exact resolution, which a naive substring
    check would (and, in an earlier draft of this test, did) misfire on."""
    import ast

    import kit.mcp.specs as specs_module

    source = Path(specs_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=specs_module.__file__)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    offenders = {m for m in imported_modules if m == "kit.mcp.types" or m.startswith("kit.mcp.types.")}
    assert not offenders, f"kit/mcp/specs.py must not import kit.mcp.types, found: {offenders}"


def test_unknown_field_in_mask_raises_with_a_useful_message() -> None:
    with pytest.raises(KeyError, match="unknown field 'bogus'"):
        cost("slides", "get_frame", fields=("bogus",))
