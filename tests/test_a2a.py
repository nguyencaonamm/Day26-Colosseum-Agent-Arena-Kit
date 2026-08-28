"""tests/test_a2a.py — the A2A peers + Agent Card admission surface
(kit/mcp/a2a.py): AgentCard, AdmissionResult, DelegationToken, TraceContext,
the five named failure modes (undeclared skill, aud mismatch, replayed
token, forged card signature, act escalation), and `execute()` — the
admission-gated execution bridge that is ENGINE-REPORT.md D-5's fix
("the entire A2A layer is dead"): everything above already implemented
admission; `execute()` is what makes it reachable in play.

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

from kit.mcp.a2a import (
    AGENT_CARD_PATH,
    AGENT_CARDS,
    AGENT_JSON_PATH_DEPRECATED,
    DEGRADED,
    KNOWN_PEERS,
    SKILL_ROUTES,
    WELL_KNOWN_PATHS,
    AdmissionReason,
    AdmissionResult,
    AgentCard,
    DelegationToken,
    TraceContext,
    admit_skill,
    execute,
    health,
    issue_card,
    mint_delegation,
    new_traceparent,
    parse_traceparent,
    propagate_traceparent,
    resolve_well_known_path,
    verify_card,
    verify_delegation,
)
from kit.mcp.types import ToolCall
from kit.world.fixture import FIXTURE_ASKS, build_fixture_world
from kit.world.loader import World

try:
    from kit.mcp.hardmode import HardMode

    _HAS_HARDMODE = True
except ImportError:  # pragma: no cover - collaborator file
    _HAS_HARDMODE = False


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> World:
    dest = tmp_path_factory.mktemp("a2a-world")
    return World.load(build_fixture_world(dest, include_truth=True))

# ---------------------------------------------------------------------------
# The three real peers — declared once here, module-level, for every test
# below to reuse without recomputing the registry-signed cards each time.
# ---------------------------------------------------------------------------


def test_all_three_peers_declared_with_at_least_one_skill() -> None:
    assert set(AGENT_CARDS) == KNOWN_PEERS == {"curriculum-analyst", "citation-checker", "roster"}
    for peer, card in AGENT_CARDS.items():
        assert card.name == peer
        assert card.skills, f"{peer} declares no skills"
        assert card.signature is not None


def test_curriculum_analyst_and_citation_checker_skill_names_match_specs() -> None:
    # Only roster needs a SKILL_ROUTES bridge (see the module docstring's
    # resolved ambiguity #2) — these two already match kit/mcp/specs.py 1:1.
    assert AGENT_CARDS["curriculum-analyst"].skills == ("which_days_cover",)
    assert AGENT_CARDS["citation-checker"].skills == ("verify_source",)


def test_roster_declares_role_of_and_who_enrolled_routed_through_lookup_learner() -> None:
    assert AGENT_CARDS["roster"].skills == ("role_of", "who_enrolled")
    assert SKILL_ROUTES["role_of"] == ("roster", "lookup_learner")
    assert SKILL_ROUTES["who_enrolled"] == ("roster", "lookup_learner")
    # lookup_learner itself is the wire tool, not a card-declared skill name.
    assert "lookup_learner" not in AGENT_CARDS["roster"].skills


def test_skill_routes_cover_every_declared_skill_on_every_card() -> None:
    for card in AGENT_CARDS.values():
        for skill in card.skills:
            assert skill in SKILL_ROUTES, f"{card.name}.{skill} has no SKILL_ROUTES entry"


# ---------------------------------------------------------------------------
# AgentCard — construction, canonicalisation, round-trip.
# ---------------------------------------------------------------------------


def test_agentcard_skills_are_sorted_and_deduped_on_construction() -> None:
    card = AgentCard(name="roster", url="a2a://roster", version="0.3", skills=("who_enrolled", "role_of", "role_of"))
    assert card.skills == ("role_of", "who_enrolled")


def test_agentcard_rejects_bare_string_skills() -> None:
    with pytest.raises(ValueError):
        AgentCard(name="roster", url="a2a://roster", version="0.3", skills="role_of")  # type: ignore[arg-type]


def test_agentcard_rejects_empty_skill_set() -> None:
    with pytest.raises(ValueError):
        AgentCard(name="roster", url="a2a://roster", version="0.3", skills=())


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": ""},
        {"name": "Roster"},  # uppercase not allowed
        {"name": "im poster"},
        {"url": ""},
        {"version": ""},
        {"skills": ("Bad-Skill",)},
        {"skills": ("0startswithdigit",)},
    ],
)
def test_agentcard_rejects_malformed_fields(overrides: dict) -> None:
    kwargs = dict(name="roster", url="a2a://roster", version="0.3", skills=("role_of",))
    kwargs.update(overrides)
    with pytest.raises(ValueError):
        AgentCard(**kwargs)  # type: ignore[arg-type]


def test_agentcard_to_dict_from_dict_round_trip() -> None:
    card = AGENT_CARDS["citation-checker"]
    restored = AgentCard.from_dict(card.to_dict())
    assert restored == card


def test_issue_card_produces_a_verifiable_signature() -> None:
    card = issue_card("roster", "a2a://roster", "0.3", ["role_of", "who_enrolled"])
    result = verify_card(card)
    assert result.admitted


def test_canonical_bytes_ignores_signature_and_skill_order() -> None:
    a = issue_card("roster", "a2a://roster", "0.3", ["role_of", "who_enrolled"])
    b = issue_card("roster", "a2a://roster", "0.3", ["who_enrolled", "role_of"])
    assert a.canonical_bytes() == b.canonical_bytes()
    assert a.signature == b.signature


# ---------------------------------------------------------------------------
# resolve_well_known_path() — CORPUS-FACTS.md contradiction #8.
# ---------------------------------------------------------------------------


def test_well_known_paths_prefers_the_v03_rename() -> None:
    assert WELL_KNOWN_PATHS == (AGENT_CARD_PATH, AGENT_JSON_PATH_DEPRECATED)
    assert resolve_well_known_path([AGENT_JSON_PATH_DEPRECATED, AGENT_CARD_PATH]) == (AGENT_CARD_PATH, False)


def test_well_known_paths_falls_back_to_deprecated_old_path() -> None:
    assert resolve_well_known_path([AGENT_JSON_PATH_DEPRECATED]) == (AGENT_JSON_PATH_DEPRECATED, True)


def test_well_known_paths_neither_offered() -> None:
    assert resolve_well_known_path([]) == (None, False)
    assert resolve_well_known_path(["/something/else.json"]) == (None, False)


# ---------------------------------------------------------------------------
# verify_card() — admit / reject a peer.
# ---------------------------------------------------------------------------


def test_verify_card_admits_a_genuine_card() -> None:
    result = verify_card(AGENT_CARDS["curriculum-analyst"])
    assert result.admitted
    assert result.peer == "curriculum-analyst"
    assert result.reason is None
    assert result.declared_skills == ("which_days_cover",)


def test_verify_card_admits_dict_shape_identically_to_dataclass_shape() -> None:
    card = AGENT_CARDS["roster"]
    from_dataclass = verify_card(card)
    from_dict = verify_card(card.to_dict())
    assert from_dataclass == from_dict


def test_verify_card_new_path_is_not_flagged_deprecated() -> None:
    result = verify_card(AGENT_CARDS["roster"], discovery_path=AGENT_CARD_PATH)
    assert result.admitted and not result.deprecated_path and result.successor_path is None


def test_verify_card_old_path_admits_but_flags_deprecated() -> None:
    result = verify_card(AGENT_CARDS["roster"], discovery_path=AGENT_JSON_PATH_DEPRECATED)
    assert result.admitted
    assert result.deprecated_path is True
    assert result.successor_path == AGENT_CARD_PATH


def test_verify_card_rejects_unrecognised_discovery_path() -> None:
    result = verify_card(AGENT_CARDS["roster"], discovery_path="/not/a/real/path.json")
    assert not result.admitted
    assert result.reason is AdmissionReason.MALFORMED_CARD


def test_verify_card_rejects_unknown_peer_name() -> None:
    result = verify_card({"name": "imposter-server", "url": "a2a://x", "version": "0.3", "skills": ["x"]})
    assert not result.admitted
    assert result.reason is AdmissionReason.UNKNOWN_PEER
    assert result.peer == "imposter-server"


def test_verify_card_rejects_structurally_malformed_dict() -> None:
    result = verify_card({"name": "roster"})  # missing url/version/skills
    assert not result.admitted
    assert result.reason is AdmissionReason.MALFORMED_CARD
    assert result.peer is None  # never trusted enough to name a peer


def test_verify_card_rejects_missing_signature() -> None:
    unsigned = AgentCard(name="roster", url="a2a://roster", version="0.3", skills=("role_of", "who_enrolled"))
    result = verify_card(unsigned)
    assert not result.admitted
    assert result.reason is AdmissionReason.FORGED_CARD_SIGNATURE


def test_verify_card_forge_card_mutation_class_rejects_tampered_content() -> None:
    """The forge_card mutation op (CONTRACTS.md section 8's closed set):
    take a genuine card's signature but change the content underneath it."""
    genuine = AGENT_CARDS["curriculum-analyst"]
    forged = AgentCard(
        name=genuine.name,
        url=genuine.url,
        version=genuine.version,
        skills=genuine.skills + ("delete_all_learners",),  # inflated capability set
        description=genuine.description,
        signature=genuine.signature,  # the OLD signature, now stale
    )
    result = verify_card(forged)
    assert not result.admitted
    assert result.reason is AdmissionReason.FORGED_CARD_SIGNATURE


def test_verify_card_forged_signature_string_rejected() -> None:
    forged = AgentCard(
        name="citation-checker",
        url="a2a://citation-checker",
        version="0.3",
        skills=("verify_source",),
        signature="0" * 64,
    )
    result = verify_card(forged)
    assert not result.admitted
    assert result.reason is AdmissionReason.FORGED_CARD_SIGNATURE


# ---------------------------------------------------------------------------
# admit_skill() — the UNDECLARED-skill admission check.
# ---------------------------------------------------------------------------


def test_admit_skill_admits_a_declared_skill() -> None:
    for skill in ("role_of", "who_enrolled"):
        result = admit_skill(AGENT_CARDS["roster"], skill)
        assert result.admitted, skill


def test_admit_skill_refuses_an_undeclared_skill() -> None:
    result = admit_skill(AGENT_CARDS["curriculum-analyst"], "delete_all_learners")
    assert not result.admitted
    assert result.reason is AdmissionReason.UNDECLARED_SKILL
    assert result.declared_skills == ("which_days_cover",)


def test_admit_skill_refuses_the_wire_tool_name_itself() -> None:
    """lookup_learner is what specs.py prices, not what the card declares
    — a call naming the wire tool directly, bypassing the two declared
    skill names, must still be refused as undeclared."""
    result = admit_skill(AGENT_CARDS["roster"], "lookup_learner")
    assert not result.admitted
    assert result.reason is AdmissionReason.UNDECLARED_SKILL


def test_admit_skill_propagates_card_level_denials_unchanged() -> None:
    forged = AgentCard(
        name="roster", url="a2a://roster", version="0.3", skills=("role_of",), signature="bad" * 20
    )
    result = admit_skill(forged, "role_of")
    assert not result.admitted
    assert result.reason is AdmissionReason.FORGED_CARD_SIGNATURE


# ---------------------------------------------------------------------------
# AdmissionResult — its own shape invariants.
# ---------------------------------------------------------------------------


def test_admissionresult_admitted_true_forbids_reason() -> None:
    with pytest.raises(ValueError):
        AdmissionResult(admitted=True, reason=AdmissionReason.UNDECLARED_SKILL)


def test_admissionresult_admitted_false_requires_reason() -> None:
    with pytest.raises(ValueError):
        AdmissionResult(admitted=False)


def test_admissionresult_deprecated_path_requires_successor() -> None:
    with pytest.raises(ValueError):
        AdmissionResult(admitted=True, deprecated_path=True)


def test_admissionresult_successor_only_meaningful_when_deprecated() -> None:
    with pytest.raises(ValueError):
        AdmissionResult(admitted=True, successor_path=AGENT_CARD_PATH)


def test_admissionresult_declared_skills_sorted_and_deduped() -> None:
    result = AdmissionResult(admitted=True, declared_skills=("b", "a", "a"))
    assert result.declared_skills == ("a", "b")


def test_as_tool_error_maps_to_unauthorized() -> None:
    denied = AdmissionResult(admitted=False, peer="roster", reason=AdmissionReason.UNDECLARED_SKILL)
    err = denied.as_tool_error()
    assert err["code"] == "unauthorized"
    assert err["admission_reason"] == "undeclared_skill"


def test_as_tool_error_refuses_on_an_admitted_result() -> None:
    admitted = AdmissionResult(admitted=True, peer="roster")
    with pytest.raises(ValueError):
        admitted.as_tool_error()


# ---------------------------------------------------------------------------
# DelegationToken / mint_delegation — construction and determinism.
# ---------------------------------------------------------------------------


def test_mint_delegation_is_deterministic() -> None:
    a = mint_delegation("learner:sv-0417", "a2a:curriculum-analyst", 3, sub="agent:vlearn-tutor", call_index=1)
    b = mint_delegation("learner:sv-0417", "a2a:curriculum-analyst", 3, sub="agent:vlearn-tutor", call_index=1)
    assert a == b
    assert a.token_id == b.token_id
    assert a.signature == b.signature


def test_mint_delegation_nonce_disambiguates_same_call_index() -> None:
    a = mint_delegation("learner:sv-0417", "a2a:roster", 1, call_index=0, nonce=0)
    b = mint_delegation("learner:sv-0417", "a2a:roster", 1, call_index=0, nonce=1)
    assert a.token_id != b.token_id


def test_mint_delegation_defaults() -> None:
    token = mint_delegation("learner:sv-0417", "a2a:roster", 2)
    assert token.sub == "agent:student"
    assert token.minted_at_call_index == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"act": "bogus"},
        {"act": "Learner:sv-0417"},  # uppercase kind not allowed
        {"sub": ""},
        {"aud": "http://not-a2a-or-mcp"},
        {"aud": "a2a:UpperCase"},
        {"ttl": -1},
        {"minted_at_call_index": -1},
        {"token_id": ""},
        {"signature": ""},
    ],
)
def test_delegationtoken_rejects_malformed_fields(overrides: dict) -> None:
    kwargs = dict(
        token_id="dlg:deadbeefdeadbeef",
        act="learner:sv-0417",
        sub="agent:vlearn-tutor",
        aud="a2a:curriculum-analyst",
        ttl=3,
        minted_at_call_index=0,
        signature="a" * 64,
    )
    kwargs.update(overrides)
    with pytest.raises(ValueError):
        DelegationToken(**kwargs)  # type: ignore[arg-type]


