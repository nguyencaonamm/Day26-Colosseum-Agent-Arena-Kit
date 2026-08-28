"""tests/test_servers.py — kit/mcp/servers.py: the seven MCP servers over
the World loader.

Covers, in order:
  1. `known_tools()` — exactly the 19 tools this task originally assigned,
     plus (12b) the 5 ENGINE-REPORT.md D-5 fix rows added later.
  2. Field-mask validation (`bad_request`, cost == spec.base only).
  3. Every closed error code each tool can produce, in isolation.
  4. `slides.search`/`query`: field masks, cursors/pagination, the
     deprecated-shim stamp, and the lease lifecycle (`lease_required` /
     valid window / `lease_expired` / a self-invented lease_id).
  5. `slides.get_frame`'s envelope-vs-row `etag` split (CONTRACTS.md 3.2:
     "etag: provenance only").
  6. `glossary.define`: the MRTR `input_required` round trip on a
     genuinely ambiguous term, the unambiguous fast path, the honest-lang
     fast path, and the wrong-lang silent-substitution trap.
  7. `glossary.list_terms`: the catalog-trap anchor price (bare >= 10),
     cursor pagination.
  8. `research.*` / `labs.*` (the latter against the supplementary
     `build_lab_section_world`, since the shared fixture has no `Lab:`
     pages).
  9. `progress.get_mastery` never leaks `Page.meta["private_fields"]`.
  10. `progress.record_mastery`'s receipt_id reproduces
      `world.truth(...)["receipt_id"]` exactly.
  11. Every write's precondition trio (missing headers / stale etag /
      success), for all four write tools.
  12. `registry.provenance` (the anchor-priced cheapest call, envelope
      etag/replica set) / `list_servers` (the other catalog trap, and
      lists what `known_tools()` actually dispatches, split into disjoint
      MCP-server and A2A-peer row sets) / `get_card` / `pin`.
  12b. ENGINE-REPORT.md D-5's fix: research.cite_source, labs.get_exercise,
      curriculum-analyst.which_days_cover (the faithless-peer surface,
      confirmed confidently WRONG on a drifting concept vs `world.truth()`),
      citation-checker.verify_source (incl. its 2-per-3-round rate limit,
      composed through `kit.mcp.hardmode`), and roster.lookup_learner
      (THE authority check: `caller_act` required, cross-learner refused).
  12c. `health()` / `DEGRADED` — the new workspace "degrade loudly" rule.
  13. Cost parity between this module's own `_cost` and
      `kit.mcp.specs.cost()` for every one of the now-15 shared tools
      (`test_every_priced_tool_is_executable` also asserts no TOOL_SPECS
      row can ever have zero executors again).
  14. Determinism: identical calls -> byte-identical results; rows/anchors
      always sorted.
  15. Composition with `kit.mcp.hardmode.HardMode` (when importable):
      hardmode-minted leases are trusted, hardmode's cross-round rate
      limit is enforced, hardmode's own precondition/etag-cache state
      gates `progress.record_mastery` and `content.flag_stale_slide`, and
      the 9 local-only tools are never wrapped by it at all.

pytest only (permitted in tests/ per the workspace's hard rules). No
network, no unseeded randomness, no wall-clock.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the repo root importable when pytest is invoked from anywhere.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from kit.mcp.servers import (
    _SPEC_TOOL_KEYS,
    DEGRADED,
    build_lab_section_world,
    check_lease,
    handle,
    health,
    known_tools,
    mint_lease,
)
from kit.mcp.types import ToolCall
from kit.world.anchor import path_id as pid_fn
from kit.world.fixture import FIXTURE_ANCHORS, FIXTURE_ASKS, FIXTURE_PATH_IDS, build_fixture_world
from kit.world.loader import World

try:
    from kit.mcp.specs import TOOL_SPECS as _SPEC_TOOL_SPECS
    from kit.mcp.specs import cost as spec_cost

    _HAS_SPECS = True
except ImportError:  # pragma: no cover - collaborator file
    _HAS_SPECS = False

try:
    from kit.mcp.hardmode import HardMode

    _HAS_HARDMODE = True
except ImportError:  # pragma: no cover - collaborator file
    _HAS_HARDMODE = False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> World:
    dest = tmp_path_factory.mktemp("servers-world")
    return World.load(build_fixture_world(dest, include_truth=True))


@pytest.fixture(scope="module")
def labsec_world(tmp_path_factory) -> World:
    dest = tmp_path_factory.mktemp("servers-labsec")
    return World.load(build_lab_section_world(dest))


def _call(server: str, tool: str, **kw) -> ToolCall:
    kw.setdefault("args", {})
    return ToolCall(server=server, tool=tool, **kw)


# ---------------------------------------------------------------------------
# 1. known_tools()
# ---------------------------------------------------------------------------


def test_known_tools_is_exactly_the_24_dispatched_tools() -> None:
    """19 original tools + the 5 ENGINE-REPORT.md D-5 fix rows
    (curriculum-analyst.which_days_cover, citation-checker.verify_source,
    roster.lookup_learner, research.cite_source, labs.get_exercise) — all
    5 used to have no executor at all (bad_request: unknown tool no matter
    what called them); see test_every_priced_tool_is_executable below for
    why this can never silently regress back to 19."""
    expected = {
        ("slides", "query"), ("slides", "search"), ("slides", "get_frame"),
        ("slides", "whatlinkshere"), ("slides", "list_sections"),
        ("glossary", "define"), ("glossary", "list_terms"),
        ("research", "search"), ("research", "get_citation"), ("research", "cite_source"),
        ("labs", "get_readme"), ("labs", "list_tasks"), ("labs", "get_exercise"),
        ("progress", "get_mastery"), ("progress", "record_mastery"),
        ("content", "flag_stale_slide"), ("content", "file_content_bug"),
        ("registry", "provenance"), ("registry", "list_servers"),
        ("registry", "get_card"), ("registry", "pin"),
        ("curriculum-analyst", "which_days_cover"),
        ("citation-checker", "verify_source"),
        ("roster", "lookup_learner"),
    }
    assert len(expected) == 24
    assert set(known_tools()) == expected
    assert known_tools() == tuple(sorted(known_tools()))  # deterministic order


@pytest.mark.skipif(not _HAS_SPECS, reason="kit.mcp.specs not importable")
def test_every_priced_tool_is_executable() -> None:
    """D-5/D-6: every kit.mcp.specs.TOOL_SPECS-priced tool must have a real
    executor in this module's dispatch table -- a priced-but-unexecuted
    tool is exactly how the whole A2A layer went dead (ENGINE-REPORT.md
    D-5). This is deliberately a ONE-DIRECTION subset check, not full set
    equality: kit/mcp/specs.py is a collaborator's file outside this task's
    assigned files, and 9 of this module's OWN tools (slides.list_sections,
    research.search, research.get_citation, labs.get_readme,
    labs.list_tasks, progress.get_mastery, content.file_content_bug,
    registry.get_card, registry.pin) are DELIBERATELY local-only extensions
    TOOL_SPECS never prices (see the module docstring's RESOLVED AMBIGUITY
    1) -- test_hardmode_never_wraps_local_only_tools below already asserts
    that on purpose, so full equality would contradict an existing,
    intentional test rather than catch a real bug. Full TOOL_SPECS ==
    known_tools() equality (D-6's OTHER direction: "every implemented tool
    must be priced") needs a kit/mcp/specs.py retune this task's file scope
    does not include; flagged in the task report rather than attempted here.
    """
    dispatched = frozenset(known_tools())
    missing = _SPEC_TOOL_KEYS - dispatched
    assert not missing, f"TOOL_SPECS prices tools with no executor: {sorted(missing)}"


# ---------------------------------------------------------------------------
# 2. Field-mask validation and unknown tool
# ---------------------------------------------------------------------------


def test_unknown_tool_is_bad_request_uncharged(world: World) -> None:
    r = handle(world, _call("slides", "teleport"))
    assert r["ok"] is False
    assert r["error"]["code"] == "bad_request"
    assert r["cost"] == 0


def test_unknown_field_in_mask_is_bad_request_charged_base_only(world: World) -> None:
    r = handle(world, _call("registry", "provenance", args={"anchor": "x"}, fields=("bogus",)))
    assert r["ok"] is False
    assert r["error"]["code"] == "bad_request"
    assert r["cost"] == 1  # registry.provenance's base, per CONTRACTS.md 3.4 anchor price


def test_star_and_default_masks_never_flagged_as_unknown_field(world: World) -> None:
    r_star = handle(world, _call("slides", "whatlinkshere", args={"anchor": "Concept:streamable-http"}, fields=("*",)))
    r_default = handle(world, _call("slides", "whatlinkshere", args={"anchor": "Concept:streamable-http"}))
    assert r_star["ok"] and r_default["ok"]


# ---------------------------------------------------------------------------
# 3-4. slides.search / slides.query
# ---------------------------------------------------------------------------


def test_slides_search_is_stamped_deprecated_with_the_right_successor(world: World) -> None:
    r = handle(world, _call("slides", "search", args={"q": "field mask"}))
    assert r["ok"] is True
    assert r["deprecated"] is True
    assert r["successor"] == "slides.query"
    assert r["lease_id"] is not None


def test_slides_query_is_not_deprecated(world: World) -> None:
    r = handle(world, _call("slides", "query", args={"q": "field mask"}))
    assert r["ok"] is True
    assert r["deprecated"] is False
    assert r["successor"] is None


def test_slides_query_missing_q_is_bad_request(world: World) -> None:
    r = handle(world, _call("slides", "query", args={}))
    assert r["error"]["code"] == "bad_request"


def test_slides_query_pagination_partial_and_continuation(world: World) -> None:
    r1 = handle(world, _call("slides", "query", args={"q": "credit", "limit": 2}))
    assert r1["ok"] is True
    if r1["partial"]:
        assert r1["continuation"] is not None
        r2 = handle(world, _call("slides", "query", args={"q": "credit", "limit": 2, "cursor": r1["continuation"]}))
        assert r2["ok"] is True
        # the second page must not repeat the first page's anchors
        assert set(r1["anchors"]).isdisjoint(r2["anchors"])
    else:
        assert r1["continuation"] is None


def test_slides_query_results_sorted_and_reproducible(world: World) -> None:
    a = handle(world, _call("slides", "query", args={"q": "lease"}))
    b = handle(world, _call("slides", "query", args={"q": "lease"}))
    assert a == b
    assert a["anchors"] == sorted(a["anchors"])


# ---------------------------------------------------------------------------
# 5. slides.get_frame: lease lifecycle + the envelope/row etag split
# ---------------------------------------------------------------------------


def _one_frame_anchor(world: World) -> str:
    # limit high enough that the alphabetically-earlier Claim:/Concept:/Deck:
    # matches (there are several in the fixture) don't crowd every Frame:
    # anchor out of a small page.
    r = handle(world, _call("slides", "query", args={"q": "MCP", "limit": 50}, call_index=0))
    return next(a for a in r["anchors"] if a.startswith("Frame:"))


def test_get_frame_requires_a_live_lease(world: World) -> None:
    anchor = _one_frame_anchor(world)
    r = handle(world, _call("slides", "get_frame", args={"anchor": anchor}, call_index=5))
    assert r["ok"] is False
    assert r["error"] == {"code": "lease_required"}
    assert r["cost"] == 4  # slides.get_frame's default-fields anchor price


def test_get_frame_lease_boundary_exact(world: World) -> None:
    """mint at call_index=K: age 0 -> lease_required, ages 1-3 -> ok,
    age 4 -> lease_expired (mirrors kit/mcp/hardmode.py's own boundary)."""
    lease = mint_lease(call_index=10)

    same_index = handle(world, _call(
        "slides", "get_frame", args={"anchor": _one_frame_anchor(world)}, lease_id=lease, call_index=10,
    ))
    assert same_index["error"] == {"code": "lease_required"}

    for offset in (1, 2, 3):
        ok = handle(world, _call(
            "slides", "get_frame", args={"anchor": _one_frame_anchor(world)}, lease_id=lease, call_index=10 + offset,
        ))
        assert ok["ok"] is True, f"offset={offset}"

    expired = handle(world, _call(
        "slides", "get_frame", args={"anchor": _one_frame_anchor(world)}, lease_id=lease, call_index=14,
    ))
    assert expired["error"] == {"code": "lease_expired"}


def test_get_frame_self_invented_lease_id_rejected(world: World) -> None:
    r = handle(world, _call(
        "slides", "get_frame", args={"anchor": _one_frame_anchor(world)}, lease_id="i-made-this-up", call_index=1,
    ))
    assert r["error"] == {"code": "lease_required"}


def test_check_lease_helper_matches_the_boundary_directly() -> None:
    assert check_lease(None, 5) == "lease_required"
    assert check_lease("not-a-real-format", 5) == "lease_required"
    assert check_lease(mint_lease(0), 0) == "lease_required"
    assert check_lease(mint_lease(0), 1) == "ok"
    assert check_lease(mint_lease(0), 3) == "ok"
    assert check_lease(mint_lease(0), 4) == "lease_expired"


def test_get_frame_not_found(world: World) -> None:
    lease = mint_lease(0)
    r = handle(world, _call(
        "slides", "get_frame", args={"anchor": "Frame:deadbeef/w/999"}, lease_id=lease, call_index=1,
    ))
    assert r["error"] == {"code": "not_found"}


def test_get_frame_envelope_etag_is_none_row_etag_is_the_page_etag(world: World) -> None:
    anchor = _one_frame_anchor(world)
    lease = mint_lease(0)
    r = handle(world, _call(
        "slides", "get_frame", args={"anchor": anchor}, fields=("*",), lease_id=lease, call_index=1,
    ))
    assert r["ok"] is True
    assert r["etag"] is None  # envelope: provenance-only
    assert r["lease_id"] is None  # get_frame consumes, never mints
    assert r["rows"][0]["etag"] == world.page(anchor).etag


def test_get_frame_replica_matches_the_page(world: World) -> None:
    lease = mint_lease(0)
    r = handle(world, _call(
        "slides", "get_frame", args={"anchor": FIXTURE_ANCHORS["alpha_frame_w_001"]}, lease_id=lease, call_index=1,
    ))
    assert r["ok"] is True and r["replica"] == "w"


# ---------------------------------------------------------------------------
# slides.whatlinkshere / slides.list_sections
# ---------------------------------------------------------------------------


def test_whatlinkshere(world: World) -> None:
    r = handle(world, _call("slides", "whatlinkshere", args={"anchor": "Concept:streamable-http"}))
    assert r["ok"] is True
    assert r["rows"][0]["targets"]
    assert "Concept:streamable-http" in r["anchors"]


def test_whatlinkshere_missing_anchor_is_bad_request(world: World) -> None:
    r = handle(world, _call("slides", "whatlinkshere", args={}))
    assert r["error"]["code"] == "bad_request"


def test_list_sections_against_labsec_world(labsec_world: World) -> None:
    r = handle(labsec_world, _call("slides", "list_sections", args={"q": "Lab"}))
    assert r["ok"] is True
    assert len(r["rows"]) == 2
    assert all(a.startswith("Section:") for a in r["anchors"])


def test_list_sections_via_path_id_derives_q_from_deck_title(labsec_world: World) -> None:
    pid = pid_fn("day26/fixture-labsec-demo.tex")
    r = handle(labsec_world, _call("slides", "list_sections", args={"path_id": pid}))
    assert r["ok"] is True
    assert len(r["rows"]) == 2


def test_list_sections_no_q_no_path_id_is_bad_request(world: World) -> None:
    r = handle(world, _call("slides", "list_sections", args={}))
    assert r["error"]["code"] == "bad_request"


def test_list_sections_empty_on_a_world_with_no_section_pages(world: World) -> None:
    r = handle(world, _call("slides", "list_sections", args={"q": "streamable"}))
    assert r["ok"] is True
    assert r["rows"] == []


# ---------------------------------------------------------------------------
# 6. glossary.define — MRTR, honest lang, the wrong-lang trap
# ---------------------------------------------------------------------------


def test_define_ambiguous_term_triggers_mrtr(world: World) -> None:
    r = handle(world, _call("glossary", "define", args={"term": "endpoint"}))
    assert r["ok"] is False
    assert r["error"]["code"] == "bad_request"
    ir = r["error"]["input_required"]
    assert "question" in ir and len(ir["options"]) == 2
    for opt in ir["options"]:
        assert set(opt) == {"anchor", "sense", "lang"}


def test_define_mrtr_round_trip_completes_with_sense(world: World) -> None:
    first = handle(world, _call("glossary", "define", args={"term": "endpoint"}))
    chosen = first["error"]["input_required"]["options"][0]["anchor"]
    second = handle(world, _call("glossary", "define", args={"term": "endpoint", "sense": chosen}))
    assert second["ok"] is True
    assert second["anchors"] == [chosen]


def test_define_unknown_sense_is_bad_request(world: World) -> None:
    r = handle(world, _call("glossary", "define", args={"term": "endpoint", "sense": "Glossary:not-a-real-sense"}))
    assert r["error"]["code"] == "bad_request"


def test_define_unambiguous_term_resolves_directly(world: World) -> None:
    r = handle(world, _call("glossary", "define", args={"term": "streamable http"}))
    assert r["ok"] is True
    assert r["anchors"] == ["Concept:streamable-http"]


def test_define_honest_lang_resolves_the_matching_sense(world: World) -> None:
    vi = handle(world, _call("glossary", "define", args={"term": "endpoint", "lang": "vi"}))
    en = handle(world, _call("glossary", "define", args={"term": "endpoint", "lang": "en"}))
    assert vi["ok"] and vi["anchors"] == [FIXTURE_ANCHORS["ambiguous_sense_vi"]]
    assert en["ok"] and en["anchors"] == [FIXTURE_ANCHORS["ambiguous_sense_en"]]


def test_define_wrong_lang_silently_substitutes_never_errors(world: World) -> None:
    """FINAL-PLAN.md 4.2 mechanic 7: a wrong lang never raises input_required
    (that would defeat the trap) — it silently returns SOME sense."""
    r = handle(world, _call("glossary", "define", args={"term": "endpoint", "lang": "fr"}))
    assert r["ok"] is True
    assert r["anchors"] == [FIXTURE_ANCHORS["ambiguous_sense_vi"]]  # sorted(all_senses)[0]


def test_define_term_not_found(world: World) -> None:
    r = handle(world, _call("glossary", "define", args={"term": "this-term-does-not-exist"}))
    assert r["error"]["code"] == "not_found"


def test_define_missing_term_is_bad_request(world: World) -> None:
    r = handle(world, _call("glossary", "define", args={}))
    assert r["error"]["code"] == "bad_request"


# ---------------------------------------------------------------------------
# 7. glossary.list_terms — the catalog trap
# ---------------------------------------------------------------------------


def test_list_terms_bare_call_is_the_expensive_anchor_price(world: World) -> None:
    r = handle(world, _call("glossary", "list_terms"))
    assert r["ok"] is True
    assert r["cost"] == 10  # CONTRACTS.md 3.4 anchor price, "the catalog trap"


def test_list_terms_masked_is_cheaper(world: World) -> None:
    r = handle(world, _call("glossary", "list_terms", fields=("term",)))
    assert r["ok"] is True
    assert r["cost"] < 10
    assert all(set(row) == {"term"} for row in r["rows"])


def test_list_terms_pagination(world: World) -> None:
    page1 = handle(world, _call("glossary", "list_terms", args={"limit": 3}))
    assert page1["ok"] is True
    if page1["partial"]:
        page2 = handle(world, _call("glossary", "list_terms", args={"limit": 3, "cursor": page1["continuation"]}))
        assert page2["ok"] is True
        terms1 = {row["term"] for row in page1["rows"]}
        terms2 = {row["term"] for row in page2["rows"]}
        assert terms1.isdisjoint(terms2)


def test_list_terms_finds_the_ambiguous_endpoint_term(world: World) -> None:
    r = handle(world, _call("glossary", "list_terms", args={"limit": 1000}, fields=("term", "sense")))
    rows_by_term = {row["term"]: row for row in r["rows"]}
    assert "endpoint" in rows_by_term
    assert sorted(rows_by_term["endpoint"]["sense"]) == ["endpoint-mcp", "endpoint-network"]


# ---------------------------------------------------------------------------
# 8. research.* / labs.*
# ---------------------------------------------------------------------------


def test_research_search(world: World) -> None:
    r = handle(world, _call("research", "search", args={"q": "MCP"}))
    assert r["ok"] is True and r["rows"]


def test_research_search_missing_q_is_bad_request(world: World) -> None:
    r = handle(world, _call("research", "search", args={}))
    assert r["error"]["code"] == "bad_request"


def test_research_get_citation_by_anchor(world: World) -> None:
    r = handle(world, _call("research", "get_citation", args={"anchor": "Source:mcp-spec-2026-07-28"}))
    assert r["ok"] is True
    assert r["rows"][0]["url"] == "https://fixture.example/mcp-spec-2026-07-28"


def test_research_get_citation_by_url(world: World) -> None:
    r = handle(world, _call("research", "get_citation", args={"url": "aaif-charter"}))
    assert r["ok"] is True


def test_research_get_citation_not_found(world: World) -> None:
    r = handle(world, _call("research", "get_citation", args={"anchor": "Source:does-not-exist"}))
    assert r["error"]["code"] == "not_found"


def test_labs_get_readme_against_labsec_world(labsec_world: World) -> None:
    readme_pid = pid_fn("day26/fixture-labsec-demo.tex")
    r = handle(labsec_world, _call("labs", "get_readme", args={"anchor": f"Lab:{readme_pid}/w"}))
    assert r["ok"] is True


def test_labs_get_readme_not_found_on_shared_fixture(world: World) -> None:
    """The shared kit/world/fixture.py world has NO Lab: pages at all."""
    r = handle(world, _call("labs", "get_readme", args={"anchor": "Lab:deadbeef/w"}))
    assert r["error"]["code"] == "not_found"


def test_labs_list_tasks_against_labsec_world(labsec_world: World) -> None:
    r = handle(labsec_world, _call("labs", "list_tasks", args={"q": "gateway"}))
    assert r["ok"] is True and len(r["rows"]) == 1


# ---------------------------------------------------------------------------
# 9-10. progress.get_mastery / progress.record_mastery
# ---------------------------------------------------------------------------


def test_get_mastery_never_leaks_private_fields(world: World) -> None:
    r = handle(world, _call("progress", "get_mastery", args={"learner": "Learner:sv-0284"}, fields=("*",)))
    assert r["ok"] is True
    row = r["rows"][0]
    assert set(row) <= {"learner", "concept", "summary"}
    private = world.page("Learner:sv-0284").meta["private_fields"]
    dumped = str(row)
    for value in private.values():
        assert str(value) not in dumped


def test_get_mastery_not_found(world: World) -> None:
    r = handle(world, _call("progress", "get_mastery", args={"learner": "Learner:not-real"}))
    assert r["error"]["code"] == "not_found"


def test_record_mastery_receipt_matches_truth_json(world: World) -> None:
    learner_page = world.page("Learner:sv-0417")
    r = handle(world, _call(
        "progress", "record_mastery",
        args={"learner": "Learner:sv-0417", "concept": "Concept:streamable-http"},
        headers={"If-Match": learner_page.etag, "Idempotency-Key": "idem-test-1"},
    ))
    assert r["ok"] is True
    truth = world.truth(FIXTURE_ASKS["record_mastery"])
    assert r["rows"][0]["receipt_id"] == truth["receipt_id"]


def test_record_mastery_accepts_learner_or_anchor_alias(world: World) -> None:
    learner_page = world.page("Learner:sv-0417")
    via_learner = handle(world, _call(
        "progress", "record_mastery",
        args={"learner": "Learner:sv-0417", "concept": "Concept:streamable-http"},
        headers={"if-match": learner_page.etag, "idempotency-key": "idem-alias-1"},
    ))
    via_anchor = handle(world, _call(
        "progress", "record_mastery",
        args={"anchor": "Learner:sv-0417", "concept": "Concept:streamable-http"},
        headers={"if-match": learner_page.etag, "idempotency-key": "idem-alias-2"},
    ))
    assert via_learner["ok"] and via_anchor["ok"]
    assert via_learner["rows"][0]["receipt_id"] == via_anchor["rows"][0]["receipt_id"]


# ---------------------------------------------------------------------------
# 11. write preconditions, uniform across the four write tools
# ---------------------------------------------------------------------------

_WRITE_CASES = [
    ("progress", "record_mastery", {"learner": "Learner:sv-0417", "concept": "Concept:streamable-http"}, "Learner:sv-0417"),
    ("content", "flag_stale_slide", {"anchor": "Deck:" + FIXTURE_PATH_IDS["beta"] + "/w"}, "Deck:" + FIXTURE_PATH_IDS["beta"] + "/w"),
    ("content", "file_content_bug", {"anchor": "Deck:" + FIXTURE_PATH_IDS["beta"] + "/w", "description": "x"}, "Deck:" + FIXTURE_PATH_IDS["beta"] + "/w"),
    ("registry", "pin", {"anchor": "Deck:" + FIXTURE_PATH_IDS["beta"] + "/w"}, "Deck:" + FIXTURE_PATH_IDS["beta"] + "/w"),
]


@pytest.mark.parametrize("server,tool,args,target_anchor", _WRITE_CASES)
def test_write_no_headers_is_precondition_missing(world: World, server, tool, args, target_anchor) -> None:
    r = handle(world, _call(server, tool, args=args))
    assert r["ok"] is False
    assert r["error"]["code"] == "precondition_missing"


@pytest.mark.parametrize("server,tool,args,target_anchor", _WRITE_CASES)
def test_write_stale_etag_is_conflict(world: World, server, tool, args, target_anchor) -> None:
    r = handle(world, _call(
        server, tool, args=args,
        headers={"if-match": "sha256:0000000000000000", "idempotency-key": "idem-stale"},
    ))
    assert r["ok"] is False
    assert r["error"]["code"] == "conflict"


@pytest.mark.parametrize("server,tool,args,target_anchor", _WRITE_CASES)
def test_write_correct_etag_succeeds(world: World, server, tool, args, target_anchor) -> None:
    etag = world.page(target_anchor).etag
    r = handle(world, _call(
        server, tool, args=args,
        headers={"if-match": etag, "idempotency-key": f"idem-ok-{server}-{tool}"},
    ))
    assert r["ok"] is True
    assert "receipt_id" in r["rows"][0]


# ---------------------------------------------------------------------------
# 12. registry.*
# ---------------------------------------------------------------------------


def test_provenance_is_the_cheapest_call_and_sets_envelope_etag(world: World) -> None:
    anchor = "Deck:" + FIXTURE_PATH_IDS["beta"] + "/w"
    r = handle(world, _call("registry", "provenance", args={"anchor": anchor}))
    assert r["ok"] is True
    assert r["cost"] == 1
    assert r["etag"] == world.page(anchor).etag
    assert r["replica"] == "w"


def test_provenance_not_found(world: World) -> None:
    r = handle(world, _call("registry", "provenance", args={"anchor": "Deck:deadbeef/w"}))
    assert r["error"]["code"] == "not_found"


def test_list_servers_full_dump_is_the_anchor_price(world: World) -> None:
    r = handle(world, _call("registry", "list_servers", fields=("*",)))
    assert r["ok"] is True
    assert r["cost"] == 12


def test_list_servers_describes_only_what_is_actually_dispatched(world: World) -> None:
    """RESOLVED AMBIGUITY 1's D-5 fix: _HANDLERS now legitimately contains
    BOTH the 7 MCP servers' tools and the 3 A2A peer tools, so this
    catalog must split them into two DISJOINT row sets whose union is
    exactly known_tools() — never double-counted, never dropped."""
    r = handle(world, _call("registry", "list_servers", fields=("name", "capabilities")))
    assert r["ok"] is True
    by_name = {row["name"]: row for row in r["rows"]}
    mcp_server_names = {"slides", "glossary", "research", "labs", "progress", "content", "registry"}
    a2a_peer_names = {"curriculum-analyst", "citation-checker", "roster"}
    assert set(by_name) == mcp_server_names | a2a_peer_names

    mcp_capabilities = {
        cap for name, row in by_name.items() if name in mcp_server_names for cap in row["capabilities"]
    }
    a2a_capabilities = {
        cap for name, row in by_name.items() if name in a2a_peer_names for cap in row["capabilities"]
    }
    assert mcp_capabilities.isdisjoint(a2a_capabilities)
    assert mcp_capabilities | a2a_capabilities == {f"{s}.{t}" for s, t in known_tools()}

    # research.cite_source / labs.get_exercise are now REAL dispatched MCP
    # tools (ENGINE-REPORT.md D-5's fix) -- advertised under their MCP
    # server, never folded into an A2A peer row.
    assert "research.cite_source" in mcp_capabilities
    assert "labs.get_exercise" in mcp_capabilities

    # the three true A2A peer tools are catalogued under their OWN peer
    # rows, never under an MCP server's capability list.
    assert "curriculum-analyst.which_days_cover" in a2a_capabilities
    assert "citation-checker.verify_source" in a2a_capabilities
    assert "roster.lookup_learner" in a2a_capabilities


def test_get_card_single_tool(world: World) -> None:
    r = handle(world, _call("registry", "get_card", args={"server": "slides", "tool": "get_frame"}, fields=("*",)))
    assert r["ok"] is True
    assert r["rows"][0]["needs_lease"] is True
    assert r["rows"][0]["base"] == 2


def test_get_card_unknown_server_not_found(world: World) -> None:
    r = handle(world, _call("registry", "get_card", args={"server": "not-a-real-server"}))
    assert r["error"]["code"] == "not_found"


def test_get_card_whole_server(world: World) -> None:
    r = handle(world, _call("registry", "get_card", args={"server": "slides"}))
    assert r["ok"] is True
    assert {row["tool"] for row in r["rows"]} == {"query", "search", "get_frame", "whatlinkshere", "list_sections"}


def test_pin_success_and_precondition(world: World) -> None:
    anchor = "Deck:" + FIXTURE_PATH_IDS["gamma"] + "/w"
    etag = world.page(anchor).etag
    r = handle(world, _call(
        "registry", "pin", args={"anchor": anchor}, fields=("*",),
        headers={"if-match": etag, "idempotency-key": "idem-pin-1"},
    ))
    assert r["ok"] is True
    assert r["rows"][0]["pinned_etag"] == etag


# ---------------------------------------------------------------------------
# 12b. A2A peer tools — ENGINE-REPORT.md D-5's fix. These 5 rows used to
# return bad_request: unknown tool no matter what called them; the shared
# `test_cost_parity_with_kit_mcp_specs` parametrized test above proves each
# is now dispatched (its per-tool skip stops firing once known_tools()
# includes them), but that test only checks cost math — the tests below
# check the actual DATA each one returns.
# ---------------------------------------------------------------------------


def test_research_cite_source_by_anchor(world: World) -> None:
    r = handle(world, _call("research", "cite_source", args={"anchor": "Source:mcp-spec-2026-07-28"}))
    assert r["ok"] is True
    assert r["rows"][0]["url"] == "https://fixture.example/mcp-spec-2026-07-28"


def test_research_cite_source_by_url_substring(world: World) -> None:
    r = handle(world, _call("research", "cite_source", args={"url": "mcp-spec-2026-07-28"}, fields=("*",)))
    assert r["ok"] is True
    assert r["anchors"] == ["Source:mcp-spec-2026-07-28"]
    assert r["rows"][0]["confidence"] == 1.0


def test_research_cite_source_fabricated_anchor_not_found(world: World) -> None:
    r = handle(world, _call("research", "cite_source", args={"anchor": "Source:does-not-exist"}))
    assert r["error"]["code"] == "not_found"


def test_research_cite_source_requires_anchor_or_url(world: World) -> None:
    r = handle(world, _call("research", "cite_source", args={}))
    assert r["error"]["code"] == "bad_request"


def test_labs_get_exercise_returns_a_task(labsec_world: World) -> None:
    pid = pid_fn("day26/fixture-labsec-demo.tex")
    r = handle(labsec_world, _call("labs", "get_exercise", args={"anchor": f"Lab:{pid}/w/002"}, fields=("*",)))
    assert r["ok"] is True
    assert r["rows"][0]["summary"] == "Task: Trien khai gateway"
    assert r["rows"][0]["instructions"]
    # not modeled by any fixture world -> documented empty defaults, never KeyError.
    assert r["rows"][0]["kc_refs"] == []
    assert r["rows"][0]["starter_code"] == ""


def test_labs_get_exercise_non_lab_anchor_not_found(world: World) -> None:
    r = handle(world, _call("labs", "get_exercise", args={"anchor": "Concept:streamable-http"}))
    assert r["error"]["code"] == "not_found"


def test_which_days_cover_matches_truth_on_course_day_and_track(world: World) -> None:
    r = handle(world, _call("curriculum-analyst", "which_days_cover", args={"concept": "Concept:streamable-http"}))
    assert r["ok"] is True
    truth = world.truth(FIXTURE_ASKS["which_day_covers"])
    assert r["rows"][0]["course_day"] == truth["course_day"]
    assert r["rows"][0]["track"] == truth["track"]


def test_which_days_cover_is_confidently_wrong_on_anchor(world: World) -> None:
    """THE faithless-peer property (the task brief, verbatim): "broad,
    useful, and DELIBERATELY UNVERIFIED... it can be confidently wrong...
    a real behaviour, not a flag." kit/world/fixture.py's "alpha" deck
    genuinely drifts between replicas for Concept:streamable-http; the
    curated truth prefers the WORKING replica, this coarse peer does not
    check replica freshness at all and picks whichever Frame anchor sorts
    alphabetically first ("c" < "w") -- provably, deterministically wrong
    on `anchor` while still agreeing on course_day/track."""
    r = handle(world, _call("curriculum-analyst", "which_days_cover", args={"concept": "Concept:streamable-http"}))
    truth = world.truth(FIXTURE_ASKS["which_day_covers"])
    assert r["ok"] is True
    assert r["rows"][0]["anchor"] != truth["anchor"]
    assert r["rows"][0]["anchor"].endswith("/c/001")
    assert truth["anchor"].endswith("/w/001")


def test_which_days_cover_unknown_concept_not_found(world: World) -> None:
    r = handle(world, _call("curriculum-analyst", "which_days_cover", args={"concept": "Concept:does-not-exist"}))
    assert r["error"]["code"] == "not_found"


def test_verify_source_matching_anchor_and_url_is_confident(world: World) -> None:
    r = handle(world, _call(
        "citation-checker", "verify_source",
        args={"anchor": "Source:mcp-spec-2026-07-28", "url": "https://fixture.example/mcp-spec-2026-07-28"},
        fields=("*",),
    ))
    assert r["ok"] is True
    assert r["rows"][0]["confidence"] == 1.0


def test_verify_source_url_mismatch_returns_the_real_url_at_zero_confidence(world: World) -> None:
    r = handle(world, _call(
        "citation-checker", "verify_source",
        args={"anchor": "Source:mcp-spec-2026-07-28", "url": "https://not-the-real-url.example"},
        fields=("*",),
    ))
    assert r["ok"] is True
    assert r["rows"][0]["confidence"] == 0.0
    assert r["rows"][0]["url"] == "https://fixture.example/mcp-spec-2026-07-28"


def test_verify_source_fabricated_anchor_not_found(world: World) -> None:
    """CONTRACTS.md 6.4's fabricated_citation detector's own first
    condition: "a cited_anchor that does not resolve in pages.jsonl"."""
    r = handle(world, _call("citation-checker", "verify_source", args={"anchor": "Source:fabricated"}))
    assert r["error"]["code"] == "not_found"


def test_verify_source_matched_span(world: World) -> None:
    r = handle(world, _call(
        "citation-checker", "verify_source",
        args={"anchor": "Source:mcp-spec-2026-07-28", "span": "Streamable HTTP"},
        fields=("*",),
    ))
    assert r["ok"] is True
    assert r["rows"][0]["matched_span"] == "Streamable HTTP"
    unmatched = handle(world, _call(
        "citation-checker", "verify_source",
        args={"anchor": "Source:mcp-spec-2026-07-28", "span": "this text is not in the source"},
        fields=("*",),
    ))
    assert unmatched["rows"][0]["matched_span"] is None


@pytest.mark.skipif(not _HAS_HARDMODE, reason="kit.mcp.hardmode not importable")
def test_verify_source_rate_limited_2_per_3_rounds(world: World) -> None:
    """kit.mcp.specs.TOOL_SPECS[("citation-checker","verify_source")].rate_limit
    == (2, 3), enforced GENERICALLY by HardMode._check_rate_limit for any
    TOOL_SPECS-priced tool -- this only needs dispatching through
    handle(..., hardmode=hm) to fire (ENGINE-REPORT.md D-5's fix)."""
    hm = HardMode(world=world, opaque_enabled=False)
    hm.reset("test-verify-source-rate", world_id=world.manifest["world_id"])
    results = []
    for round_no in (1, 2, 3):
        hm.begin_round(round_no)
        results.append(handle(
            world, _call("citation-checker", "verify_source", args={"anchor": "Source:mcp-spec-2026-07-28"}),
            hardmode=hm,
        ))
    assert results[0]["ok"] is True
    assert results[1]["ok"] is True
    assert results[2]["ok"] is False
    assert results[2]["error"]["code"] == "rate_limited"


def test_lookup_learner_refuses_without_any_caller_act(world: World) -> None:
    r = handle(world, _call("roster", "lookup_learner", args={"learner": "Learner:sv-0417"}))
    assert r["error"]["code"] == "unauthorized"


def test_lookup_learner_self_read_succeeds(world: World) -> None:
    r = handle(
        world, _call("roster", "lookup_learner", args={"learner": "Learner:sv-0417"}, fields=("*",)),
        caller_act="learner:sv-0417",
    )
    assert r["ok"] is True
    assert r["rows"][0]["act"] == "learner:sv-0417"
    assert r["rows"][0]["track"] == "P2T2"


def test_lookup_learner_accepts_the_act_shaped_identity_string_too(world: World) -> None:
    """args.learner may be the wire Anchor ("Learner:sv-0417") or the
    act-shaped identity string ("learner:sv-0417") a DelegationToken.act
    would carry -- both must resolve to the same page."""
    by_anchor = handle(
        world, _call("roster", "lookup_learner", args={"learner": "Learner:sv-0417"}), caller_act="learner:sv-0417",
    )
    by_act = handle(
        world, _call("roster", "lookup_learner", args={"learner": "learner:sv-0417"}), caller_act="learner:sv-0417",
    )
    assert by_anchor["ok"] is True and by_act["ok"] is True
    assert by_anchor["anchors"] == by_act["anchors"] == ["Learner:sv-0417"]


def test_lookup_learner_refuses_a_cross_learner_read(world: World) -> None:
    """THE AUTHORITY CHECK (the task brief, verbatim): resolves against
    ctx.act, refuses a cross-learner read -- even though sv-0392 is a REAL
    learner in this world, an authenticated sv-0417 cannot read it."""
    r = handle(
        world, _call("roster", "lookup_learner", args={"learner": "Learner:sv-0392"}),
        caller_act="learner:sv-0417",
    )
    assert r["error"]["code"] == "unauthorized"


def test_lookup_learner_nonexistent_target_is_not_found_when_authenticated(world: World) -> None:
    r = handle(
        world, _call("roster", "lookup_learner", args={"learner": "Learner:does-not-exist"}),
        caller_act="learner:sv-0417",
    )
    assert r["error"]["code"] == "not_found"


def test_lookup_learner_scopes_include_wiki_read(world: World) -> None:
    r = handle(
        world, _call("roster", "lookup_learner", args={"learner": "Learner:sv-0417"}, fields=("*",)),
        caller_act="learner:sv-0417",
    )
    assert "wiki.read" in r["rows"][0]["scopes"]


# ---------------------------------------------------------------------------
# 12c. health() / DEGRADED — the new workspace "degrade loudly" rule.
# ---------------------------------------------------------------------------


def test_health_reports_not_degraded_in_this_environment() -> None:
    h = health()
    assert h["has_specs"] is _HAS_SPECS
    assert h["has_hardmode"] is _HAS_HARDMODE
    assert h["ok"] == (h["has_specs"] and h["has_hardmode"])
    assert h["degraded"] == DEGRADED
    assert isinstance(DEGRADED, tuple)


# ---------------------------------------------------------------------------
# 13. Cost parity with kit.mcp.specs.cost() for every shared tool
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_SPECS, reason="kit.mcp.specs not importable")
@pytest.mark.parametrize("server,tool", sorted(_SPEC_TOOL_KEYS) if _HAS_SPECS else [])
def test_cost_parity_with_kit_mcp_specs(server: str, tool: str) -> None:
    if (server, tool) not in known_tools():
        pytest.skip(f"{server}.{tool} is a TOOL_SPECS row this module never dispatches")
    from kit.mcp.servers import _cost, _lookup_spec

    spec = _lookup_spec(server, tool)
    for mask in ((), ("*",)):
        assert _cost(spec, mask, 1) == spec_cost(server, tool, fields=mask, n_rows=1)


# ---------------------------------------------------------------------------
# 15. Composition with kit.mcp.hardmode.HardMode
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_HARDMODE, reason="kit.mcp.hardmode not importable")
def test_hardmode_minted_lease_is_trusted_by_get_frame(world: World) -> None:
    hm = HardMode(world=world, opaque_enabled=False)
    hm.reset("test-hm-lease", world_id=world.manifest["world_id"])
    search_r = handle(world, _call("slides", "search", args={"q": "field mask"}, call_index=0), hardmode=hm)
    assert search_r["ok"] and search_r["lease_id"] is not None
    gf = handle(world, _call(
        "slides", "get_frame", args={"anchor": search_r["anchors"][0]},
        lease_id=search_r["lease_id"], call_index=1,
    ), hardmode=hm)
    assert gf["ok"] is True


