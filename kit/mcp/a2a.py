"""kit/mcp/a2a.py — three A2A peers and the Agent Card admission surface.

The identity half of the Day-26 lecture: FINAL-PLAN.md section 4 gives three
A2A peers (`curriculum-analyst` · `citation-checker` · `roster`) alongside the
seven MCP servers, and CONTRACTS.md section 8's `identity` duel class exists
because A2A carries its OWN discovery/auth surface, distinct from the MCP tool
contract in `kit/mcp/types.py`. This module is that surface:

  * an :class:`AgentCard` per peer, served at the 2026-era well-known path,
    with a registry signature a gateway can verify without trusting whoever
    served the document;
  * :func:`verify_card` / :func:`admit_skill` — the admission gate a gateway
    calls to decide whether to trust a peer and whether a specific skill call
    is even declared;
  * :class:`DelegationToken` / :func:`mint_delegation` / :func:`verify_delegation`
    — per-hop tokens carrying `act` / `sub` / `aud`, where **authority derives
    from `act` (whom you serve), never from `sub` (what you are)**;
  * :class:`TraceContext` / :func:`new_traceparent` / :func:`propagate_traceparent`
    — W3C `traceparent` propagation across hops.

RESOLVED AMBIGUITIES (this task's brief vs. the frozen contracts):

1. **Agent Card discovery path.** CORPUS-FACTS.md contradiction #8: the
   canonical day26 deck still says `/.well-known/agent.json`; the working
   deck already has the v0.3 rename to `/.well-known/agent-card.json`. Per
   this task's brief ("support both, prefer the new one, and mark the old
   path deprecated"), :data:`WELL_KNOWN_PATHS` lists both,
   :func:`resolve_well_known_path` prefers the new one, and `verify_card`'s
   `discovery_path` argument admits the old path but flags it
   `deprecated_path=True` — the same "still works, but flagged" shape
   CONTRACTS.md 4.2 mechanic 8 gives `slides.search`'s deprecation, not an
   outright rejection.

2. **roster's declared skills vs. its costed wire tool.** This task's brief
   names roster's identity-boundary skills as `role_of` and `who_enrolled`.
   `kit/mcp/specs.py` (a collaborator's file, already committed and
   `_validate_specs`-checked at its own import time) prices exactly one A2A
   tool for roster: `("roster", "lookup_learner")` — CONTRACTS.md section 3
   leaves A2A skill *names* undefined, only the priced `(server, tool)` wire
   shape, so this is not a contradiction of anything frozen, just two naming
   layers that need a bridge. Resolution taken here: the Agent Card declares
   both `role_of` and `who_enrolled` (this task's brief is authoritative for
   what THIS file ships), and :data:`SKILL_ROUTES` maps each to the single
   `(roster, lookup_learner)` RPC specs.py actually charges for — so a future
   executor built against specs.py's pricing and a gateway built against this
   card's declared skills agree on what roster can do. `_cross_check_against_specs`
   asserts this mapping resolves against `kit.mcp.specs.TOOL_SPECS` whenever
   that module is importable, so a retune of either file that breaks the
   bridge fails loudly at import time rather than silently at duel time.

3. **`aud` values reuse `deck.json`'s `mutation.target` convention exactly**
   (CONTRACTS.md section 8's card example: `"target": "a2a:curriculum-analyst"`)
   — `"a2a:<peer>"` / `"mcp:<server>"` — so a mutation card and a delegation
   token name the same peer the same way.

4. **`ttl` is a call-index hop budget, never wall-clock** (this workspace's
   hard rule 4; CONTRACTS.md section 11 bans wall-clock in anything that
   touches a score). A token minted at `call_index=N` with `ttl=T` stays
   valid through `call_index<=N+T` — the same shape CONTRACTS.md 4.2
   mechanic 2 gives a `get_frame` lease ("valid 3 calls"), applied to
   delegation instead of frame reads.

5. **`execute()` — the admission-gated execution bridge (ENGINE-REPORT.md D-5's
   fix).** Every function above this point in the file already existed and
   fully implemented ADMISSION (card verification, declared-skill checks,
   per-hop `aud`, delegation tokens) — but nothing EXECUTED an admitted
   skill: the three real A2A wire tools (`curriculum-analyst.which_days_cover`
   / `citation-checker.verify_source` / `roster.lookup_learner`) had no
   caller at all, so admission was unreachable in play. :func:`execute` is
   that caller: it runs :func:`admit_skill` (which already runs
   :func:`verify_card` internally) and :func:`verify_delegation`, and ONLY
   on full admission calls `kit.mcp.servers.handle` — which now has real
   executors for all three (a `kit/mcp/servers.py` fix; this module never
   duplicates that world-lookup logic, only gates the path to it). A denied
   call never reaches `handle()` at all: admission runs first, unconditionally,
   for every one of the three A2A tools, every time. NEW WORKSPACE RULE
   applied throughout this module: every previously-silent
   `except (ImportError, AttributeError): _HAS_X = False` now also
   `warnings.warn()`s, and :data:`DEGRADED`/:func:`health` give a gate
   something to assert on rather than guess at.

Stdlib only (`hashlib`, `hmac`, `json`, `re`, `warnings`). No network. No
`datetime.now()` / `time.time()` / unseeded `random` anywhere: `mint_delegation`,
`issue_card`, and `new_traceparent` are all pure functions of their own
arguments, so replaying an exchange through `FrozenBroker` mints
byte-identical cards, tokens, and trace ids at every hop (CONTRACTS.md
section 11, G-REPRO). `execute()` itself does no I/O either — `world` is
handed in already loaded, exactly like every `kit.mcp.servers` handler.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import warnings
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "AdmissionReason",
    "AdmissionResult",
    "AgentCard",
    "DelegationToken",
    "TraceContext",
    "KNOWN_PEERS",
    "AGENT_CARDS",
    "SKILL_ROUTES",
    "WELL_KNOWN_PATHS",
    "AGENT_CARD_PATH",
    "AGENT_JSON_PATH_DEPRECATED",
    "issue_card",
    "verify_card",
    "admit_skill",
    "resolve_well_known_path",
    "mint_delegation",
    "verify_delegation",
    "new_traceparent",
    "propagate_traceparent",
    "parse_traceparent",
    "execute",
    "health",
    "DEGRADED",
]

# ---------------------------------------------------------------------------
# Best-effort integration with three collaborator files: the cost table
# (kit/mcp/specs.py), the error taxonomy (kit/mcp/errors.py), the shared
# request/result types (kit/mcp/types.py), and the tool dispatcher
# (kit/mcp/servers.py). Every dataclass and admission function ABOVE
# :func:`execute` in this file works with NONE of these present — per this
# workspace's hard rule 2, a missing collaborator file degrades this module
# to "no cross-check available" / "no error-shape convenience", never to an
# ImportError at import time. NEW WORKSPACE RULE (ENGINE-REPORT.md D-3's
# root cause, generalised): a degrade that cannot be observed is a bug that
# cannot be found — every branch below is now LOUD (a RuntimeWarning at
# import time) and :data:`DEGRADED`/:func:`health` give a gate something
# to assert on. :func:`execute` itself, unlike the admission primitives
# above it, genuinely NEEDS all four to do its job (it prices a denial and
# dispatches an admitted call) — see its own docstring for how it behaves
# when one is missing.
# ---------------------------------------------------------------------------
try:
    from kit.mcp.specs import A2A_PEERS, TOOL_SPECS, cost_of as _spec_cost_of  # type: ignore[import-not-found]

    _HAS_SPECS = True
except (ImportError, AttributeError):  # pragma: no cover - collaborator file
    A2A_PEERS = frozenset({"curriculum-analyst", "citation-checker", "roster"})
    TOOL_SPECS = {}
    _spec_cost_of = None
    _HAS_SPECS = False
    warnings.warn(
        "kit.mcp.a2a: kit.mcp.specs is not importable — no cross-check "
        "against the real cost table, and execute() cannot price a denied "
        "call (every denial will charge cost=0). Call kit.mcp.a2a.health() "
        "to check this at runtime.",
        RuntimeWarning,
        stacklevel=2,
    )

try:
    from kit.mcp.errors import ErrorCode, make_error  # type: ignore[import-not-found]

    _HAS_ERRORS = True
except (ImportError, AttributeError):  # pragma: no cover - collaborator file
    _HAS_ERRORS = False
    warnings.warn(
        "kit.mcp.a2a: kit.mcp.errors is not importable — AdmissionResult."
        "as_tool_error() falls back to a plain {'code': 'unauthorized', "
        "'admission_reason': ...} dict instead of kit.mcp.errors.make_error's "
        "validated shape. Call kit.mcp.a2a.health() to check this at runtime.",
        RuntimeWarning,
        stacklevel=2,
    )

try:
    from kit.mcp.types import ToolCall, ToolResult  # type: ignore[import-not-found]

    _HAS_TYPES = True
except (ImportError, AttributeError):  # pragma: no cover - collaborator file
    _HAS_TYPES = False
    ToolCall = None  # type: ignore[assignment,misc]
    ToolResult = None  # type: ignore[assignment,misc]
    warnings.warn(
        "kit.mcp.a2a: kit.mcp.types is not importable — execute() is "
        "unavailable and will refuse every call with a LOUD 'unavailable' "
        "rather than crash or silently no-op. Call kit.mcp.a2a.health() to "
        "check this at runtime.",
        RuntimeWarning,
        stacklevel=2,
    )

try:
    from kit.mcp.servers import handle as _servers_handle  # type: ignore[import-not-found]

    _HAS_SERVERS = True
except ImportError:  # pragma: no cover - collaborator file
    _HAS_SERVERS = False
    _servers_handle = None
    warnings.warn(
        "kit.mcp.a2a: kit.mcp.servers is not importable — execute() cannot "
        "dispatch an admitted call and will refuse every call with a LOUD "
        "'unavailable' rather than crash or silently no-op. Call "
        "kit.mcp.a2a.health() to check this at runtime.",
        RuntimeWarning,
        stacklevel=2,
    )


DEGRADED: tuple[str, ...] = tuple(
    sorted(
        name
        for name, present in (
            ("kit.mcp.specs", _HAS_SPECS),
            ("kit.mcp.errors", _HAS_ERRORS),
            ("kit.mcp.types", _HAS_TYPES),
            ("kit.mcp.servers", _HAS_SERVERS),
        )
        if not present
    )
)


def health() -> dict:
    """``{"ok": bool, "degraded": (...), "has_specs"/"has_errors"/
    "has_types"/"has_servers": bool}``. ``ok`` is ``False`` iff any
    optional collaborator this module can degrade without failed to
    import. In particular: :func:`execute` needs ``has_types`` AND
    ``has_servers`` to do anything at all (see its own docstring) — a gate
    that will drive `execute()` in a real duel should call this first and
    refuse to proceed on a degraded kit, exactly the discipline
    `kit.mcp.servers.health()` documents for the same reason."""
    return {
        "ok": not DEGRADED,
        "degraded": DEGRADED,
        "has_specs": _HAS_SPECS,
        "has_errors": _HAS_ERRORS,
        "has_types": _HAS_TYPES,
        "has_servers": _HAS_SERVERS,
    }


# ---------------------------------------------------------------------------
# The simulated registry PKI. A GAME secret standing in for a real signing
# key over the three peers' identities and their delegation tokens — never
# the DEEPSEEK_API_KEY and never a literal starting with "sk-" (this
# workspace's hard rule 3 is about model-provider keys; this HMAC key signs
# in-game Agent Cards / tokens, an entirely different trust boundary that
# never touches a network call).
# ---------------------------------------------------------------------------
_REGISTRY_KEY = b"colosseum-a2a-registry-2026-07-28"


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _hmac_hex(domain: str, payload: bytes) -> str:
    """Domain-separated HMAC-SHA256 over `payload`, keyed by the registry
    secret. `domain` stops a signature minted for one purpose (an Agent
    Card) from ever verifying as valid for another (a delegation token),
    even on colliding byte payloads — cheap insurance against a cross-
    protocol confusion attack, the same failure family as `header_spoof`."""
    mac = hmac.new(_REGISTRY_KEY, domain.encode("ascii") + b":" + payload, hashlib.sha256)
    return mac.hexdigest()


def _peer_name_from_aud(aud: str) -> str:
    _, _, name = aud.partition(":")
    return name or aud


KNOWN_PEERS: frozenset[str] = frozenset({"curriculum-analyst", "citation-checker", "roster"})

_PEER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# CONTRACTS.md section 4.2's own examples: act="learner:sv-0417",
# sub="agent:vlearn-tutor" — "<kind>:<name>", kind lowercase, name loose.
_ACT_RE = re.compile(r"^[a-z][a-z0-9_-]*:[A-Za-z0-9_.-]+$")
_SUB_RE = _ACT_RE
# CONTRACTS.md section 8's mutation.target convention: "a2a:curriculum-analyst".
_AUD_RE = re.compile(r"^(?:a2a|mcp):[a-z][a-z0-9-]*$")


# ===========================================================================
# AdmissionReason / AdmissionResult — one decision shape for every admission
# call this module makes (card, skill, or delegation token), mirroring
# kit/mcp/types.py's ToolResult "one shape, always" discipline.
# ===========================================================================


class AdmissionReason(StrEnum):
    """Closed set of reasons an :class:`AdmissionResult` can deny for.

    Every failure mode this task's brief names by name is here —
    `undeclared_skill`, `aud_mismatch`, `replayed_token`,
    `forged_card_signature`, `act_escalation` — plus the small number of
    structural/lifecycle reasons a real admission gate also needs
    (`malformed_card`, `unknown_peer`, `forged_token_signature`, `expired`).
    """

    MALFORMED_CARD = "malformed_card"
    UNKNOWN_PEER = "unknown_peer"
    FORGED_CARD_SIGNATURE = "forged_card_signature"
    UNDECLARED_SKILL = "undeclared_skill"
    FORGED_TOKEN_SIGNATURE = "forged_token_signature"
    AUD_MISMATCH = "aud_mismatch"
    ACT_ESCALATION = "act_escalation"
    REPLAYED_TOKEN = "replayed_token"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    """What a gateway does with a peer, a card, a skill call, or a
    delegation token. `admitted=True` -> forward; `admitted=False` ->
    `reason` names exactly why (an :class:`AdmissionReason`), never a
    free-text guess the caller has to pattern-match.
    """

    admitted: bool
    peer: str | None = None
    reason: AdmissionReason | None = None
    detail: str = ""
    deprecated_path: bool = False
    successor_path: str | None = None
    declared_skills: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.admitted, bool):
            raise ValueError(f"AdmissionResult.admitted must be a bool, got {self.admitted!r}")
        if not self.admitted and self.reason is None:
            raise ValueError("AdmissionResult.reason is required when admitted is False")
        if self.admitted and self.reason is not None:
            raise ValueError("AdmissionResult.reason must be None when admitted is True")
        if self.reason is not None and not isinstance(self.reason, AdmissionReason):
            raise ValueError(f"AdmissionResult.reason must be an AdmissionReason, got {self.reason!r}")
        if self.deprecated_path and self.successor_path is None:
            raise ValueError("AdmissionResult.deprecated_path=True requires successor_path")
        if not self.deprecated_path and self.successor_path is not None:
            raise ValueError("AdmissionResult.successor_path is only meaningful when deprecated_path is True")
        if isinstance(self.declared_skills, (str, bytes)):
            raise ValueError("AdmissionResult.declared_skills must be an iterable of skill names, not a bare str")
        object.__setattr__(self, "declared_skills", tuple(sorted(set(self.declared_skills))))

    def to_dict(self) -> dict:
        return {
            "admitted": self.admitted,
            "peer": self.peer,
            "reason": self.reason.value if self.reason is not None else None,
            "detail": self.detail,
            "deprecated_path": self.deprecated_path,
            "successor_path": self.successor_path,
            "declared_skills": list(self.declared_skills),
        }

    def as_tool_error(self) -> dict:
        """Convenience for a gateway that wants to deny a `ToolCall` in
        `kit/mcp/errors.py`'s shape: every admission denial maps onto the
        MCP taxonomy's `unauthorized` code (CONTRACTS.md 3.3: "scope/act
        mismatch") — an admission failure IS a scope/identity mismatch,
        whichever of the nine reasons produced it. Degrades to a plain
        dict of the same shape if kit/mcp/errors.py is not importable."""
        if self.admitted:
            raise ValueError("as_tool_error() called on an admitted AdmissionResult")
        assert self.reason is not None  # guaranteed by __post_init__
        if _HAS_ERRORS:
            return make_error(ErrorCode.UNAUTHORIZED, admission_reason=self.reason.value)
        return {"code": "unauthorized", "admission_reason": self.reason.value}


# ===========================================================================
# AgentCard — the A2A discovery document.
# ===========================================================================

AGENT_CARD_PATH = "/.well-known/agent-card.json"  # v0.3+ — the current path
AGENT_JSON_PATH_DEPRECATED = "/.well-known/agent.json"  # pre-v0.3 — still served, deprecated
WELL_KNOWN_PATHS: tuple[str, ...] = (AGENT_CARD_PATH, AGENT_JSON_PATH_DEPRECATED)


def resolve_well_known_path(available: Iterable[str]) -> tuple[str | None, bool]:
    """Given the `/.well-known/...` paths a peer actually answers on,
    choose which one discovery should use. Prefers :data:`AGENT_CARD_PATH`
    (this task's brief: "support both, prefer the new one, and mark the old
    path deprecated" — CORPUS-FACTS.md contradiction #8: the canonical deck
    still says `agent.json`, the working deck already has the v0.3 rename).

    Returns `(chosen_path, deprecated)`; `(None, False)` if the peer offers
    neither — discovery has nothing to fetch.
    """
    offered = set(available)
    if AGENT_CARD_PATH in offered:
        return AGENT_CARD_PATH, False
    if AGENT_JSON_PATH_DEPRECATED in offered:
        return AGENT_JSON_PATH_DEPRECATED, True
    return None, False


@dataclass(frozen=True, slots=True)
class AgentCard:
    """One peer's Agent Card — the 2026-era A2A discovery document.

    `skills` is always sorted+deduped on construction (mirrors
    `kit/mcp/types.py`'s `ToolCall.fields` canonicalisation), so two cards
    declaring the same capability set in a different order compare equal
    and — because signing operates on `canonical_bytes()`, not on
    whatever order a caller happened to list skills in — sign identically.
    """

    name: str
    url: str
    version: str
    skills: tuple[str, ...]
    description: str = ""
    signature: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _PEER_NAME_RE.match(self.name):
            raise ValueError(f"AgentCard.name must match {_PEER_NAME_RE.pattern!r}, got {self.name!r}")
        if not isinstance(self.url, str) or not self.url:
            raise ValueError(f"AgentCard.url must be a non-empty str, got {self.url!r}")
        if not isinstance(self.version, str) or not self.version:
            raise ValueError(f"AgentCard.version must be a non-empty str, got {self.version!r}")
        if isinstance(self.skills, (str, bytes)):
            raise ValueError("AgentCard.skills must be an iterable of skill-name strings, not a bare str")
        skills_t = tuple(sorted(set(self.skills)))
        for s in skills_t:
            if not isinstance(s, str) or not _SKILL_NAME_RE.match(s):
                raise ValueError(f"AgentCard skill name {s!r} must match {_SKILL_NAME_RE.pattern!r}")
        if not skills_t:
            raise ValueError("AgentCard.skills must declare at least one skill")
        object.__setattr__(self, "skills", skills_t)
        if not isinstance(self.description, str):
            raise ValueError(f"AgentCard.description must be a str, got {self.description!r}")
        if self.signature is not None and not isinstance(self.signature, str):
            raise ValueError(f"AgentCard.signature must be a str or None, got {self.signature!r}")

    def canonical_bytes(self) -> bytes:
        """Deterministic JSON of every field EXCEPT `signature` itself —
        what gets signed, and what `verify_card()` recomputes and compares
        against."""
        return _canonical_json(
            {
                "name": self.name,
                "url": self.url,
                "version": self.version,
                "skills": list(self.skills),
                "description": self.description,
            }
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "url": self.url,
            "version": self.version,
            "skills": list(self.skills),
            "description": self.description,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> "AgentCard":
        """Inverse of :meth:`to_dict`. Required keys are read with `d[...]`
        (raises `KeyError`, not a silent default) — the same discipline
        `kit/world/page.py`'s `Page.from_json` uses for fields a card
        cannot legitimately be missing."""
        return cls(
            name=d["name"],
            url=d["url"],
            version=d["version"],
            skills=tuple(d.get("skills", ())),
            description=d.get("description", ""),
            signature=d.get("signature"),
        )


def issue_card(name: str, url: str, version: str, skills: Iterable[str], *, description: str = "") -> AgentCard:
    """Mint a REGISTRY-SIGNED :class:`AgentCard` — what a legitimate peer's
    well-known document genuinely serves. This is the one function in this
    module allowed to produce a card whose signature will verify;
    everything else (a mutated, forged, or hand-built dict a gateway
    receives over the wire) goes through :func:`verify_card` to find out
    whether it matches what this function would have produced for the same
    content."""
    unsigned = AgentCard(name=name, url=url, version=version, skills=tuple(skills), description=description)
    signature = _hmac_hex("agent-card", unsigned.canonical_bytes())
    return AgentCard(
        name=unsigned.name,
        url=unsigned.url,
        version=unsigned.version,
        skills=unsigned.skills,
        description=unsigned.description,
        signature=signature,
    )


def verify_card(card: AgentCard | Mapping[str, object], *, discovery_path: str | None = None) -> AdmissionResult:
    """Admit or reject a peer's Agent Card so a gateway can act on the
    result. Accepts either a real :class:`AgentCard` or the raw dict a
    network fetch would hand back (a `forge_card` mutation — CONTRACTS.md
    section 8's closed mutation-op set — produces exactly this shape), so
    a caller never needs to know which one it received before checking it.

    Checks, in order:
      1. structurally well-formed -> :attr:`AdmissionReason.MALFORMED_CARD`
      2. `name` is one of the three known peers -> `UNKNOWN_PEER`
      3. `signature` matches what :func:`issue_card` would have produced
         for this exact content -> `FORGED_CARD_SIGNATURE`

    `discovery_path`, when given, is checked against :data:`WELL_KNOWN_PATHS`:
    the new `agent-card.json` path admits clean; the deprecated `agent.json`
    path still admits (both are real per CORPUS-FACTS.md contradiction #8)
    but `deprecated_path`/`successor_path` are set, so a gateway can treat
    it as the `wasteful`-style hygiene signal CONTRACTS.md 4.2 mechanic 8
    gives every other deprecated tool.
    """
    try:
        parsed = card if isinstance(card, AgentCard) else AgentCard.from_dict(card)
    except (ValueError, KeyError, TypeError) as exc:
        return AdmissionResult(admitted=False, peer=None, reason=AdmissionReason.MALFORMED_CARD, detail=str(exc))

    if parsed.name not in KNOWN_PEERS:
        return AdmissionResult(
            admitted=False,
            peer=parsed.name,
            reason=AdmissionReason.UNKNOWN_PEER,
            detail=f"{parsed.name!r} is not one of {sorted(KNOWN_PEERS)}",
        )

    expected_sig = _hmac_hex("agent-card", parsed.canonical_bytes())
    if parsed.signature is None or not hmac.compare_digest(parsed.signature, expected_sig):
        return AdmissionResult(
            admitted=False,
            peer=parsed.name,
            reason=AdmissionReason.FORGED_CARD_SIGNATURE,
            detail="signature does not match the registry-issued card for this content",
        )

    deprecated_path = False
    successor_path: str | None = None
    if discovery_path is not None:
        if discovery_path == AGENT_JSON_PATH_DEPRECATED:
            deprecated_path = True
            successor_path = AGENT_CARD_PATH
        elif discovery_path != AGENT_CARD_PATH:
            return AdmissionResult(
                admitted=False,
                peer=parsed.name,
                reason=AdmissionReason.MALFORMED_CARD,
                detail=f"discovery_path {discovery_path!r} is neither of {WELL_KNOWN_PATHS}",
            )

    return AdmissionResult(
        admitted=True,
        peer=parsed.name,
        deprecated_path=deprecated_path,
        successor_path=successor_path,
        declared_skills=parsed.skills,
    )


def admit_skill(
    card: AgentCard | Mapping[str, object], skill: str, *, discovery_path: str | None = None
) -> AdmissionResult:
    """The other half of the admission surface: "a call to an UNDECLARED
    skill must be refusable by an admission check." Verifies the card
    first (a malformed/forged/unknown-peer denial propagates unchanged),
    then checks `skill` against the card's own declared `skills` — so a
    gateway can call this alone as the one-stop admission gate for a
    single A2A call, without separately invoking :func:`verify_card`.
    """
    verified = verify_card(card, discovery_path=discovery_path)
    if not verified.admitted:
        return verified
    if skill not in verified.declared_skills:
        return AdmissionResult(
            admitted=False,
            peer=verified.peer,
            reason=AdmissionReason.UNDECLARED_SKILL,
            detail=f"{skill!r} is not in this card's declared skills {verified.declared_skills}",
            declared_skills=verified.declared_skills,
        )
    return verified


# ===========================================================================
# The three real peers.
# ===========================================================================

# RESOLVED AMBIGUITY (module docstring, point 2): roster's declared skills
# per this task's brief (`role_of`, `who_enrolled`) both route to the single
# wire tool kit/mcp/specs.py prices for roster (`lookup_learner`).
# curriculum-analyst and citation-checker need no such bridge — their card
# skill name already equals their specs.py tool name.
SKILL_ROUTES: Mapping[str, tuple[str, str]] = {
    "which_days_cover": ("curriculum-analyst", "which_days_cover"),
    "verify_source": ("citation-checker", "verify_source"),
    "role_of": ("roster", "lookup_learner"),
    "who_enrolled": ("roster", "lookup_learner"),
}

AGENT_CARDS: Mapping[str, AgentCard] = {
    "curriculum-analyst": issue_card(
        "curriculum-analyst",
        "a2a://curriculum-analyst",
        "0.3",
        ["which_days_cover"],
        description=(
            "broad curriculum lookups across all 27 lectures — "
            "UNVERIFIED: the faithless-peer surface (FINAL-PLAN.md section 9)"
        ),
    ),
    "citation-checker": issue_card(
        "citation-checker",
        "a2a://citation-checker",
        "0.3",
        ["verify_source"],
        description="the only independent defence against a poisoned citation, rate-limited 2 per 3 rounds",
    ),
    "roster": issue_card(
        "roster",
        "a2a://roster",
        "0.3",
        ["role_of", "who_enrolled"],
        description="the identity boundary — role_of / who_enrolled, wire-routed through lookup_learner",
    ),
}


def _cross_check_against_specs() -> None:
    """At import time (mirroring `kit/mcp/specs.py`'s own
    `_validate_specs(TOOL_SPECS)` call), assert this module's peer/skill
    surface agrees with the collaborator cost table whenever it is
    importable — so a retune of either file that breaks the bridge fails
    loudly here, not silently at duel time. A no-op when specs.py is not
    yet available (hard rule 2)."""
    if not _HAS_SPECS:
        return
    if KNOWN_PEERS != frozenset(A2A_PEERS):
        raise AssertionError(f"KNOWN_PEERS {sorted(KNOWN_PEERS)} != kit.mcp.specs.A2A_PEERS {sorted(A2A_PEERS)}")
    for skill, (server, tool) in SKILL_ROUTES.items():
        if (server, tool) not in TOOL_SPECS:
            raise AssertionError(f"SKILL_ROUTES[{skill!r}] -> {(server, tool)} has no TOOL_SPECS entry")
    for peer in AGENT_CARDS:
        if peer not in A2A_PEERS:
            raise AssertionError(f"AGENT_CARDS names {peer!r}, not in kit.mcp.specs.A2A_PEERS")


_cross_check_against_specs()


# ===========================================================================
# DelegationToken — per-hop act / sub / aud, authority from act alone.
# ===========================================================================


def _delegation_payload(
    token_id: str, act: str, sub: str, aud: str, ttl: int, minted_at_call_index: int
) -> dict:
    return {
        "token_id": token_id,
        "act": act,
        "sub": sub,
        "aud": aud,
        "ttl": ttl,
        "minted_at_call_index": minted_at_call_index,
    }


@dataclass(frozen=True, slots=True)
class DelegationToken:
    """A per-hop delegation token: carries `act` / `sub` / `aud`.

    **Authority derives from `act` — whom you serve — never from `sub` —
    what you are.** A gateway that reads `sub` to decide what a call is
    allowed to do has already lost; that is exactly the bug
    :func:`verify_delegation`'s `act_escalation` check exists to catch
    even when `sub` looks perfectly legitimate.

    `token_id` and `signature` are both pure functions of the other
    fields — no wall-clock, no unseeded randomness — so minting the same
    `(act, sub, aud, ttl, minted_at_call_index, nonce)` twice always
    produces a byte-identical token, and a `FrozenBroker` replay is exact
    (CONTRACTS.md section 11, G-REPRO).
    """

    token_id: str
    act: str
    sub: str
    aud: str
    ttl: int
    minted_at_call_index: int
    signature: str

    def __post_init__(self) -> None:
        if not isinstance(self.token_id, str) or not self.token_id:
            raise ValueError(f"DelegationToken.token_id must be a non-empty str, got {self.token_id!r}")
        if not isinstance(self.act, str) or not _ACT_RE.match(self.act):
            raise ValueError(f"DelegationToken.act must match {_ACT_RE.pattern!r}, got {self.act!r}")
        if not isinstance(self.sub, str) or not _SUB_RE.match(self.sub):
            raise ValueError(f"DelegationToken.sub must match {_SUB_RE.pattern!r}, got {self.sub!r}")
        if not isinstance(self.aud, str) or not _AUD_RE.match(self.aud):
            raise ValueError(f"DelegationToken.aud must match {_AUD_RE.pattern!r}, got {self.aud!r}")
        if not isinstance(self.ttl, int) or isinstance(self.ttl, bool) or self.ttl < 0:
            raise ValueError(f"DelegationToken.ttl must be a non-negative int, got {self.ttl!r}")
        if (
            not isinstance(self.minted_at_call_index, int)
            or isinstance(self.minted_at_call_index, bool)
            or self.minted_at_call_index < 0
        ):
            raise ValueError(
                f"DelegationToken.minted_at_call_index must be a non-negative int, "
                f"got {self.minted_at_call_index!r}"
            )
        if not isinstance(self.signature, str) or not self.signature:
            raise ValueError("DelegationToken.signature must be a non-empty str")

    def canonical_bytes(self) -> bytes:
        return _canonical_json(
            _delegation_payload(self.token_id, self.act, self.sub, self.aud, self.ttl, self.minted_at_call_index)
        )

    def is_expired(self, call_index: int) -> bool:
        """CONTRACTS.md 4.2 mechanic 2's lease pattern, applied to
        delegation: a token minted at `minted_at_call_index` stays valid
        through `minted_at_call_index + ttl` inclusive. `call_index` is
        never wall-clock, so this is safe to call from scored code."""
        return (call_index - self.minted_at_call_index) > self.ttl

    def to_dict(self) -> dict:
        return {
            "token_id": self.token_id,
            "act": self.act,
            "sub": self.sub,
            "aud": self.aud,
            "ttl": self.ttl,
            "minted_at_call_index": self.minted_at_call_index,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> "DelegationToken":
        return cls(
            token_id=d["token_id"],
            act=d["act"],
            sub=d["sub"],
            aud=d["aud"],
            ttl=d["ttl"],
            minted_at_call_index=d["minted_at_call_index"],
            signature=d["signature"],
        )


def mint_delegation(
    act: str,
    aud: str,
    ttl: int,
    *,
    sub: str = "agent:student",
    call_index: int = 0,
    nonce: int = 0,
) -> DelegationToken:
    """Mint a REGISTRY-SIGNED per-hop delegation token.

    `sub` / `call_index` / `nonce` are keyword-only extensions this module
    needs to make minting deterministic and hop-aware without breaking the
    three-positional `(act, aud, ttl)` call shape. `nonce` only matters if
    a caller mints two DIFFERENT tokens for the exact same
    `(act, sub, aud, ttl, call_index)` — rare within one exchange, but
    `token_id` must still never collide, so a caller who needs a second
    token at the same `call_index` bumps `nonce` explicitly rather than
    this function reaching for `time.time()` / `random` (hard rule 4).
    """
    id_payload = f"{act}|{sub}|{aud}|{ttl}|{call_index}|{nonce}".encode("utf-8")
    token_id = "dlg:" + hashlib.sha256(id_payload).hexdigest()[:16]
    canonical = _canonical_json(_delegation_payload(token_id, act, sub, aud, ttl, call_index))
    signature = _hmac_hex("delegation", canonical)
    return DelegationToken(
        token_id=token_id,
        act=act,
        sub=sub,
        aud=aud,
        ttl=ttl,
        minted_at_call_index=call_index,
        signature=signature,
    )


def verify_delegation(
    token: DelegationToken | Mapping[str, object],
    *,
    aud: str,
    call_index: int,
    expected_act: str,
    seen_token_ids: Iterable[str] = (),
) -> AdmissionResult:
    """Admit or reject one hop's delegation token — the "identity" duel
    class (CONTRACTS.md section 8 / FINAL-PLAN.md section 4.4): "A
    delegation whose `aud` does not match the server actually called is the
    'identity' attack class."

    Checks, in order — each maps onto exactly one named failure mode:
      1. signature verifies against the registry secret -> else
         `FORGED_TOKEN_SIGNATURE` (a card-forgery-style attack, applied to
         a token instead of an Agent Card; a structurally unparseable
         token dict is folded into this same reason — either way, it is
         not a token this gate can trust)
      2. `token.aud == aud` (the server THIS hop is actually calling) ->
         else `AUD_MISMATCH`
      3. not expired (`call_index <= minted_at_call_index + ttl`) -> else
         `EXPIRED`
      4. `token.token_id not in seen_token_ids` -> else `REPLAYED_TOKEN`
      5. `token.act == expected_act` — the authority check. **`sub` never
         participates here.** A token whose `sub` is a perfectly
         legitimate agent but whose `act` has been swapped (CONTRACTS.md
         section 8's `replace_act` mutation op — the sample card there does
         exactly this to `curriculum-analyst`) is `ACT_ESCALATION`, full
         stop, regardless of what `sub` says.

    `expected_act` is the caller's own ground truth — normally
    `GatewayContext.act` (CONTRACTS.md section 4.2: "WHOM YOU SERVE.
    Authority derives from this.") — never something read back out of the
    token itself, or this check would be circular and catch nothing.
    """
    try:
        parsed = token if isinstance(token, DelegationToken) else DelegationToken.from_dict(token)
    except (ValueError, KeyError, TypeError) as exc:
        return AdmissionResult(
            admitted=False,
            peer=_peer_name_from_aud(aud),
            reason=AdmissionReason.FORGED_TOKEN_SIGNATURE,
            detail=f"malformed token: {exc}",
        )

    expected_sig = _hmac_hex(
        "delegation",
        _canonical_json(
            _delegation_payload(
                parsed.token_id, parsed.act, parsed.sub, parsed.aud, parsed.ttl, parsed.minted_at_call_index
            )
        ),
    )
    if not hmac.compare_digest(parsed.signature, expected_sig):
        return AdmissionResult(
            admitted=False,
            peer=_peer_name_from_aud(aud),
            reason=AdmissionReason.FORGED_TOKEN_SIGNATURE,
            detail="signature does not match the registry-issued token for this content",
        )

    if parsed.aud != aud:
        return AdmissionResult(
            admitted=False,
            peer=_peer_name_from_aud(aud),
            reason=AdmissionReason.AUD_MISMATCH,
            detail=f"token aud {parsed.aud!r} != server actually called {aud!r}",
        )

    if parsed.is_expired(call_index):
        return AdmissionResult(
            admitted=False,
            peer=_peer_name_from_aud(aud),
            reason=AdmissionReason.EXPIRED,
            detail=(
                f"call_index {call_index} exceeds ttl window "
                f"[{parsed.minted_at_call_index}, {parsed.minted_at_call_index + parsed.ttl}]"
            ),
        )

    if parsed.token_id in set(seen_token_ids):
        return AdmissionResult(
            admitted=False,
            peer=_peer_name_from_aud(aud),
            reason=AdmissionReason.REPLAYED_TOKEN,
            detail=f"token_id {parsed.token_id!r} already used this duel",
        )

    if parsed.act != expected_act:
        return AdmissionResult(
            admitted=False,
            peer=_peer_name_from_aud(aud),
            reason=AdmissionReason.ACT_ESCALATION,
            detail=(
                f"token act {parsed.act!r} != authenticated act {expected_act!r} "
                f"(sub={parsed.sub!r} is never authority)"
            ),
        )

    return AdmissionResult(admitted=True, peer=_peer_name_from_aud(aud))


# ===========================================================================
# TraceContext — W3C `traceparent` propagation.
# ===========================================================================

_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-(?P<parent_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)


@dataclass(frozen=True, slots=True)
class TraceContext:
    """A parsed W3C `traceparent` header: `version-trace_id-parent_id-flags`
    (`00-<32hex>-<16hex>-<2hex>`). This module only ever mints
    `version="00"`."""

    version: str
    trace_id: str
    parent_id: str
    flags: str

    def __post_init__(self) -> None:
        for name, value, width in (
            ("version", self.version, 2),
            ("trace_id", self.trace_id, 32),
            ("parent_id", self.parent_id, 16),
            ("flags", self.flags, 2),
        ):
            if not isinstance(value, str) or len(value) != width or not re.fullmatch(r"[0-9a-f]+", value):
                raise ValueError(f"TraceContext.{name} must be {width} lowercase hex chars, got {value!r}")
        if self.trace_id == "0" * 32:
            raise ValueError("TraceContext.trace_id must not be all-zero (W3C Trace Context)")
        if self.parent_id == "0" * 16:
            raise ValueError("TraceContext.parent_id must not be all-zero (W3C Trace Context)")

    def to_header(self) -> str:
        return f"{self.version}-{self.trace_id}-{self.parent_id}-{self.flags}"

    def child(self, hop_seed: str) -> "TraceContext":
        """Propagate: SAME `trace_id` (the whole exchange stays one
        trace), a NEW `parent_id` deterministically derived from this
        context plus `hop_seed` (e.g. `"curriculum-analyst:call_index=2"`)
        — never `os.urandom`/`random`, so replaying an exchange through
        `FrozenBroker` reproduces byte-identical trace ids at every hop."""
        digest = hashlib.sha256(f"{self.trace_id}|{self.parent_id}|{hop_seed}".encode("utf-8")).hexdigest()
        return TraceContext(version=self.version, trace_id=self.trace_id, parent_id=digest[:16], flags=self.flags)


def parse_traceparent(value: str) -> TraceContext:
    """Strict parse of a `traceparent` header string."""
    m = _TRACEPARENT_RE.match(value.strip())
    if not m:
        raise ValueError(f"malformed traceparent header: {value!r}")
    return TraceContext(**m.groupdict())


def new_traceparent(seed: str, *, sampled: bool = True) -> TraceContext:
    """Mint a FRESH root :class:`TraceContext`, deterministically from
    `seed` (e.g. an `exchange_id` like `"d03-r06-A"`) — never
    `os.urandom`/`random`, so the same exchange always starts the same
    trace (hard rule 4)."""
    digest = hashlib.sha256(f"traceparent-root|{seed}".encode("utf-8")).hexdigest()
    return TraceContext(
        version="00", trace_id=digest[:32], parent_id=digest[32:48], flags="01" if sampled else "00"
    )


def propagate_traceparent(parent: TraceContext | str, *, hop_seed: str) -> TraceContext:
    """Module-level convenience for "traceparent propagation": accepts
    either a parsed :class:`TraceContext` or a raw header string, always
    returns the next hop's context."""
    ctx = parent if isinstance(parent, TraceContext) else parse_traceparent(parent)
    return ctx.child(hop_seed)


# ===========================================================================
# execute() — THE admission-gated execution bridge. ENGINE-REPORT.md D-5's
# fix: this is the ONE intended caller of kit.mcp.servers.handle() for the
# three real A2A wire tools in a live duel. Every other function above this
# point already existed; this is what makes their admission decisions
# reachable in play at all.
# ===========================================================================

#: The declared skill this bridge assumes when a caller does not name one
#: explicitly via `call.args["skill"]`. `curriculum-analyst` and
#: `citation-checker` each declare exactly one skill (module docstring,
#: RESOLVED AMBIGUITY 2), so there is only one honest default for them;
#: `roster` declares TWO (`role_of`, `who_enrolled`) that both route to the
#: same wire tool — `role_of` is picked deterministically (alphabetically
#: first of the two) when a caller does not say which framing it meant. A
#: caller that cares about the distinction (e.g. to exercise
#: `AdmissionReason.UNDECLARED_SKILL` against a name NEITHER card declares)
#: passes `call.args["skill"]` explicitly; `execute()` always prefers that
#: over the default when present.
_DEFAULT_SKILL_FOR_TOOL: Mapping[tuple[str, str], str] = {
    ("curriculum-analyst", "which_days_cover"): "which_days_cover",
    ("citation-checker", "verify_source"): "verify_source",
    ("roster", "lookup_learner"): "role_of",
}


def _attempted_cost(call: "ToolCall") -> int:
    """The price of a call that NEVER RAN — what an admission denial
    charges (CONTRACTS.md 3.3: `unauthorized` is "Charged? yes"). `n_rows=0`
    because nothing was ever fetched; the caller still spent the attempt.
    `0` when `kit.mcp.specs` is not importable or does not price this
    `(server, tool)` at all — nothing this module can honestly charge for
    a call it cannot even cost (see `health()`'s `has_specs`)."""
    if not _HAS_SPECS or _spec_cost_of is None or (call.server, call.tool) not in TOOL_SPECS:
        return 0
    return _spec_cost_of(call, 0)


def _denial_dict(reason_result: "AdmissionResult", *, cost: int) -> dict:
    """`AdmissionResult.as_tool_error()`, wrapped in CONTRACTS.md 3.3's
    `{"ok": false, "error": {...}, "cost": n}` envelope. Uses the real
    `ToolResult` for validation when `kit.mcp.types` is importable (it must
    be, to reach this function at all — see `execute()`'s own early-outs);
    kept as a thin wrapper so every denial path in `execute()` builds this
    dict identically."""
    return ToolResult(ok=False, error=reason_result.as_tool_error(), cost=cost).to_dict()


def execute(
    world: Any,
    call: "ToolCall",
    *,
    expected_act: str,
    delegation: "DelegationToken | Mapping[str, object] | None" = None,
    seen_token_ids: Iterable[str] = (),
    discovery_path: str | None = None,
    hardmode: Any = None,
) -> dict:
    """Admit, THEN execute, one A2A `ToolCall` — the fix for ENGINE-REPORT.md
    D-5 ("the entire A2A layer is dead"). Runs the FULL admission surface
    this module already built, in order, EVERY time, for EVERY one of the
    three real A2A wire tools:

      1. `call.server` must be a :data:`KNOWN_PEERS` name, and
         `(call.server, call.tool)` must be a real `kit.mcp.specs.TOOL_SPECS`
         row (else `unknown_peer` / a plain `bad_request` — nothing to admit
         at all).
      2. :func:`admit_skill` against the REGISTRY-SIGNED :data:`AGENT_CARDS`
         entry for that peer — this ALSO runs :func:`verify_card` (its own
         first step), so a `forge_card`-mutated card would be caught here
         too if a future caller ever threaded one through (this module
         always uses the genuine registry card; there is no override
         parameter, deliberately — a caller that wants to exercise a forged
         card calls :func:`verify_card`/:func:`admit_skill` directly, as
         `test_a2a.py` already does).
      3. :func:`verify_delegation` against `expected_act` — the caller's
         OWN authenticated ground truth (CONTRACTS.md 4.2: "normally
         `GatewayContext.act`"), never read back out of anything the wire
         call itself carries. `delegation=None` is treated as "no token
         presented at all" (folds into `FORGED_TOKEN_SIGNATURE`'s malformed-
         token path — there is no dedicated "missing" reason in the closed
         nine-member `AdmissionReason` set, and "no credential" is at least
         as suspect as "unparseable credential").

    A call that fails ANY of the three steps NEVER reaches
    `kit.mcp.servers.handle()` — admission is not best-effort, it is not
    bypassable by construction: every `return` before the final one is a
    denial. Only full admission dispatches to `handle()`, which is what
    actually runs the world-lookup logic (`kit/mcp/servers.py`'s fix, not
    this module's). `roster.lookup_learner`'s OWN identity-boundary check
    (`kit/mcp/servers.py`'s RESOLVED AMBIGUITY 7) is threaded through as
    `handle()`'s `caller_act` keyword — the SAME `expected_act` this
    function already authenticated via `verify_delegation`, so the wire
    tool never has to re-derive or duplicate that check.

    Degrades LOUDLY, never silently, when a hard dependency is missing
    (`health()`'s `has_types`/`has_servers`): returns a structured
    `{"ok": false, "error": {"code": "unavailable"}, "cost": 0}` — the
    closed taxonomy's own "something is wrong, no further detail" shape —
    rather than crash or silently pretend the call succeeded.
    """
    if not _HAS_TYPES:
        return {"ok": False, "error": {"code": "unavailable"}, "cost": 0}

    peer = call.server
    if peer not in KNOWN_PEERS:
        denial = AdmissionResult(
            admitted=False, peer=None, reason=AdmissionReason.UNKNOWN_PEER,
            detail=f"{peer!r} is not a known A2A peer {sorted(KNOWN_PEERS)}",
        )
        return _denial_dict(denial, cost=_attempted_cost(call))

    if not _HAS_SPECS or (call.server, call.tool) not in TOOL_SPECS:
        reason = f"unknown tool {call.server}.{call.tool}"
        error = make_error(ErrorCode.BAD_REQUEST, reason=reason) if _HAS_ERRORS else {"code": "bad_request", "reason": reason}
        return ToolResult(ok=False, error=error, cost=0).to_dict()

    skill = call.args.get("skill") if isinstance(call.args, Mapping) else None
    if not isinstance(skill, str) or not skill:
        skill = _DEFAULT_SKILL_FOR_TOOL.get((call.server, call.tool))
    if skill is None:  # pragma: no cover - every TOOL_SPECS A2A row has a default above
        error = (
            make_error(ErrorCode.BAD_REQUEST, reason=f"no declared A2A skill maps to {call.server}.{call.tool}")
            if _HAS_ERRORS else {"code": "bad_request"}
        )
        return ToolResult(ok=False, error=error, cost=0).to_dict()

    skill_admission = admit_skill(AGENT_CARDS[peer], skill, discovery_path=discovery_path)
    if not skill_admission.admitted:
        return _denial_dict(skill_admission, cost=_attempted_cost(call))

    deleg_admission = verify_delegation(
        delegation if delegation is not None else {},
        aud=f"a2a:{peer}",
        call_index=call.call_index,
        expected_act=expected_act,
        seen_token_ids=seen_token_ids,
    )
    if not deleg_admission.admitted:
        return _denial_dict(deleg_admission, cost=_attempted_cost(call))

    if not _HAS_SERVERS:
        return {"ok": False, "error": {"code": "unavailable"}, "cost": 0}

    caller_act = expected_act if (call.server, call.tool) == ("roster", "lookup_learner") else None
    return _servers_handle(world, call, hardmode=hardmode, caller_act=caller_act)


# ===========================================================================
if __name__ == "__main__":
    print("=== kit.mcp.a2a: three A2A peers + the admission surface ===")
    print(f"  specs.py cross-check available: {_HAS_SPECS}")
    print(f"  errors.py convenience available: {_HAS_ERRORS}\n")

    print("=== the three real Agent Cards ===")
    for peer in sorted(AGENT_CARDS):
        card = AGENT_CARDS[peer]
        print(f"  {peer:20} skills={card.skills}  sig={card.signature[:12]}...")
    assert set(AGENT_CARDS) == KNOWN_PEERS

    print("\n=== resolve_well_known_path() prefers the v0.3 rename ===")
    both = resolve_well_known_path([AGENT_JSON_PATH_DEPRECATED, AGENT_CARD_PATH])
    only_old = resolve_well_known_path([AGENT_JSON_PATH_DEPRECATED])
    neither = resolve_well_known_path([])
    print(f"  both offered      -> {both}")
    print(f"  only agent.json   -> {only_old}")
    print(f"  neither offered   -> {neither}")
    assert both == (AGENT_CARD_PATH, False)
    assert only_old == (AGENT_JSON_PATH_DEPRECATED, True)
    assert neither == (None, False)

    print("\n=== verify_card(): a genuine card admits, both well-known paths ===")
    genuine = AGENT_CARDS["curriculum-analyst"]
    admitted_new = verify_card(genuine, discovery_path=AGENT_CARD_PATH)
    admitted_old = verify_card(genuine, discovery_path=AGENT_JSON_PATH_DEPRECATED)
    print(f"  new path -> {admitted_new.to_dict()}")
    print(f"  old path -> {admitted_old.to_dict()}")
    assert admitted_new.admitted and not admitted_new.deprecated_path
    assert admitted_old.admitted and admitted_old.deprecated_path
    assert admitted_old.successor_path == AGENT_CARD_PATH

    print("\n=== verify_card(): the forge_card mutation class, caught ===")
    forged = AgentCard(
        name="curriculum-analyst",
        url="a2a://curriculum-analyst",
        version="0.3",
        skills=("which_days_cover", "delete_all_learners"),  # attacker inflated the skill set
        signature="deadbeef" * 8,
    )
    forged_result = verify_card(forged)
    print(f"  forged card -> {forged_result.to_dict()}")
    assert not forged_result.admitted and forged_result.reason is AdmissionReason.FORGED_CARD_SIGNATURE

    print("\n=== verify_card(): unknown peer + malformed card ===")
    unknown = verify_card({"name": "imposter-server", "url": "a2a://x", "version": "0.3", "skills": ["x"]})
    print(f"  unknown peer -> reason={unknown.reason}")
    assert unknown.reason is AdmissionReason.UNKNOWN_PEER
    malformed = verify_card({"name": "roster", "url": "a2a://roster"})  # missing version/skills
    print(f"  malformed    -> reason={malformed.reason}")
    assert malformed.reason is AdmissionReason.MALFORMED_CARD

    print("\n=== admit_skill(): declared vs. UNDECLARED (the identity boundary) ===")
    role_ok = admit_skill(AGENT_CARDS["roster"], "role_of")
    enrolled_ok = admit_skill(AGENT_CARDS["roster"], "who_enrolled")
    wire_tool_undeclared = admit_skill(AGENT_CARDS["roster"], "lookup_learner")
    bogus = admit_skill(AGENT_CARDS["curriculum-analyst"], "delete_all_learners")
    print(f"  roster.role_of                  admitted={role_ok.admitted}")
    print(f"  roster.who_enrolled             admitted={enrolled_ok.admitted}")
    print(f"  roster.lookup_learner (the wire tool, not a declared skill) admitted={wire_tool_undeclared.admitted}")
    print(f"  curriculum-analyst.delete_all_learners admitted={bogus.admitted} reason={bogus.reason}")
    assert role_ok.admitted and enrolled_ok.admitted
    assert not wire_tool_undeclared.admitted and wire_tool_undeclared.reason is AdmissionReason.UNDECLARED_SKILL
    assert not bogus.admitted and bogus.reason is AdmissionReason.UNDECLARED_SKILL

    print("\n=== SKILL_ROUTES bridges the card's skill names to specs.py's priced tool ===")
    for skill, route in sorted(SKILL_ROUTES.items()):
        print(f"  {skill:16} -> {route}")
    assert SKILL_ROUTES["role_of"] == SKILL_ROUTES["who_enrolled"] == ("roster", "lookup_learner")

    print("\n=== mint_delegation() / verify_delegation(): the happy path ===")
    legit = mint_delegation("learner:sv-0417", "a2a:curriculum-analyst", ttl=3, sub="agent:vlearn-tutor", call_index=1)
    print(f"  minted: {legit.to_dict()}")
    happy = verify_delegation(legit, aud="a2a:curriculum-analyst", call_index=2, expected_act="learner:sv-0417")
    print(f"  verified at call_index=2 -> {happy.to_dict()}")
    assert happy.admitted

    print("\n=== determinism: minting the SAME token twice is byte-identical (G-REPRO) ===")
    legit_again = mint_delegation(
        "learner:sv-0417", "a2a:curriculum-analyst", ttl=3, sub="agent:vlearn-tutor", call_index=1
    )
    print(f"  token_id match:   {legit.token_id == legit_again.token_id}")
    print(f"  signature match:  {legit.signature == legit_again.signature}")
    assert legit == legit_again

    print("\n=== the five named failure modes ===")

    print("\n  [aud_mismatch] — CONTRACTS.md section 8's 'identity' attack class, verbatim:")
    print("  a token minted for curriculum-analyst, presented to roster instead.")
    wrong_peer = verify_delegation(legit, aud="a2a:roster", call_index=2, expected_act="learner:sv-0417")
    print(f"    -> {wrong_peer.reason}")
    assert wrong_peer.reason is AdmissionReason.AUD_MISMATCH

    print("\n  [act_escalation] — the deck.json replace_act mutation, verbatim:")
    print("  the same legitimate sub, a swapped act. sub never grants authority.")
    escalated = verify_delegation(
        legit, aud="a2a:curriculum-analyst", call_index=2, expected_act="learner:sv-0392"
    )
    print(f"    -> {escalated.reason}")
    assert escalated.reason is AdmissionReason.ACT_ESCALATION

    print("\n  [replayed_token] — the same token_id seen already this duel:")
    replayed = verify_delegation(
        legit,
        aud="a2a:curriculum-analyst",
        call_index=2,
        expected_act="learner:sv-0417",
        seen_token_ids={legit.token_id},
    )
    print(f"    -> {replayed.reason}")
    assert replayed.reason is AdmissionReason.REPLAYED_TOKEN

    print("\n  [expired] — ttl exceeded (the lease pattern, applied to delegation):")
    expired = verify_delegation(legit, aud="a2a:curriculum-analyst", call_index=5, expected_act="learner:sv-0417")
    print(f"    -> {expired.reason}")
    assert expired.reason is AdmissionReason.EXPIRED

    print("\n  [forged_token_signature] — the field was tampered after minting:")
    tampered = legit.to_dict()
    tampered["act"] = "learner:sv-0392"  # signature no longer matches this payload
    forged_token = verify_delegation(
        tampered, aud="a2a:curriculum-analyst", call_index=2, expected_act="learner:sv-0392"
    )
    print(f"    -> {forged_token.reason}  (caught BEFORE act_escalation would even run)")
    assert forged_token.reason is AdmissionReason.FORGED_TOKEN_SIGNATURE

    print("\n  as_tool_error() maps every denial onto kit.mcp.errors' closed taxonomy:")
    print(f"    {wrong_peer.as_tool_error()}")
    assert wrong_peer.as_tool_error()["code"] == "unauthorized"

    print("\n=== traceparent propagation ===")
    root = new_traceparent("d03-r06-A")
    print(f"  root:  {root.to_header()}")
    hop1 = propagate_traceparent(root, hop_seed="curriculum-analyst:call_index=1")
    hop2 = propagate_traceparent(hop1.to_header(), hop_seed="citation-checker:call_index=2")
    print(f"  hop1:  {hop1.to_header()}")
    print(f"  hop2:  {hop2.to_header()}")
    assert hop1.trace_id == hop2.trace_id == root.trace_id
    assert len({root.parent_id, hop1.parent_id, hop2.parent_id}) == 3
    round_tripped = parse_traceparent(hop2.to_header())
    assert round_tripped == hop2
    print(f"  parse(hop2.to_header()) == hop2 -> {round_tripped == hop2}")

    root_again = new_traceparent("d03-r06-A")
    print(f"  determinism: same seed -> same root -> {root_again == root}")
    assert root_again == root

    print("\n  malformed traceparent rejected:")
    try:
        parse_traceparent("not-a-traceparent")
    except ValueError as exc:
        print(f"    -> ValueError: {exc}")
    else:
        raise AssertionError("expected ValueError for a malformed traceparent header")

    # =======================================================================
    # execute() — ENGINE-REPORT.md D-5's fix: admission actually reachable.
    # =======================================================================
    print(f"\n=== execute(): health() -> {health()} ===")
    assert not DEGRADED, f"unexpectedly degraded: {DEGRADED}"

    import tempfile
    from pathlib import Path

    from kit.mcp.types import ToolCall
    from kit.world.fixture import FIXTURE_ASKS, build_fixture_world
    from kit.world.loader import World

    with tempfile.TemporaryDirectory(prefix="colosseum-a2a-") as tmp:
        world = World.load(build_fixture_world(Path(tmp) / "world", include_truth=True))

        print("\n--- which_days_cover: full admission, then a REAL (if faithless) answer ---")
        act = "learner:sv-0417"
        token = mint_delegation(act, "a2a:curriculum-analyst", ttl=3, call_index=0)
        wdc = execute(
            world,
            ToolCall(server="curriculum-analyst", tool="which_days_cover", args={"concept": "Concept:streamable-http"}),
            expected_act=act, delegation=token, seen_token_ids=set(),
        )
        print(f"  admitted + dispatched -> ok={wdc['ok']} row={wdc.get('rows', [None])[0]}")
        assert wdc["ok"] is True
        truth = world.truth(FIXTURE_ASKS["which_day_covers"])
        assert wdc["rows"][0]["anchor"] != truth["anchor"], "the faithless-peer surface must stay reachable AND wrong"
        print(f"  (and it is still confidently WRONG on anchor vs {truth['anchor']!r} — admission gates identity, not correctness)")

        print("\n--- BEFORE this fix: calling handle() directly for an A2A tool returned bad_request: unknown tool ---")
        from kit.mcp.servers import handle as _direct_handle
        direct = _direct_handle(world, ToolCall(server="curriculum-analyst", tool="which_days_cover", args={"concept": "Concept:streamable-http"}))
        print(f"  kit.mcp.servers.handle() directly, no admission at all -> ok={direct['ok']}  (D-5's fix: this now WORKS too — servers.py never checked identity to begin with, see its RESOLVED AMBIGUITY 1/7)")
        assert direct["ok"] is True

        print("\n--- the five named failure modes, end to end through execute() (never just verify_delegation in isolation) ---")

        no_token = execute(
            world, ToolCall(server="citation-checker", tool="verify_source", args={"anchor": "Source:mcp-spec-2026-07-28"}),
            expected_act=act, delegation=None,
        )
        print(f"  no delegation token at all -> ok={no_token['ok']} error={no_token['error']}")
        assert no_token["ok"] is False and no_token["error"]["admission_reason"] == "forged_token_signature"

        wrong_peer_token = mint_delegation(act, "a2a:roster", ttl=3, call_index=0)
        aud_bad = execute(
            world, ToolCall(server="citation-checker", tool="verify_source", args={"anchor": "Source:mcp-spec-2026-07-28"}),
            expected_act=act, delegation=wrong_peer_token,
        )
        print(f"  token minted for roster, presented to citation-checker -> {aud_bad['error']}")
        assert aud_bad["error"]["admission_reason"] == "aud_mismatch"

        escalated_token = mint_delegation(act, "a2a:roster", ttl=3, call_index=0)
        escalated = execute(
            world, ToolCall(server="roster", tool="lookup_learner", args={"learner": "Learner:sv-0417"}),
            expected_act="learner:sv-0392", delegation=escalated_token,  # the token's own act != what the caller claims
        )
        print(f"  token act={act!r}, caller claims expected_act='learner:sv-0392' -> {escalated['error']}")
        assert escalated["error"]["admission_reason"] == "act_escalation"

        replay_token = mint_delegation(act, "a2a:roster", ttl=3, call_index=0)
        first_use = execute(
            world, ToolCall(server="roster", tool="lookup_learner", args={"learner": "Learner:sv-0417"}, call_index=1),
            expected_act=act, delegation=replay_token, seen_token_ids=set(),
        )
        replay = execute(
            world, ToolCall(server="roster", tool="lookup_learner", args={"learner": "Learner:sv-0417"}, call_index=1),
            expected_act=act, delegation=replay_token, seen_token_ids={replay_token.token_id},
        )
        print(f"  first use -> ok={first_use['ok']}   replay (same token_id already seen) -> {replay['error']}")
        assert first_use["ok"] is True and replay["error"]["admission_reason"] == "replayed_token"

        stale_token = mint_delegation(act, "a2a:roster", ttl=1, call_index=0)
        expired = execute(
            world, ToolCall(server="roster", tool="lookup_learner", args={"learner": "Learner:sv-0417"}, call_index=5),
            expected_act=act, delegation=stale_token,
        )
        print(f"  ttl exceeded -> {expired['error']}")
        assert expired["error"]["admission_reason"] == "expired"

        print("\n--- undeclared skill, unknown peer, unknown tool ---")
        undeclared = execute(
            world, ToolCall(server="curriculum-analyst", tool="which_days_cover", args={"skill": "delete_all_learners"}),
            expected_act=act, delegation=mint_delegation(act, "a2a:curriculum-analyst", ttl=3),
        )
        print(f"  args.skill names an undeclared skill -> {undeclared['error']}")
        assert undeclared["error"]["admission_reason"] == "undeclared_skill"

        unknown_peer_call = execute(
            world, ToolCall(server="not-a-real-peer", tool="anything", args={}),
            expected_act=act, delegation=None,
        )
        print(f"  call.server is not a known peer at all -> {unknown_peer_call['error']}")
        assert unknown_peer_call["error"]["admission_reason"] == "unknown_peer"

        print("\n--- cost: an admission denial still charges (CONTRACTS.md 3.3: unauthorized is charged) ---")
        print(f"  aud_mismatch denial cost -> {aud_bad['cost']}  (attempted verify_source, never ran)")
        assert aud_bad["cost"] > 0

        print("\n--- roster.lookup_learner: execute() threads the ALREADY-authenticated act, cross-learner still refused ---")
        good_token = mint_delegation(act, "a2a:roster", ttl=3, call_index=0)
        self_read = execute(
            world, ToolCall(server="roster", tool="lookup_learner", args={"learner": "Learner:sv-0417"}, call_index=1, fields=("*",)),
            expected_act=act, delegation=good_token,
        )
        print(f"  fully admitted, self-read -> ok={self_read['ok']} row={self_read.get('rows', [None])[0]}")
        assert self_read["ok"] is True and self_read["rows"][0]["act"] == act

        cross_token = mint_delegation(act, "a2a:roster", ttl=3, call_index=0)
        cross_read = execute(
            world, ToolCall(server="roster", tool="lookup_learner", args={"learner": "Learner:sv-0392"}, call_index=1),
            expected_act=act, delegation=cross_token,
        )
        print(f"  fully admitted (aud/act/replay/skill all clean), but targets ANOTHER learner -> {cross_read['error']}")
        assert cross_read["ok"] is False and cross_read["error"]["code"] == "unauthorized"
        print("  (admission got the caller in the door; roster.lookup_learner's OWN identity")
        print("   boundary is what refuses it — the two checks are deliberately independent)")

    print("\nAll kit/mcp/a2a.py demos passed.")