def test_delegationtoken_is_expired_boundary() -> None:
    token = mint_delegation("learner:sv-0417", "a2a:roster", 3, call_index=2)
    assert token.is_expired(4) is False  # 2 hops elapsed, ttl=3 -> still valid
    assert token.is_expired(5) is False  # exactly at the boundary (== ttl)
    assert token.is_expired(6) is True  # one past the boundary


def test_delegationtoken_to_dict_from_dict_round_trip() -> None:
    token = mint_delegation("learner:sv-0417", "a2a:citation-checker", 2, call_index=3)
    restored = DelegationToken.from_dict(token.to_dict())
    assert restored == token


# ---------------------------------------------------------------------------
# verify_delegation() — the five named failure modes, plus the happy path.
# ---------------------------------------------------------------------------


@pytest.fixture
def legit_token() -> DelegationToken:
    return mint_delegation(
        "learner:sv-0417", "a2a:curriculum-analyst", 3, sub="agent:vlearn-tutor", call_index=1
    )


def test_verify_delegation_happy_path(legit_token: DelegationToken) -> None:
    result = verify_delegation(
        legit_token, aud="a2a:curriculum-analyst", call_index=2, expected_act="learner:sv-0417"
    )
    assert result.admitted
    assert result.peer == "curriculum-analyst"