@pytest.mark.skipif(not _HAS_HARDMODE, reason="kit.mcp.hardmode not importable")
def test_hardmode_enforces_list_servers_rate_limit(world: World) -> None:
    hm = HardMode(world=world, opaque_enabled=False)
    hm.reset("test-hm-rate", world_id=world.manifest["world_id"])
    hm.begin_round(1)
    first = handle(world, _call("registry", "list_servers"), hardmode=hm)
    hm.begin_round(2)
    second = handle(world, _call("registry", "list_servers"), hardmode=hm)
    assert first["ok"] is True
    assert second["ok"] is False and second["error"]["code"] == "rate_limited"


@pytest.mark.skipif(not _HAS_HARDMODE, reason="kit.mcp.hardmode not importable")
def test_hardmode_precondition_state_gates_record_mastery(world: World) -> None:
    hm = HardMode(world=world, opaque_enabled=False)
    hm.reset("test-hm-precond", world_id=world.manifest["world_id"])
    learner_page = world.page("Learner:sv-0417")

    never_read = handle(world, _call(
        "progress", "record_mastery",
        args={"anchor": "Learner:sv-0417", "concept": "Concept:streamable-http"},
        headers={"if-match": learner_page.etag, "idempotency-key": "idem-hm-a"},
    ), hardmode=hm)
    assert never_read["ok"] is False and never_read["error"]["code"] == "conflict"

    handle(world, _call("registry", "provenance", args={"anchor": "Learner:sv-0417"}), hardmode=hm)
    after_read = handle(world, _call(
        "progress", "record_mastery",
        args={"anchor": "Learner:sv-0417", "concept": "Concept:streamable-http"},
        headers={"if-match": learner_page.etag, "idempotency-key": "idem-hm-b"},
    ), hardmode=hm)
    assert after_read["ok"] is True


