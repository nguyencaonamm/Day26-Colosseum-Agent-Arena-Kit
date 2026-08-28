"""tests/test_world_loader.py — kit/world/loader.py + kit/world/fixture.py.

pytest only (permitted in tests/ per the workspace's hard rules). No
network, no unseeded randomness. Every world used here is built fresh, in a
pytest `tmp_path`, by `kit.world.fixture.build_fixture_world` — no
dependency on the real corpus.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make the repo root importable when pytest is invoked from anywhere.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from kit.world.fixture import (
    FIXTURE_ANCHORS,
    FIXTURE_ASKS,
    FIXTURE_INJECTION_NOTES,
    FIXTURE_PATH_IDS,
    FIXTURE_PRIVATE_NOTES,
    build_fixture_world,
)
from kit.world.loader import ASK_IDENTITY_FIELDS, AskKeyError, World, ask_key
from kit.world.page import compute_etag


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def world_dir(tmp_path_factory) -> Path:
    dest = tmp_path_factory.mktemp("world-with-truth")
    return build_fixture_world(dest)


@pytest.fixture(scope="module")
def world(world_dir: Path) -> World:
    return World.load(world_dir)


@pytest.fixture(scope="module")
def student_world_dir(tmp_path_factory) -> Path:
    dest = tmp_path_factory.mktemp("world-no-truth")
    return build_fixture_world(dest, include_truth=False)


@pytest.fixture(scope="module")
def student_world(student_world_dir: Path) -> World:
    return World.load(student_world_dir)


# ---------------------------------------------------------------------------
# build_fixture_world() — artifact shape and CONTRACTS.md section 2 invariants
# ---------------------------------------------------------------------------


def test_all_seven_artifact_files_written(world_dir: Path) -> None:
    for name in (
        "manifest.json", "pages.jsonl", "terms.json", "links.json",
        "drift.json", "truth.json", "denylist_report.json",
    ):
        assert (world_dir / name).is_file(), f"missing {name}"


def test_student_kit_shape_omits_truth_json(student_world_dir: Path) -> None:
    # CONTRACTS.md section 2, invariant 4: truth.json is absent from the
    # student kit's world/. Every other artifact file is still present.
    assert not (student_world_dir / "truth.json").exists()
    for name in ("manifest.json", "pages.jsonl", "terms.json", "links.json", "drift.json"):
        assert (student_world_dir / name).is_file()


def test_page_counts_match_the_documented_fixture_shape(world_dir: Path) -> None:
    rows = [json.loads(line) for line in (world_dir / "pages.jsonl").read_text("utf-8").splitlines()]
    by_ns: dict[str, int] = {}
    for row in rows:
        by_ns[row["ns"]] = by_ns.get(row["ns"], 0) + 1
    assert by_ns["Frame"] == 41  # 8+5 alpha, 7+7 beta, 8+6 gamma
    assert by_ns["Deck"] == 6
    assert by_ns.get("Concept", 0) + by_ns.get("Glossary", 0) == 15
    assert by_ns["Source"] == 5
    assert by_ns["Talk"] == 2
    assert by_ns["Claim"] == 2
    assert by_ns["Note"] == 6
    assert by_ns["Learner"] == 3
    assert len(rows) == 80


def test_invariant_1_anchor_uniqueness(world_dir: Path) -> None:
    rows = [json.loads(line) for line in (world_dir / "pages.jsonl").read_text("utf-8").splitlines()]
    anchors = [r["anchor"] for r in rows]
    assert len(anchors) == len(set(anchors))


def test_invariant_2_terms_and_links_resolve_to_real_pages(world_dir: Path) -> None:
    rows = [json.loads(line) for line in (world_dir / "pages.jsonl").read_text("utf-8").splitlines()]
    known = {r["anchor"] for r in rows}
    terms = json.loads((world_dir / "terms.json").read_text("utf-8"))
    for term, anchors in terms.items():
        for a in anchors:
            assert a in known, f"terms.json[{term!r}] -> {a!r} does not resolve"
    links = json.loads((world_dir / "links.json").read_text("utf-8"))
    for src, targets in links.items():
        for t in targets:
            assert t in known, f"links.json[{src!r}] -> {t!r} does not resolve"
    # And every page's own forward `links` field resolves too.
    for r in rows:
        for t in r.get("links", []):
            assert t in known, f"page {r['anchor']} links to {t!r}, which does not resolve"


def test_invariant_3_etag_is_pure_function_of_body(world_dir: Path) -> None:
    rows = [json.loads(line) for line in (world_dir / "pages.jsonl").read_text("utf-8").splitlines()]
    for r in rows:
        assert compute_etag(r["body"]) == r["etag"], f"etag mismatch for {r['anchor']}"


def test_invariant_4_truth_absent_from_student_kit(student_world_dir: Path) -> None:
    assert not (student_world_dir / "truth.json").exists()


def test_pages_jsonl_sorted_by_anchor(world_dir: Path) -> None:
    lines = (world_dir / "pages.jsonl").read_text("utf-8").splitlines()
    anchors = [json.loads(line)["anchor"] for line in lines]
    assert anchors == sorted(anchors)


def test_build_is_byte_for_byte_deterministic(tmp_path: Path) -> None:
    d1 = build_fixture_world(tmp_path / "a")
    d2 = build_fixture_world(tmp_path / "b")
    assert (d1 / "pages.jsonl").read_bytes() == (d2 / "pages.jsonl").read_bytes()
    assert (d1 / "manifest.json").read_bytes() == (d2 / "manifest.json").read_bytes()
    assert (d1 / "terms.json").read_bytes() == (d2 / "terms.json").read_bytes()
    assert (d1 / "links.json").read_bytes() == (d2 / "links.json").read_bytes()
    assert (d1 / "drift.json").read_bytes() == (d2 / "drift.json").read_bytes()
    assert (d1 / "truth.json").read_bytes() == (d2 / "truth.json").read_bytes()


def test_vietnamese_prose_present_with_english_code_spans(world_dir: Path) -> None:
    rows = [json.loads(line) for line in (world_dir / "pages.jsonl").read_text("utf-8").splitlines()]
    bodies = "\n".join(r["body"] for r in rows)
    # Vietnamese diacritics genuinely present (not just ASCII placeholders).
    assert any(ch in bodies for ch in "ăâđêôơư")
    assert any(ch in bodies for ch in "ĂÂĐÊÔƠƯ") or "MCP" in bodies
    # English code spans embedded in the (mostly Vietnamese) prose.
    assert "slides.query" in bodies or "get_frame" in bodies
    assert "lease_id" in bodies


# ---------------------------------------------------------------------------
# World.load() / World.manifest / World.has_truth
# ---------------------------------------------------------------------------


def test_load_raises_on_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        World.load(tmp_path / "does-not-exist")


def test_manifest_shape(world: World) -> None:
    m = world.manifest
    assert m["world_id"] == "fixture-v1"
    assert m["slice"] == "main"
    assert m["counts"]["total"] == 80


def test_manifest_returns_a_copy(world: World) -> None:
    m1 = world.manifest
    m1["world_id"] = "tampered"
    m2 = world.manifest
    assert m2["world_id"] == "fixture-v1"  # mutation of the returned dict must not stick


def test_has_truth_true_for_default_fixture(world: World) -> None:
    assert world.has_truth is True


def test_has_truth_false_for_student_kit_fixture(student_world: World) -> None:
    assert student_world.has_truth is False


# ---------------------------------------------------------------------------
# .page()
# ---------------------------------------------------------------------------


def test_page_resolves_known_frame(world: World) -> None:
    anchor = FIXTURE_ANCHORS["alpha_frame_w_001"]
    page = world.page(anchor)
    assert page is not None
    assert page.anchor == anchor
    assert page.ns == "Frame"
    assert page.rev == "w"


def test_page_unknown_anchor_returns_none_not_raise(world: World) -> None:
    assert world.page("Frame:00000000/w/999") is None


def test_page_accepts_anchor_object_or_string(world: World) -> None:
    from kit.world.anchor import Anchor

    anchor_str = FIXTURE_ANCHORS["beta_frame_w_001"]
    parsed = Anchor.parse(anchor_str)
    by_str = world.page(anchor_str)
    by_obj = world.page(parsed)
    assert by_str is not None and by_obj is not None
    assert by_str.anchor == by_obj.anchor == anchor_str


def test_page_caches_the_same_object(world: World) -> None:
    anchor = FIXTURE_ANCHORS["gamma_deck_w"]
    p1 = world.page(anchor)
    p2 = world.page(anchor)
    assert p1 is p2


def test_page_never_loads_more_than_the_requested_anchor(world_dir: Path) -> None:
    # A fresh World must not eagerly materialise every Page at load() time —
    # only the offset index. This is the "lazy" half of the lazy/indexed
    # requirement, checked at the object level rather than by timing.
    fresh = World.load(world_dir)
    assert fresh._page_cache == {}  # noqa: SLF001 - internal, intentionally checked
    fresh.page(FIXTURE_ANCHORS["alpha_frame_w_001"])
    assert len(fresh._page_cache) == 1  # noqa: SLF001


# ---------------------------------------------------------------------------
# .search()
# ---------------------------------------------------------------------------


def test_search_finds_a_known_vietnamese_phrase(world: World) -> None:
    hits = world.search("bắt tay")
    assert hits
    assert all("bắt tay" in f"{h.title}\n{h.body}".lower() for h in hits)


def test_search_is_case_insensitive(world: World) -> None:
    hits_lower = world.search("streamable http")
    hits_upper = world.search("STREAMABLE HTTP")
    assert {h.anchor for h in hits_lower} == {h.anchor for h in hits_upper}
    assert hits_lower


def test_search_ns_filter(world: World) -> None:
    hits = world.search("MCP", ns="Concept")
    assert hits
    assert all(h.ns == "Concept" for h in hits)


def test_search_respects_limit(world: World) -> None:
    hits = world.search("MCP", limit=2)
    assert len(hits) <= 2


def test_search_empty_query_returns_empty_list(world: World) -> None:
    assert world.search("") == []
    assert world.search("   ") == []


def test_search_no_match_returns_empty_list(world: World) -> None:
    assert world.search("khong-ton-tai-trong-fixture-nay-dau") == []


def test_search_is_deterministic_across_repeated_calls(world: World) -> None:
    a = [p.anchor for p in world.search("gateway")]
    b = [p.anchor for p in world.search("gateway")]
    assert a == b == sorted(a)


# ---------------------------------------------------------------------------
# .terms() — the ambiguous "endpoint" term and hard-mode mechanic #7
# ---------------------------------------------------------------------------


def test_terms_ambiguous_endpoint_has_two_senses(world: World) -> None:
    all_senses = world.terms("endpoint")
    assert len(all_senses) == 2
    assert {str(a) for a in all_senses} == {
        FIXTURE_ANCHORS["ambiguous_sense_vi"],
        FIXTURE_ANCHORS["ambiguous_sense_en"],
    }


def test_terms_lang_filter_honest_single_match(world: World) -> None:
    vi = world.terms("endpoint", lang="vi")
    en = world.terms("endpoint", lang="en")
    assert [str(a) for a in vi] == [FIXTURE_ANCHORS["ambiguous_sense_vi"]]
    assert [str(a) for a in en] == [FIXTURE_ANCHORS["ambiguous_sense_en"]]


def test_terms_lang_filter_no_match_is_empty_not_a_guess(world: World) -> None:
    # A hypothetical third language has no entry — the loader must not
    # silently substitute one; that substitution is a server-layer decision
    # (module docstring), never automatic here.
    assert world.terms("endpoint", lang="fr") == []


def test_terms_unambiguous_term_returns_one_anchor(world: World) -> None:
    hits = world.terms("field mask")
    assert len(hits) == 1
    assert str(hits[0]) == "Concept:field-mask"


def test_terms_unknown_term_returns_empty_list(world: World) -> None:
    assert world.terms("thuat-ngu-khong-ton-tai") == []


def test_terms_case_and_whitespace_normalised(world: World) -> None:
    a = world.terms("Endpoint")
    b = world.terms("  endpoint  ")
    assert {str(x) for x in a} == {str(x) for x in b} == {
        FIXTURE_ANCHORS["ambiguous_sense_vi"], FIXTURE_ANCHORS["ambiguous_sense_en"],
    }


def test_terms_raw_view_lets_a_server_reproduce_the_wrong_lang_mechanic(world: World) -> None:
    # FINAL-PLAN.md section 4.2 mechanic #7: wrong/missing `lang` should be
    # *able* to silently return the other language's entry. Demonstrate
    # that the raw (lang=None) view alone is enough for a tool server to
    # build that behaviour, without the loader doing it automatically: a
    # server that ignores `lang` and just takes the first raw result picks
    # the "vi" sense (terms.json stores anchors sorted), while a caller who
    # actually asked for "en" gets a *different*, genuinely wrong anchor —
    # that mismatch is the bug the mechanic exists to test for, not a
    # tautology of "equal or not equal".
    raw = world.terms("endpoint")
    naive_pick = raw[0]  # a server that ignores `lang` entirely
    assert str(naive_pick) == FIXTURE_ANCHORS["ambiguous_sense_vi"]
    honest_en = world.terms("endpoint", lang="en")
    assert str(honest_en[0]) == FIXTURE_ANCHORS["ambiguous_sense_en"]
    assert str(naive_pick) != str(honest_en[0]), (
        "a caller asking for lang='en' but served the naive index-0 pick "
        "would silently get the vi-language entry back — exactly mechanic #7"
    )


# ---------------------------------------------------------------------------
# .links() — whatlinkshere
# ---------------------------------------------------------------------------


def test_links_whatlinkshere_nonempty_for_a_cited_concept(world: World) -> None:
    backlinks = world.links("Concept:streamable-http")
    assert backlinks
    assert all(str(a).startswith(("Frame:", "Claim:", "Talk:")) for a in backlinks)


def test_links_unlinked_anchor_returns_empty_list(world: World) -> None:
    assert world.links("Concept:rate-limit-window-nobody-links-to-this-exact-slug") == []


def test_links_matches_whatlinkshere_truth_entry(world: World) -> None:
    from_links = {str(a) for a in world.links("Concept:streamable-http")}
    truth_answer = world.truth(FIXTURE_ASKS["whatlinkshere"])
    assert truth_answer is not None
    assert set(truth_answer["anchors"]) == from_links


# ---------------------------------------------------------------------------
# .drifts() / .drift_info()
# ---------------------------------------------------------------------------


def test_drifts_true_for_alpha_and_gamma(world: World) -> None:
    assert world.drifts(FIXTURE_PATH_IDS["alpha"]) is True
    assert world.drifts(FIXTURE_PATH_IDS["gamma"]) is True


def test_drifts_false_for_byte_identical_beta(world: World) -> None:
    assert world.drifts(FIXTURE_PATH_IDS["beta"]) is False


def test_drifts_unknown_path_id_is_false(world: World) -> None:
    assert world.drifts("00000000") is False


def test_drift_info_full_record(world: World) -> None:
    info = world.drift_info(FIXTURE_PATH_IDS["alpha"])
    assert info == {"w_frames": 8, "c_frames": 5, "drifts": True, "delta": 3}


def test_drift_info_unknown_path_id_is_none(world: World) -> None:
    assert world.drift_info("00000000") is None


# ---------------------------------------------------------------------------
# .truth() / ask_key() — all 8 CONTRACTS.md section 7 ask types
# ---------------------------------------------------------------------------


def test_ask_identity_fields_covers_all_8_types() -> None:
    assert set(ASK_IDENTITY_FIELDS) == {
        "which_day_covers", "source_of", "citation_for", "current_version_of",
        "contradiction_between", "define_term", "whatlinkshere", "record_mastery",
    }


@pytest.mark.parametrize("ask_type", sorted(FIXTURE_ASKS))
def test_truth_resolves_every_ask_type(world: World, ask_type: str) -> None:
    answer = world.truth(FIXTURE_ASKS[ask_type])
    assert answer is not None, f"no truth.json entry for {ask_type}"
    assert isinstance(answer, dict)


def test_truth_returns_none_when_has_truth_is_false(student_world: World) -> None:
    for ask in FIXTURE_ASKS.values():
        assert student_world.truth(ask) is None


def test_truth_returns_none_for_unresolved_ask(world: World) -> None:
    unresolved = {"type": "which_day_covers", "concept": "Concept:does-not-exist-at-all"}
    assert world.truth(unresolved) is None


def test_truth_returns_a_copy(world: World) -> None:
    a1 = world.truth(FIXTURE_ASKS["source_of"])
    a1["anchor"] = "tampered"
    a2 = world.truth(FIXTURE_ASKS["source_of"])
    assert a2["anchor"] != "tampered"


def test_truth_returns_a_deep_copy_nested_lists_included(world: World) -> None:
    # whatlinkshere's answer nests a mutable list one level down — a
    # shallow `dict(answer)` would still alias it, letting a caller mutating
    # the returned list corrupt World's own truth index for every later
    # call. This is a scoring-input safety property, not cosmetic.
    a1 = world.truth(FIXTURE_ASKS["whatlinkshere"])
    original_len = len(a1["anchors"])
    a1["anchors"].append("Frame:tampered/w/001")
    a2 = world.truth(FIXTURE_ASKS["whatlinkshere"])
    assert len(a2["anchors"]) == original_len
    assert "Frame:tampered/w/001" not in a2["anchors"]


def test_ask_key_ignores_require_field() -> None:
    ask1 = {"type": "which_day_covers", "concept": "Concept:streamable-http", "require": ["a"]}
    ask2 = {"type": "which_day_covers", "concept": "Concept:streamable-http", "require": ["b", "c"]}
    assert ask_key(ask1) == ask_key(ask2)


def test_ask_key_distinguishes_different_identities() -> None:
    ask1 = {"type": "source_of", "anchor": "Frame:aaaaaaaa/w/001"}
    ask2 = {"type": "source_of", "anchor": "Frame:bbbbbbbb/w/001"}
    assert ask_key(ask1) != ask_key(ask2)


def test_ask_key_accepts_anchor_instances() -> None:
    from kit.world.anchor import Anchor

    as_str = {"type": "source_of", "anchor": "Frame:aaaaaaaa/w/001"}
    as_anchor = {"type": "source_of", "anchor": Anchor.parse("Frame:aaaaaaaa/w/001")}
    assert ask_key(as_str) == ask_key(as_anchor)


def test_ask_key_rejects_unknown_type() -> None:
    with pytest.raises(AskKeyError):
        ask_key({"type": "not_a_real_ask_type"})


def test_ask_key_rejects_non_mapping() -> None:
    with pytest.raises(AskKeyError):
        ask_key("not-a-dict")  # type: ignore[arg-type]


def test_ask_key_is_deterministic(world: World) -> None:
    for ask in FIXTURE_ASKS.values():
        assert ask_key(ask) == ask_key(dict(ask))  # rebuilding the dict must not change the key


# ---------------------------------------------------------------------------
# The injection + privacy seed material other modules (guardrails.py,
# prosecute.py's privacy_leak detector) will read against.
# ---------------------------------------------------------------------------


def test_injection_notes_are_flagged_and_resolve(world: World) -> None:
    assert len(FIXTURE_INJECTION_NOTES) == 2
    for anchor in FIXTURE_INJECTION_NOTES:
        page = world.page(anchor)
        assert page is not None
        assert page.ns == "Note"
        assert page.meta.get("injection_payload") is True


def test_injection_notes_cover_both_languages(world: World) -> None:
    langs = {world.page(a).lang for a in FIXTURE_INJECTION_NOTES}
    assert langs == {"vi", "en"}


def test_private_notes_are_flagged_and_long_enough_to_test_privacy_leak(world: World) -> None:
    assert len(FIXTURE_PRIVATE_NOTES) == 2
    for anchor in FIXTURE_PRIVATE_NOTES:
        page = world.page(anchor)
        assert page is not None
        assert page.meta.get("private") is True
        # CONTRACTS.md section 6.4's privacy_leak detector normalises and
        # requires >= 40 chars verbatim; the seed body must clear that bar.
        assert len(page.body) >= 40


def test_learner_records_have_private_fields_not_in_body(world: World) -> None:
    for anchor in ("Learner:sv-0417", "Learner:sv-0392", "Learner:sv-0284"):
        page = world.page(anchor)
        assert page is not None
        assert page.ns == "Learner"
        private_fields = page.meta.get("private_fields")
        assert private_fields, f"{anchor} has no private_fields"
        for value in private_fields.values():
            assert value not in page.body, f"{anchor} leaks {value!r} into its own public body"


def test_cross_learner_target_matches_final_plan_worked_example(world: World) -> None:
    # FINAL-PLAN.md section 5.2's own card example mutates a2a target to
    # "learner:sv-0392" — the fixture deliberately reuses that id so
    # identity-class attack tests have a real Learner: record to target.
    page = world.page(FIXTURE_ANCHORS["cross_learner_target"])
    assert page is not None
    assert page.anchor == "Learner:sv-0392"


def test_contradiction_pair_is_reachable_from_the_talk_thread(world: World) -> None:
    answer = world.truth(FIXTURE_ASKS["contradiction_between"])
    assert answer is not None
    claim_a = world.page(answer["a"])
    claim_b = world.page(answer["b"])
    talk = world.page(answer["talk"])
    assert claim_a is not None and claim_b is not None and talk is not None
    assert claim_a.ns == claim_b.ns == "Claim"
    assert talk.ns == "Talk"
    assert claim_a.meta.get("stance") != claim_b.meta.get("stance")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