def test_verify_delegation_accepts_dict_shape(legit_token: DelegationToken) -> None:
    result = verify_delegation(
        legit_token.to_dict(), aud="a2a:curriculum-analyst", call_index=2, expected_act="learner:sv-0417"
    )
    assert result.admitted


def test_verify_delegation_aud_mismatch_is_the_identity_attack(legit_token: DelegationToken) -> None:
    """CONTRACTS.md section 8: "A delegation whose aud does not match the
    server actually called is the 'identity' attack class." A token
    legitimately minted for curriculum-analyst, presented to roster."""
    result = verify_delegation(
        legit_token, aud="a2a:roster", call_index=2, expected_act="learner:sv-0417"
    )
    assert not result.admitted
    assert result.reason is AdmissionReason.AUD_MISMATCH


def test_verify_delegation_act_escalation_ignores_sub(legit_token: DelegationToken) -> None:
    """The deck.json replace_act mutation example (CONTRACTS.md section 8):
    same sub, swapped act. Authority derives from act, never sub — this
    must fire regardless of how legitimate sub looks."""
    assert legit_token.sub == "agent:vlearn-tutor"  # sub is untouched and legitimate
    result = verify_delegation(
        legit_token, aud="a2a:curriculum-analyst", call_index=2, expected_act="learner:sv-0392"
    )
    assert not result.admitted
    assert result.reason is AdmissionReason.ACT_ESCALATION