@pytest.mark.skipif(not _HAS_HARDMODE, reason="kit.mcp.hardmode not importable")
def test_hardmode_never_wraps_local_only_tools(world: World) -> None:
    """registry.pin / content.file_content_bug / progress.get_mastery /
    slides.list_sections / research.* / labs.* are never in
    kit.mcp.specs.TOOL_SPECS, so hardmode.check_before is never consulted
    for them — this module's own precondition checks are the only gate,
    with or without a hardmode instance passed in."""
    hm = HardMode(world=world, opaque_enabled=False)
    hm.reset("test-hm-local-only", world_id=world.manifest["world_id"])
    r = handle(world, _call("registry", "pin", args={"anchor": "Deck:" + FIXTURE_PATH_IDS["beta"] + "/w"}), hardmode=hm)
    assert r["ok"] is False and r["error"]["code"] == "precondition_missing"


@pytest.mark.skipif(not _HAS_HARDMODE, reason="kit.mcp.hardmode not importable")
def test_hardmode_absent_vs_present_agree_on_the_happy_path(world: World) -> None:
    """A structurally simple, unambiguous call should succeed identically
    whether or not hardmode is engaged (module docstring's "degrade
    gracefully" contract)."""
    without = handle(world, _call("registry", "provenance", args={"anchor": "Concept:streamable-http"}))
    hm = HardMode(world=world, opaque_enabled=False)
    hm.reset("test-hm-agree", world_id=world.manifest["world_id"])
    with_hm = handle(world, _call("registry", "provenance", args={"anchor": "Concept:streamable-http"}), hardmode=hm)
    assert without["ok"] is True and with_hm["ok"] is True
    assert without["rows"] == with_hm["rows"]