def test_verify_delegation_replayed_token(legit_token: DelegationToken) -> None:
    result = verify_delegation(
        legit_token,
        aud="a2a:curriculum-analyst",
        call_index=2,
        expected_act="learner:sv-0417",
        seen_token_ids={legit_token.token_id},
    )
    assert not result.admitted
    assert result.reason is AdmissionReason.REPLAYED_TOKEN


def test_verify_delegation_replayed_token_ignores_unrelated_ids(legit_token: DelegationToken) -> None:
    result = verify_delegation(
        legit_token,
        aud="a2a:curriculum-analyst",
        call_index=2,
        expected_act="learner:sv-0417",
        seen_token_ids={"dlg:someotherid"},
    )
    assert result.admitted


def test_verify_delegation_expired(legit_token: DelegationToken) -> None:
    # minted_at_call_index=1, ttl=3 -> valid through call_index 4
    result = verify_delegation(
        legit_token, aud="a2a:curriculum-analyst", call_index=5, expected_act="learner:sv-0417"
    )
    assert not result.admitted
    assert result.reason is AdmissionReason.EXPIRED


def test_verify_delegation_forged_signature_after_tampering(legit_token: DelegationToken) -> None:
    tampered = legit_token.to_dict()
    tampered["act"] = "learner:sv-0392"
    result = verify_delegation(
        tampered, aud="a2a:curriculum-analyst", call_index=2, expected_act="learner:sv-0392"
    )
    assert not result.admitted
    assert result.reason is AdmissionReason.FORGED_TOKEN_SIGNATURE


def test_verify_delegation_forged_signature_takes_priority_over_act_escalation(
    legit_token: DelegationToken,
) -> None:
    """A tampered act, verified against the ATTACKER'S claimed act (so a
    naive act-only check would pass it): must still be caught, because the
    signature no longer matches — signature verification runs first."""
    tampered = legit_token.to_dict()
    tampered["act"] = "learner:sv-0392"
    result = verify_delegation(
        tampered, aud="a2a:curriculum-analyst", call_index=2, expected_act="learner:sv-0392"
    )
    assert result.reason is AdmissionReason.FORGED_TOKEN_SIGNATURE  # not ACT_ESCALATION


def test_verify_delegation_malformed_token_dict_rejected() -> None:
    result = verify_delegation(
        {"act": "learner:sv-0417"},  # missing everything else
        aud="a2a:curriculum-analyst",
        call_index=1,
        expected_act="learner:sv-0417",
    )
    assert not result.admitted
    assert result.reason is AdmissionReason.FORGED_TOKEN_SIGNATURE


def test_verify_delegation_gate_ordering_signature_before_aud(legit_token: DelegationToken) -> None:
    """A tampered aud is caught by the signature check first, not reported
    as a plain aud_mismatch (which would imply the token was otherwise
    trustworthy)."""
    tampered = legit_token.to_dict()
    tampered["aud"] = "a2a:roster"
    result = verify_delegation(
        tampered, aud="a2a:roster", call_index=2, expected_act="learner:sv-0417"
    )
    assert result.reason is AdmissionReason.FORGED_TOKEN_SIGNATURE


# ---------------------------------------------------------------------------
# TraceContext / traceparent propagation.
# ---------------------------------------------------------------------------


def test_new_traceparent_is_deterministic_in_seed() -> None:
    a = new_traceparent("d03-r06-A")
    b = new_traceparent("d03-r06-A")
    assert a == b
    c = new_traceparent("d03-r06-B")
    assert a.trace_id != c.trace_id


def test_new_traceparent_sampled_flag() -> None:
    sampled = new_traceparent("seed", sampled=True)
    unsampled = new_traceparent("seed", sampled=False)
    assert sampled.flags == "01"
    assert unsampled.flags == "00"


def test_traceparent_round_trips_through_header_string() -> None:
    ctx = new_traceparent("d03-r06-A")
    restored = parse_traceparent(ctx.to_header())
    assert restored == ctx


def test_parse_traceparent_rejects_malformed_header() -> None:
    for bad in ["", "not-a-traceparent", "00-short-short-01", "01-" + "g" * 32 + "-" + "0" * 16 + "-01"]:
        with pytest.raises(ValueError):
            parse_traceparent(bad)


def test_child_keeps_trace_id_changes_parent_id() -> None:
    root = new_traceparent("d03-r06-A")
    hop = root.child("curriculum-analyst:call_index=1")
    assert hop.trace_id == root.trace_id
    assert hop.parent_id != root.parent_id


def test_child_is_deterministic_in_hop_seed() -> None:
    root = new_traceparent("d03-r06-A")
    a = root.child("hop-1")
    b = root.child("hop-1")
    c = root.child("hop-2")
    assert a == b
    assert a.parent_id != c.parent_id


def test_propagate_traceparent_accepts_context_or_header_string() -> None:
    root = new_traceparent("d03-r06-A")
    via_context = propagate_traceparent(root, hop_seed="hop-1")
    via_string = propagate_traceparent(root.to_header(), hop_seed="hop-1")
    assert via_context == via_string


def test_tracecontext_rejects_all_zero_trace_id() -> None:
    with pytest.raises(ValueError):
        TraceContext(version="00", trace_id="0" * 32, parent_id="a" * 16, flags="01")


def test_tracecontext_rejects_all_zero_parent_id() -> None:
    with pytest.raises(ValueError):
        TraceContext(version="00", trace_id="a" * 32, parent_id="0" * 16, flags="01")


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": "x"},
        {"trace_id": "short"},
        {"parent_id": "TOOSHORT"},
        {"flags": "zz"},
    ],
)
def test_tracecontext_rejects_malformed_fields(overrides: dict) -> None:
    kwargs = dict(version="00", trace_id="a" * 32, parent_id="b" * 16, flags="01")
    kwargs.update(overrides)
    with pytest.raises(ValueError):
        TraceContext(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# health() / DEGRADED — the new workspace "degrade loudly" rule.
# ---------------------------------------------------------------------------


def test_health_reports_not_degraded_in_this_environment() -> None:
    h = health()
    assert h["ok"] is True
    assert h["degraded"] == ()
    assert h["has_specs"] and h["has_errors"] and h["has_types"] and h["has_servers"]
    assert DEGRADED == ()


# ---------------------------------------------------------------------------
# execute() — ENGINE-REPORT.md D-5's fix: admission actually reachable.
# THE POINT of every test below: a call that fails admission must NEVER
# reach kit.mcp.servers.handle() at all — it is exercised through
# execute() end to end, never by calling verify_delegation() in isolation
# and assuming the wiring works.
# ---------------------------------------------------------------------------

_ACT = "learner:sv-0417"


def test_execute_which_days_cover_admits_and_dispatches(world: World) -> None:
    token = mint_delegation(_ACT, "a2a:curriculum-analyst", ttl=3, call_index=0)
    call = ToolCall(server="curriculum-analyst", tool="which_days_cover", args={"concept": "Concept:streamable-http"})
    r = execute(world, call, expected_act=_ACT, delegation=token, seen_token_ids=set())
    assert r["ok"] is True
    assert r["rows"][0]["course_day"] == 26


def test_execute_which_days_cover_is_still_confidently_wrong(world: World) -> None:
    """execute()'s admission gate must never LAUNDER the faithless-peer
    surface into a correct one -- admission and correctness are orthogonal
    (the task brief's whole point)."""
    token = mint_delegation(_ACT, "a2a:curriculum-analyst", ttl=3, call_index=0)
    call = ToolCall(server="curriculum-analyst", tool="which_days_cover", args={"concept": "Concept:streamable-http"})
    r = execute(world, call, expected_act=_ACT, delegation=token, seen_token_ids=set())
    truth = world.truth(FIXTURE_ASKS["which_day_covers"])
    assert r["ok"] is True
    assert r["rows"][0]["anchor"] != truth["anchor"]


def test_execute_verify_source_admits_and_dispatches(world: World) -> None:
    token = mint_delegation(_ACT, "a2a:citation-checker", ttl=3, call_index=0)
    call = ToolCall(server="citation-checker", tool="verify_source", args={"anchor": "Source:mcp-spec-2026-07-28"})
    r = execute(world, call, expected_act=_ACT, delegation=token, seen_token_ids=set())
    assert r["ok"] is True
    assert r["rows"][0]["anchor"] == "Source:mcp-spec-2026-07-28"


@pytest.mark.skipif(not _HAS_HARDMODE, reason="kit.mcp.hardmode not importable")
def test_execute_verify_source_rate_limit_composes_through_hardmode(world: World) -> None:
    """The 2-per-3-rounds rate limit lives in kit.mcp.specs.TOOL_SPECS and
    is enforced by kit.mcp.hardmode -- execute() must pass `hardmode`
    through to kit.mcp.servers.handle() unmodified for this to fire."""
    hm = HardMode(world=world, opaque_enabled=False)
    hm.reset("test-execute-rate", world_id=world.manifest["world_id"])
    outcomes = []
    for round_no in (1, 2, 3):
        hm.begin_round(round_no)
        token = mint_delegation(_ACT, "a2a:citation-checker", ttl=3, call_index=round_no)
        call = ToolCall(
            server="citation-checker", tool="verify_source",
            args={"anchor": "Source:mcp-spec-2026-07-28"}, call_index=round_no,
        )
        outcomes.append(execute(world, call, expected_act=_ACT, delegation=token, hardmode=hm))
    assert outcomes[0]["ok"] is True and outcomes[1]["ok"] is True
    assert outcomes[2]["ok"] is False and outcomes[2]["error"]["code"] == "rate_limited"


def test_execute_lookup_learner_self_read_admits_and_dispatches(world: World) -> None:
    token = mint_delegation(_ACT, "a2a:roster", ttl=3, call_index=0)
    call = ToolCall(server="roster", tool="lookup_learner", args={"learner": "Learner:sv-0417"}, fields=("*",))
    r = execute(world, call, expected_act=_ACT, delegation=token, seen_token_ids=set())
    assert r["ok"] is True
    assert r["rows"][0]["act"] == _ACT


def test_execute_lookup_learner_cross_learner_refused_after_full_admission(world: World) -> None:
    """Admission (card/skill/aud/act/replay all clean) is orthogonal to
    roster.lookup_learner's OWN identity boundary -- a fully-admitted
    caller can still be refused by the tool itself for targeting someone
    else's record (kit/mcp/servers.py's RESOLVED AMBIGUITY 7)."""
    token = mint_delegation(_ACT, "a2a:roster", ttl=3, call_index=0)
    call = ToolCall(server="roster", tool="lookup_learner", args={"learner": "Learner:sv-0392"})
    r = execute(world, call, expected_act=_ACT, delegation=token, seen_token_ids=set())
    assert r["ok"] is False
    assert r["error"]["code"] == "unauthorized"
    # NOT an admission denial -- no admission_reason key, this came from
    # kit.mcp.servers._h_roster_lookup_learner itself, past the gate.
    assert "admission_reason" not in r["error"]


@pytest.mark.parametrize(
    "make_call_and_kwargs,expected_reason",
    [
        (
            lambda act: (
                ToolCall(server="citation-checker", tool="verify_source", args={"anchor": "Source:mcp-spec-2026-07-28"}),
                {"delegation": mint_delegation(act, "a2a:roster", ttl=3, call_index=0)},  # wrong aud
            ),
            AdmissionReason.AUD_MISMATCH,
        ),
        (
            lambda act: (
                ToolCall(server="roster", tool="lookup_learner", args={"learner": "Learner:sv-0417"}),
                {"delegation": None},  # no token presented at all
            ),
            AdmissionReason.FORGED_TOKEN_SIGNATURE,
        ),
        (
            lambda act: (
                ToolCall(server="roster", tool="lookup_learner", args={"learner": "Learner:sv-0417"}),
                {
                    "delegation": mint_delegation(act, "a2a:roster", ttl=1, call_index=0),
                    "call_index_override": 5,  # past the ttl window
                },
            ),
            AdmissionReason.EXPIRED,
        ),
        (
            lambda act: (
                ToolCall(server="curriculum-analyst", tool="which_days_cover", args={"skill": "delete_all_learners"}),
                {"delegation": mint_delegation(act, "a2a:curriculum-analyst", ttl=3, call_index=0)},
            ),
            AdmissionReason.UNDECLARED_SKILL,
        ),
        (
            lambda act: (
                ToolCall(server="not-a-real-peer", tool="anything", args={}),
                {"delegation": None},
            ),
            AdmissionReason.UNKNOWN_PEER,
        ),
    ],
)
def test_execute_denies_before_ever_reaching_the_world(world: World, make_call_and_kwargs, expected_reason) -> None:
    call, kwargs = make_call_and_kwargs(_ACT)
    call_index_override = kwargs.pop("call_index_override", None)
    if call_index_override is not None:
        call = ToolCall(server=call.server, tool=call.tool, args=call.args, call_index=call_index_override)
    r = execute(world, call, expected_act=_ACT, **kwargs)
    assert r["ok"] is False
    assert r["error"]["code"] == "unauthorized"
    assert r["error"]["admission_reason"] == expected_reason.value


def test_execute_act_escalation_replace_act_mutation(world: World) -> None:
    """CONTRACTS.md section 8's deck.json example, verbatim: a token
    legitimately minted for sv-0417, presented while the caller's own
    authenticated act is sv-0392 (the replace_act mutation's target)."""
    token = mint_delegation("learner:sv-0417", "a2a:roster", ttl=3, call_index=0)
    call = ToolCall(server="roster", tool="lookup_learner", args={"learner": "Learner:sv-0417"})
    r = execute(world, call, expected_act="learner:sv-0392", delegation=token)
    assert r["error"]["admission_reason"] == "act_escalation"


def test_execute_denial_charges_the_attempted_cost(world: World) -> None:
    """CONTRACTS.md 3.3: unauthorized is Charged? yes -- an admission
    denial must not be free."""
    call = ToolCall(server="citation-checker", tool="verify_source", args={"anchor": "Source:mcp-spec-2026-07-28"})
    r = execute(world, call, expected_act=_ACT, delegation=mint_delegation(_ACT, "a2a:roster", ttl=3))  # wrong aud
    assert r["error"]["admission_reason"] == "aud_mismatch"
    assert r["cost"] > 0


def test_execute_unpriced_tool_is_bad_request_not_unauthorized(world: World) -> None:
    call = ToolCall(server="roster", tool="not_a_real_tool", args={})
    r = execute(world, call, expected_act=_ACT, delegation=None)
    assert r["ok"] is False
    assert r["error"]["code"] == "bad_request"


def test_execute_never_bypasses_admission_for_a_deck_json_style_forged_call(world: World) -> None:
    """A composite check that admission runs in full even when everything
    ELSE about a call looks legitimate: right peer, right tool, right
    skill, unexpired, unreplayed token -- but minted for a different
    act than the caller now claims. Every layer must independently agree
    before the world is ever touched."""
    legit_token = mint_delegation("learner:sv-0417", "a2a:roster", ttl=5, call_index=0)
    forged_claim = execute(
        world,
        ToolCall(server="roster", tool="lookup_learner", args={"learner": "Learner:sv-0392"}, call_index=1),
        expected_act="learner:sv-0392",  # attacker claims to BE sv-0392
        delegation=legit_token,  # but the token was minted for sv-0417
    )
    assert forged_claim["ok"] is False
    assert forged_claim["error"]["admission_reason"] == "act_escalation"


if __name__ == "__main__":
    import pytest as _pytest

    raise SystemExit(_pytest.main([__file__, "-v"]))
