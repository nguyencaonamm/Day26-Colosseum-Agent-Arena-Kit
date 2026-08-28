"""kit/mcp/servers.py — the seven MCP servers, implemented over the World loader.

    slides:   query (field masks, cursors, MINTS LEASES) · search (DEPRECATED
              shim for query) · get_frame (REQUIRES A LIVE LEASE) ·
              whatlinkshere · list_sections
    glossary: define (lang-negotiated, sense-disambiguated -> an MRTR
              input_required round trip when the term is genuinely ambiguous)
              · list_terms (the catalog trap)
    research: search · get_citation
    labs:     get_readme · list_tasks
    progress: get_mastery · record_mastery (WRITE)
    content:  flag_stale_slide (WRITE) · file_content_bug (WRITE)
    registry: provenance (the cheapest call, returns etag + replica identity)
              · list_servers (the other catalog trap) · get_card · pin (WRITE)

Every tool returns CONTRACTS.md 3.2's one result shape, always
(``ToolResult.to_dict()``): never raises, ``{"ok": bool, ...}``. Every
success carries ``anchors``. Every write requires ``If-Match`` +
``Idempotency-Key``.

HOW THIS COMPOSES WITH kit/mcp/hardmode.py
===========================================
``kit/mcp/hardmode.py`` (a collaborator's file — it did not exist when this
module's design started, and does now) already implements all eight
FINAL-PLAN.md 4.2 mechanics as a WRAPPING layer around a "raw" tool call:

    err = hardmode.check_before(call)          # -> error dict | None
    if err is not None: return hardmode.deny_result(call, err)
    raw = the_raw_tool(call)                   # what THIS module computes
    result = hardmode.record_after(call, raw)  # -> the FINAL ToolResult

So every handler below computes the "as if hard mode did not exist" raw
result, and :func:`handle` — the one entry point this module exports —
brackets it with ``hardmode.check_before``/``record_after`` for exactly the
``(server, tool)`` pairs ``kit.mcp.specs.TOOL_SPECS`` (and therefore
``hardmode.py`` itself, which looks tools up in that same table) actually
knows about. A handler never needs to know whether hardmode is present
EXCEPT for three spots where a self-contained fallback would actively
CONFLICT with hardmode's own state if both ran at once — see
``trust_caller`` below.

RESOLVED AMBIGUITY 1 — this task's 19 tool names vs. ``kit.mcp.specs.TOOL_SPECS``
(UPDATED — the fix for ENGINE-REPORT.md D-5/D-6). ``TOOL_SPECS`` prices 15
``(server, tool)`` pairs; this module now dispatches every one of them,
plus 9 local-only extensions ``TOOL_SPECS`` never prices at all:
``slides.list_sections``, ``research.search``, ``research.get_citation``,
``labs.get_readme``, ``labs.list_tasks``, ``progress.get_mastery``,
``content.file_content_bug``, ``registry.get_card``, ``registry.pin`` —
built from the same imported :class:`kit.mcp.specs.ToolSpec` class via
:data:`_LOCAL_TOOL_SPECS` (so the same validation applies), and therefore
never wrapped by ``hardmode.check_before``/``record_after`` (it only
recognises ``TOOL_SPECS`` keys) — :func:`handle` runs them "raw" always.
This is a deliberate, deployment-tested design (``test_hardmode_never_wraps_
local_only_tools``), not a gap: these 9 stay outside the priced/RPC-allowed
economy on purpose (D-6's *other* direction — "every implemented tool must
be priced" — is not attempted here: ``kit/mcp/specs.py`` is a collaborator's
file outside this task's assigned files, and re-pricing 9 tools that were
deliberately scoped out is not this fix's job; flagged in the task report).

Previously, 5 of the 15 ``TOOL_SPECS`` entries had NO executor at all —
``curriculum-analyst.which_days_cover``, ``citation-checker.verify_source``,
``roster.lookup_learner``, ``research.cite_source``, ``labs.get_exercise`` —
so calling any of them returned ``bad_request: unknown tool`` no matter how
correctly a caller admitted itself first (ENGINE-REPORT.md D-5: "the entire
A2A layer is dead"). All 5 now have real raw handlers below, registered in
:data:`_HANDLERS` exactly like every other tool, over the SAME built ``World``
artifact every other handler reads. ``research.cite_source``/
``labs.get_exercise`` are plain MCP tools (``research``/``labs`` are MCP
servers, not A2A peers) — nothing more than an ordinary new row each.

The three genuine A2A peer tools are architecturally different: this module
still never checks *who is allowed to ask* for them (see the "Servers never
emit ``unauthorized``" paragraph below — the ONE deliberate exception is
RESOLVED AMBIGUITY 7). Calling ``handle()`` directly for
``curriculum-analyst``/``citation-checker``/``roster`` now returns REAL data
instead of ``unknown tool`` — that is the D-5 fix — but it is still not the
gated path a live duel is meant to use. ``kit/mcp/a2a.py``'s new
:func:`kit.mcp.a2a.execute` is that gated path: it runs the FULL A2A
admission surface a2a.py already implemented (card verification, declared-
skill check, per-hop ``aud``/delegation-token verification) and only THEN
calls this module's :func:`handle` — exactly mirroring how CONTRACTS.md 4
expects a (not-yet-built) MCP-layer gateway to check act/scope authority
BEFORE any ``ToolCall`` reaches a server. ``a2a.execute()`` never bypasses
this module; it is the one intended caller for those three ``(server, tool)``
keys in a real duel.

RESOLVED AMBIGUITY 2 — ``trust_caller`` (the ONLY per-handler hardmode
awareness). Three of hardmode.py's mechanics keep their OWN authoritative
per-duel state that this module cannot see and must not try to
second-guess:

  * **leases** (mechanic 2) — hardmode mints an opaque ``lse_<8hex>`` id and
    tracks ``{lease_id: minted_at_call_index}`` itself; it does NOT scope a
    lease to the anchors a particular ``search``/``query`` call returned —
    ANY live lease authorises ANY ``get_frame`` within its call-index
    window (read directly off ``HardMode._check_lease`` — simpler than this
    module's own first-draft anchor-scoped design, which is why the local
    fallback below mirrors this exact, simpler rule instead).
  * **write preconditions** (mechanic 3) — a write's ``conflict`` verdict
    depends on whether ITS anchor's etag was ever handed out by a
    ``registry.provenance`` call THROUGH THE SAME ``HardMode`` instance —
    state this module has no visibility into.

When ``hardmode`` is engaged for a tool ``kit.mcp.specs.TOOL_SPECS`` knows
(``handle()``'s local ``covered`` flag), it has ALREADY run
``check_before`` before a handler is ever invoked — re-validating a
hardmode-minted lease id against this module's OWN, differently-shaped
local scheme would misparse a perfectly valid ticket and reject a call
hardmode already approved. So exactly three handlers
(``slides.get_frame``, ``progress.record_mastery``,
``content.flag_stale_slide``) take a ``trust_caller: bool`` flag and SKIP
their self-contained lease/precondition check when it is ``True``. Every
other handler ignores it — minting a lease locally that hardmode will
unconditionally overwrite in ``record_after`` is harmless, not a
correctness bug, so there is nothing to branch on there.

RESOLVED AMBIGUITY 3 — the MRTR ``input_required`` round trip vs.
``kit/mcp/types.py``'s frozen ``ToolResult``. ``ToolResult`` (a
collaborator's file, already built, tested, and not owned by this task) has
no top-level slot for anything named ``input_required`` — its ``ok=False``
branch is EXACTLY ``{"ok", "error", "cost"}``. CONTRACTS.md 3.2/3.3 (the
frozen, binding interface) never mentions ``input_required`` at all — it is
this task's own brief describing the mechanic, not a frozen field name. The
closest compliant realisation is CONTRACTS.md 3.3's own escape hatch:
``bad_request``'s ``error`` dict accepts arbitrary extra keys. So a
disambiguation prompt is ``ToolResult(ok=False, error=make_error(
"bad_request", input_required={"question": ..., "options": [...]}))`` —
nested one level inside ``error``, never a fabricated tenth top-level key.
The caller re-calls ``glossary.define`` with ``args["sense"]`` set to the
chosen ``anchor`` string from ``options`` to complete the round trip. This
composes correctly with hardmode.py's OWN mechanic 7 (the "wrong/missing
lang silently substitutes" trap): ``HardMode.record_after`` returns any
``ok=False`` result UNCHANGED (mechanic 7 only reshapes successes), so an
MRTR prompt this module raises always reaches the caller untouched,
regardless of whether hardmode is engaged.

RESOLVED AMBIGUITY 4 — ``registry.list_servers``/``get_card`` describe what
is actually callable. Their ``capabilities``/tool listings for the 7 MCP
servers are derived from :data:`_HANDLERS` — this module's own dispatch
table — filtered to :data:`_MCP_SERVER_NAMES` so a real MCP server's row
never double-counts a tool. A2A peer rows (``curriculum-analyst``/
``citation-checker``/``roster``) are STILL sourced from
``kit.mcp.specs.A2A_PEERS``/``TOOL_SPECS`` (never from ``_HANDLERS``,
even though ``_HANDLERS`` now legitimately contains their keys too, per
RESOLVED AMBIGUITY 1's D-5 fix) and clearly labelled ``is_peer=True`` — this
keeps the catalog's peer/server split intact even though a single dispatch
table now serves both categories underneath it.

RESOLVED AMBIGUITY 5 — ``progress.record_mastery``'s receipt formula.
``kit/world/fixture.py``'s own ``truth.json`` bakes
``receipt:sha256(learner|concept|"fixture-v1")[:12]`` — and
``"fixture-v1"`` is exactly ``world.manifest["world_id"]`` for the fixture
world, not a coincidence. This module reproduces that EXACT formula, salted
by ``world.manifest["world_id"]`` (never a fixture-specific literal, so it
generalises to the real arena world too) — verified in ``tests/`` against
``world.truth(FIXTURE_ASKS["record_mastery"])["receipt_id"]`` so a
defender's genuinely-correct write can never be scored a spurious
``wrong_answer``. The other three writes (``flag_stale_slide``,
``file_content_bug``, ``pin``) are not part of CONTRACTS.md 7's eight ask
types — no ``truth.json`` entry constrains them — so their receipt formulas
are free local decisions, salted by the caller's own ``Idempotency-Key``
for genuine retry-idempotency.

RESOLVED AMBIGUITY 6 — enumerating "every glossary term" /
"every Section page". ``kit.world.loader.World`` has no public "list every
page in a namespace" or "list every registered term" method — only
``.page(anchor)`` (point lookup), ``.search(q, ns=...)`` (a NON-EMPTY-query
substring scan), ``.terms(term, lang=...)`` (point lookup by exact term),
``.links(anchor)``. ``glossary.list_terms`` (the deliberate "catalog trap":
FINAL-PLAN.md 4.2 mechanic 1's "a bare call is the punishment button")
structurally NEEDS to enumerate every known term with no query string at
all — a design that requires ``args["q"]`` would delete the trap it exists
to teach. This module reaches into ``World._terms`` (an underscore-prefixed
but not otherwise hidden attribute — ``kit/world/loader.py``'s OWN
``__main__`` demo already does the same thing, e.g.
``sorted(world._drift)``/``world._truth``, so this is precedented practice
in this exact codebase, not a boundary violation invented here), guarded by
``try/except AttributeError`` so a future ``World`` shape change degrades
to an empty catalog rather than crashing. Flagged in the task report as a
genuine upstream gap: ``World`` would benefit from a public
``.all_terms()``. ``slides.list_sections`` does NOT have the same
structural need (it is not one of the two tools this task calls out as a
"catalog trap"), so it is implemented honestly against the public API
instead: it requires ``args["q"]`` (or a resolvable ``args["path_id"]`` to
derive one from that deck's title) — the v1 fixture world (`kit/world/
fixture.py`) has no ``Section:`` pages at all, so its own demo/tests below
exercise this tool against a small supplementary world built in this same
file (:func:`build_lab_section_world`), never by editing the shared fixture
(not this task's file to touch).

Servers never emit ``unauthorized`` — CONTRACTS.md 4 places ``act``/scope
authority checking at the GATEWAY layer (``agent/gateway.py``, a student's
file), strictly before a ``ToolCall`` ever reaches a server. These are
"backend" functions; they enforce protocol mechanics (leases, etags, field
masks, closed error codes), never who-is-allowed-to-ask.

RESOLVED AMBIGUITY 7 — the ONE deliberate exception to the paragraph above:
``roster.lookup_learner``. The task brief is explicit that "THE AUTHORITY
CHECK LIVES HERE": ``roster`` is an A2A peer, not an MCP server, and A2A
calls have no student-owned gateway sitting in front of them the way MCP
calls eventually will — ``kit/mcp/a2a.py``'s admission surface (card +
skill + per-hop delegation token) is the closest thing this A2A peer has to
a gateway, and it already authenticates a caller's ``act`` via
``verify_delegation`` before this module ever runs. ``_h_roster_lookup_learner``
receives that ALREADY-AUTHENTICATED value as ``caller_act`` — a keyword
:func:`handle` accepts and threads to exactly this one handler, NEVER read
from ``call.args`` (attacker-controlled wire data an untrusted caller could
set to anything) — and refuses (``unauthorized``) any lookup that does not
resolve to the caller's own ``Learner:`` page, including when ``caller_act``
is absent entirely (fail closed: no authenticated identity, no read, not
even of a call that happens to name a real learner). This is why
``roster.lookup_learner`` is the one row in :data:`_HANDLERS` that can
return ``unauthorized`` — a documented, tested exception to the paragraph
above, not a violation of it.

Stdlib only. No network, no unseeded randomness, no wall-clock.
"""

from __future__ import annotations

import hashlib
import re
import warnings
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from kit.mcp.errors import make_error
from kit.mcp.types import ToolCall, ToolResult
from kit.world.anchor import Anchor
from kit.world.loader import World
from kit.world.page import Page, compute_etag

__all__ = [
    "handle",
    "known_tools",
    "mint_lease",
    "check_lease",
    "build_lab_section_world",
    "health",
    "DEGRADED",
]

# The 7 MCP servers this module knows by name, independent of whichever
# (server, tool) pairs happen to be registered in _HANDLERS at any given
# moment — used to keep A2A peer names (curriculum-analyst/citation-checker/
# roster) out of the "MCP server" branch of registry.list_servers/get_card
# now that _HANDLERS legitimately contains BOTH categories' keys (RESOLVED
# AMBIGUITY 1's D-5 fix). Mirrors kit.mcp.specs.MCP_SERVERS; kept as a local
# literal (not imported) so this module's catalog logic still works even in
# the degraded-specs fallback branch below.
_MCP_SERVER_NAMES: frozenset[str] = frozenset(
    {"slides", "glossary", "research", "labs", "progress", "content", "registry"}
)

# ---------------------------------------------------------------------------
# kit/mcp/specs.py: a collaborator's file. Already complete and imported
# hard by kit/mcp/hardmode.py itself, but this module still degrades
# gracefully per the workspace's hard rule 2 (loader.py / kit/mcp/__init__.py
# set the same precedent) — a minimal local fallback keeps every tool
# priceable even if specs.py is ever missing. NEW WORKSPACE RULE: a fallback
# that cannot be observed is a bug that cannot be found (this is exactly how
# ENGINE-REPORT.md D-3 hid) — so this degrade is now LOUD: a RuntimeWarning
# at import time, plus :data:`DEGRADED`/:func:`health` a gate can assert on.
# ---------------------------------------------------------------------------
try:
    from kit.mcp.specs import (  # type: ignore[import-not-found]
        A2A_PEERS as _SPEC_A2A_PEERS,
        TOOL_SPECS as _SPEC_TOOL_SPECS,
        ToolSpec,
        WRITE_HEADERS as _SPEC_WRITE_HEADERS,
    )

    _HAS_SPECS = True
except (ImportError, AttributeError):  # pragma: no cover - collaborator file
    _HAS_SPECS = False
    _SPEC_TOOL_SPECS = {}
    _SPEC_A2A_PEERS = frozenset()
    _SPEC_WRITE_HEADERS = ("idempotency-key", "if-match")
    warnings.warn(
        "kit.mcp.servers: kit.mcp.specs is not importable — degrading to a "
        "LOCAL ToolSpec fallback (the 15 real TOOL_SPECS-priced tools, "
        "including all 3 A2A peer tools, are untested against the real "
        "cost table and registry.list_servers will show 0 A2A peer rows). "
        "Call kit.mcp.servers.health() to check this at runtime.",
        RuntimeWarning,
        stacklevel=2,
    )

    from dataclasses import dataclass as _dataclass
    from types import MappingProxyType as _MappingProxyType

    @_dataclass(frozen=True, slots=True)
    class ToolSpec:  # type: ignore[no-redef]
        """Minimal fallback matching kit/mcp/specs.py's shape closely
        enough for this module's own use (no cross-file validation, since
        the real file is what performs that)."""

        server: str
        tool: str
        base: int
        field_weight: Mapping[str, int]
        default_fields: tuple[str, ...]
        all_fields: tuple[str, ...]
        row_weight: int = 0
        deprecated: bool = False
        successor: str | None = None
        rate_limit: tuple[int, int] | None = None
        is_write: bool = False
        required_headers: tuple[str, ...] = ()
        needs_lease: bool = False

        def __post_init__(self) -> None:
            object.__setattr__(self, "field_weight", _MappingProxyType(dict(self.field_weight)))

# ---------------------------------------------------------------------------
# kit/mcp/hardmode.py: a collaborator's file. Per the task brief, imported
# best-effort — this module runs every tool "raw" (no lease/precondition/
# rate-limit/partial/lang-trap/deprecation enforcement beyond what each
# handler does itself) when it is absent. Same LOUD-degradation rule as
# specs.py above.
# ---------------------------------------------------------------------------
try:
    from kit.mcp import hardmode as _hardmode_module  # noqa: F401  (import-availability probe only)

    _HAS_HARDMODE = True
except ImportError:  # pragma: no cover - collaborator file
    _HAS_HARDMODE = False
    warnings.warn(
        "kit.mcp.servers: kit.mcp.hardmode is not importable — every tool "
        "runs 'raw' (no lease/precondition/rate-limit/partial/lang-trap/"
        "deprecation enforcement beyond each handler's own self-check; "
        "citation-checker.verify_source's 2-per-3-round rate limit in "
        "particular is enforced ONLY by hardmode and will not fire). "
        "Call kit.mcp.servers.health() to check this at runtime.",
        RuntimeWarning,
        stacklevel=2,
    )

_SPEC_TOOL_KEYS: frozenset[tuple[str, str]] = frozenset(_SPEC_TOOL_SPECS)

# ---------------------------------------------------------------------------
# DEGRADED / health() — the LOUD, assertable witness of the two optional-
# dependency flags above (new workspace rule: "a fallback that cannot be
# observed is a bug that cannot be found" — ENGINE-REPORT.md D-3's root
# cause, generalised). A gate (a test, a CI check, a duel's own preflight)
# can call health() and refuse to proceed on a degraded kit rather than
# silently scoring a duel with citation-checker's rate limit unenforced.
# ---------------------------------------------------------------------------
DEGRADED: tuple[str, ...] = tuple(
    sorted(
        name
        for name, present in (("kit.mcp.specs", _HAS_SPECS), ("kit.mcp.hardmode", _HAS_HARDMODE))
        if not present
    )
)


def health() -> dict:
    """``{"ok": bool, "degraded": (...), "has_specs": bool, "has_hardmode": bool}``.
    ``ok`` is ``False`` iff any optional collaborator module this file can
    degrade without failed to import. Never guess a module's availability
    from whether a call happened to work — call this instead."""
    return {
        "ok": not DEGRADED,
        "degraded": DEGRADED,
        "has_specs": _HAS_SPECS,
        "has_hardmode": _HAS_HARDMODE,
    }


# ===========================================================================
# The 9 tools this task names that kit.mcp.specs.TOOL_SPECS does not price.
# Same ToolSpec class as the real table -> the same validation, the same
# cost formula, the same shape everywhere else in this module.
# ===========================================================================

_LOCAL_TOOL_SPECS: dict[tuple[str, str], ToolSpec] = {
    ("slides", "list_sections"): ToolSpec(
        server="slides",
        tool="list_sections",
        base=1,
        field_weight={"anchor": 0, "body": 2, "title": 1},
        default_fields=("anchor", "title"),
        all_fields=("anchor", "body", "title"),
        row_weight=0,
    ),
    ("research", "search"): ToolSpec(
        server="research",
        tool="search",
        base=1,
        field_weight={"anchor": 0, "host": 1, "snippet": 2, "title": 1, "url": 1},
        default_fields=("anchor", "title", "url"),
        all_fields=("anchor", "host", "snippet", "title", "url"),
        row_weight=0,
    ),
    ("research", "get_citation"): ToolSpec(
        server="research",
        tool="get_citation",
        base=2,
        field_weight={"anchor": 0, "host": 1, "snippet": 2, "title": 1, "url": 1},
        default_fields=("anchor", "url"),
        all_fields=("anchor", "host", "snippet", "title", "url"),
        row_weight=0,
    ),
    ("labs", "get_readme"): ToolSpec(
        server="labs",
        tool="get_readme",
        base=2,
        field_weight={"anchor": 0, "body": 3, "status": 1, "title": 1},
        default_fields=("anchor", "title"),
        all_fields=("anchor", "body", "status", "title"),
        row_weight=0,
    ),
    ("labs", "list_tasks"): ToolSpec(
        server="labs",
        tool="list_tasks",
        base=1,
        field_weight={"anchor": 0, "status": 1, "title": 1},
        default_fields=("anchor", "title"),
        all_fields=("anchor", "status", "title"),
        row_weight=0,
    ),
    ("progress", "get_mastery"): ToolSpec(
        server="progress",
        tool="get_mastery",
        base=1,
        field_weight={"concept": 1, "learner": 0, "summary": 2},
        default_fields=("learner", "summary"),
        all_fields=("concept", "learner", "summary"),
        row_weight=0,
    ),
    ("content", "file_content_bug"): ToolSpec(
        server="content",
        tool="file_content_bug",
        base=3,
        field_weight={"bug_id": 1, "receipt_id": 0},
        default_fields=(),
        all_fields=("bug_id", "receipt_id"),
        row_weight=0,
        is_write=True,
        required_headers=_SPEC_WRITE_HEADERS,
    ),
    ("registry", "get_card"): ToolSpec(
        server="registry",
        tool="get_card",
        base=1,
        field_weight={
            "all_fields": 1,
            "base": 0,
            "default_fields": 1,
            "deprecated": 1,
            "is_write": 1,
            "needs_lease": 1,
            "rate_limit": 1,
            "row_weight": 1,
            "server": 0,
            "successor": 1,
            "tool": 0,
        },
        default_fields=("base", "server", "tool"),
        all_fields=(
            "all_fields", "base", "default_fields", "deprecated", "is_write", "needs_lease",
            "rate_limit", "row_weight", "server", "successor", "tool",
        ),
        row_weight=0,
    ),
    ("registry", "pin"): ToolSpec(
        server="registry",
        tool="pin",
        base=3,
        field_weight={"pinned_anchor": 0, "pinned_etag": 1, "receipt_id": 0},
        default_fields=(),
        all_fields=("pinned_anchor", "pinned_etag", "receipt_id"),
        row_weight=0,
        is_write=True,
        required_headers=_SPEC_WRITE_HEADERS,
    ),
}


def _lookup_spec(server: str, tool: str) -> "ToolSpec | None":
    """`kit.mcp.specs.TOOL_SPECS` first (so pricing for the 10 shared tools
    is byte-identical to what `hardmode.record_after` will independently
    recompute), then :data:`_LOCAL_TOOL_SPECS` for this task's 9 extras."""
    spec = _SPEC_TOOL_SPECS.get((server, tool))
    if spec is not None:
        return spec
    return _LOCAL_TOOL_SPECS.get((server, tool))


# ===========================================================================
# The cost formula (CONTRACTS.md 3.4), reimplemented locally: `cost_of` in
# kit/mcp/specs.py is a public function, but the `_effective_fields`/
# `_cost_from_spec` helpers it is built from are private to that module, and
# this module needs to price both TOOL_SPECS-known AND local-only tools
# through one code path.
# ===========================================================================


def _effective_fields(spec: "ToolSpec", fields: tuple[str, ...]) -> tuple[str, ...]:
    if not fields:
        return spec.default_fields
    if fields == ("*",):
        return spec.all_fields
    return fields


def _cost(spec: "ToolSpec", fields: tuple[str, ...], n_rows: int) -> int:
    effective = _effective_fields(spec, fields)
    total = spec.base
    for f in effective:
        total += spec.field_weight.get(f, 0)
    return total + n_rows * spec.row_weight


def _validate_fields(spec: "ToolSpec", fields: tuple[str, ...]) -> str | None:
    """The unknown-field-in-mask check (CONTRACTS.md 3.3: "malformed args or
    unknown field in mask" -> `bad_request`) — a job of THIS module, never
    hardmode.py's (its `check_before` never inspects `call.fields` at all).
    Returns the first offending field name, or `None` if `fields` is `()`,
    `("*",)`, or every named field is real."""
    if not fields or fields == ("*",):
        return None
    all_set = set(spec.all_fields)
    for f in fields:
        if f not in all_set:
            return f
    return None


def _ok(cost: int, rows: Sequence[Mapping[str, object]], anchors: Sequence[str], **kw: object) -> ToolResult:
    return ToolResult(ok=True, rows=tuple(rows), anchors=tuple(anchors), cost=cost, **kw)


def _err(cost: int, code: str, **extra: object) -> ToolResult:
    return ToolResult(ok=False, error=make_error(code, **extra), cost=cost)


def _mask(extractors: Mapping[str, Callable[[Page], object]], page: Page, fields: tuple[str, ...]) -> dict:
    return {f: extractors[f](page) for f in fields if f in extractors}


def _lower_headers(call: ToolCall) -> dict[str, object]:
    return {str(k).lower(): v for k, v in call.headers.items()}


# ===========================================================================
# The local, stateless lease fallback — used ONLY when a caller of
# slides.get_frame has `trust_caller=False` (hardmode absent, or this
# particular call's tool is not one hardmode.py recognises). Deliberately
# mirrors `HardMode`'s OWN, simpler-than-first-drafted semantics, read
# directly off `kit/mcp/hardmode.py`'s `_check_lease`: a lease is scoped to
# a CALL-INDEX WINDOW only, never to the specific anchors a search/query
# call happened to return — "live for K+1..K+3, dead from K+4", exactly
# `LEASE_SUBSEQUENT_CALLS` there. Self-describing (the mint call_index is
# encoded IN the id) so no module-level mutable state is needed — this
# module has no per-duel lifecycle of its own the way `HardMode` does.
# ===========================================================================

_LEASE_RE = re.compile(r"^lse_(\d{4})$")
_LEASE_SUBSEQUENT_CALLS = 3


def mint_lease(call_index: int) -> str:
    """A self-describing lease token for the local fallback path."""
    return f"lse_{call_index:04d}"


def check_lease(lease_id: str | None, call_index: int) -> str:
    """`"ok"` | `"lease_required"` | `"lease_expired"` for the local
    fallback path (`trust_caller=False`)."""
    if not lease_id:
        return "lease_required"
    m = _LEASE_RE.match(lease_id)
    if not m:
        return "lease_required"
    age = call_index - int(m.group(1))
    if age <= 0:
        return "lease_required"
    if age > _LEASE_SUBSEQUENT_CALLS:
        return "lease_expired"
    return "ok"


def _receipt_id(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"receipt:{digest[:12]}"


def _world_id(world: World) -> str:
    return str(world.manifest.get("world_id", "unknown-world"))


# ===========================================================================
# slides
# ===========================================================================


def _slides_row_extractors(q: str) -> dict[str, Callable[[Page], object]]:
    needle = q.strip().lower()

    def _score(p: Page) -> int:
        haystack = f"{p.title}\n{p.body}".lower()
        return haystack.count(needle)

    return {"title": lambda p: p.title, "body": lambda p: p.body, "score": _score}


_SLIDES_FRAME_EXTRACTORS: dict[str, Callable[[Page], object]] = {
    "body": lambda p: p.body,
    "confidence": lambda p: p.confidence,
    "etag": lambda p: p.etag,
    "extraction_tier": lambda p: p.extraction_tier,
    "lang": lambda p: p.lang,
    "links": lambda p: list(p.links),
    "meta": lambda p: dict(p.meta),
    "status": lambda p: p.status,
    "title": lambda p: p.title,
}

_SECTION_EXTRACTORS: dict[str, Callable[[Page], object]] = {
    "anchor": lambda p: p.anchor,
    "body": lambda p: p.body,
    "title": lambda p: p.title,
}


def _search_like(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], *, deprecated: bool) -> ToolResult:
    q = call.args.get("q")
    if not isinstance(q, str) or not q.strip():
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.q must be a non-empty string")
    ns = call.args.get("ns")
    if ns is not None and not isinstance(ns, str):
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.ns must be a string or omitted")
    limit = call.args.get("limit", 5)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.limit must be a positive int")
    cursor_raw = call.args.get("cursor", "0")
    try:
        offset = int(cursor_raw)
        if offset < 0:
            raise ValueError
    except (TypeError, ValueError):
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.cursor must be a non-negative int string")

    fetch_n = offset + limit + 1  # one extra row to detect "more remain"
    matches = world.search(q, ns=ns, limit=fetch_n)
    page_slice = matches[offset : offset + limit]
    more = len(matches) > offset + limit

    extractors = _slides_row_extractors(q)
    rows = [_mask(extractors, p, fields) for p in page_slice]
    anchors = [p.anchor for p in page_slice]
    cost = _cost(spec, fields, len(rows))
    kw: dict[str, object] = {
        "lease_id": mint_lease(call.call_index),
        "partial": more,
        "continuation": str(offset + limit) if more else None,
    }
    if deprecated:
        kw["deprecated"] = True
        kw["successor"] = "slides.query"
    return _ok(cost, rows, anchors, **kw)


def _h_slides_search(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    return _search_like(world, call, spec, fields, deprecated=True)


def _h_slides_query(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    return _search_like(world, call, spec, fields, deprecated=False)


def _h_slides_get_frame(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    anchor = call.args.get("anchor")
    if not isinstance(anchor, str) or not anchor:
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.anchor must be a non-empty string")

    if not trust_caller:
        status = check_lease(call.lease_id, call.call_index)
        if status == "lease_required":
            return _err(_cost(spec, fields, 0), "lease_required")
        if status == "lease_expired":
            return _err(_cost(spec, fields, 0), "lease_expired")

    page = world.page(anchor)
    if page is None:
        return _err(_cost(spec, fields, 0), "not_found")

    row = _mask(_SLIDES_FRAME_EXTRACTORS, page, fields)
    cost = _cost(spec, fields, 1)
    # Envelope `etag`/`lease_id` stay at their ToolResult defaults (None):
    # CONTRACTS.md 3.2 marks the envelope `etag` "provenance only"
    # (registry.provenance is the sole source of truth an If-Match should
    # ever be built from) and `lease_id` "minted by search/locate; null
    # otherwise" — get_frame CONSUMES a lease, it does not mint one. The
    # page's own etag is still reachable, honestly, as a maskable ROW field
    # (`"etag"` is in `_SLIDES_FRAME_EXTRACTORS`, weight 0 in the real spec).
    return _ok(cost, [row], [page.anchor], replica=page.rev, ttl=30)


def _h_slides_whatlinkshere(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    anchor = call.args.get("anchor")
    if not isinstance(anchor, str) or not anchor:
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.anchor must be a non-empty string")
    targets = sorted(str(a) for a in world.links(anchor))
    row = {f: (targets if f == "targets" else len(targets)) for f in fields}
    cost = _cost(spec, fields, 1)
    grounding = sorted({anchor, *targets})
    return _ok(cost, [row], grounding)


def _h_slides_list_sections(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    q = call.args.get("q")
    path_id_arg = call.args.get("path_id")
    if (q is None or (isinstance(q, str) and not q.strip())) and isinstance(path_id_arg, str) and path_id_arg:
        deck = world.page(f"Deck:{path_id_arg}/w") or world.page(f"Deck:{path_id_arg}/c")
        if deck is not None:
            # A full multi-word title is almost never a verbatim substring
            # of any one Section page's own title/body (`world.search()` is
            # a plain substring match, no tokenisation) — so this tries the
            # deck's own words, longest first (more distinctive, less
            # likely to false-positive), and keeps the first one that
            # actually finds a Section page. Falling through to the full
            # title (which will most likely just find nothing) rather than
            # erroring keeps "this deck genuinely has no indexed Section
            # pages" a legitimate empty-rows answer, not a bad_request.
            candidate_words = sorted({w for w in deck.title.split() if len(w) >= 3}, key=len, reverse=True)
            q = deck.title
            for word in candidate_words:
                if world.search(word, ns="Section", limit=1):
                    q = word
                    break
    if not isinstance(q, str) or not q.strip():
        return _err(
            _cost(spec, fields, 0), "bad_request",
            reason="args.q, or a resolvable args.path_id, is required (kit.world.loader.World.search "
            "has no query-free 'list every Section page' method)",
        )
    limit = call.args.get("limit", 20)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.limit must be a positive int")
    pages = world.search(q, ns="Section", limit=limit)
    rows = [_mask(_SECTION_EXTRACTORS, p, fields) for p in pages]
    anchors = [p.anchor for p in pages]
    return _ok(_cost(spec, fields, len(rows)), rows, anchors)


# ===========================================================================
# glossary
# ===========================================================================


def _glossary_extractors(term: str) -> dict[str, Callable[[Page], object]]:
    return {
        "definition": lambda p: p.body,
        "examples": lambda p: list(p.meta.get("examples", ())),
        "sense": lambda p: p.path_id,
        "source_term": lambda p: term.strip().lower(),
    }


def _mrtr_options(world: World, anchor_strs: Sequence[str]) -> list[dict]:
    options = []
    for a in sorted(anchor_strs):
        page = world.page(a)
        options.append({"anchor": a, "sense": page.path_id if page else None, "lang": page.lang if page else None})
    return options


def _h_glossary_define(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    term = call.args.get("term")
    if not isinstance(term, str) or not term.strip():
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.term must be a non-empty string")
    lang = call.args.get("lang")
    if lang is not None and not isinstance(lang, str):
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.lang must be a string or omitted")
    sense_arg = call.args.get("sense")
    if sense_arg is not None and not isinstance(sense_arg, str):
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.sense must be a string or omitted")

    all_senses = [str(a) for a in world.terms(term)]
    if not all_senses:
        return _err(_cost(spec, fields, 0), "not_found")

    chosen: str | None = None
    if sense_arg is not None:
        # The second half of the MRTR round trip: the caller re-calls with
        # the `anchor` string it was handed in a prior `input_required`.
        if sense_arg not in all_senses:
            return _err(
                _cost(spec, fields, 0), "bad_request",
                reason=f"unknown sense {sense_arg!r} for term {term!r}",
                valid_senses=sorted(all_senses),
            )
        chosen = sense_arg
    elif len(all_senses) == 1:
        chosen = all_senses[0]
    elif lang is not None:
        honest = [str(a) for a in world.terms(term, lang=lang)]
        if len(honest) == 1:
            chosen = honest[0]
        elif len(honest) > 1:
            # An honest lang filter still leaves >1 sense (two senses in the
            # SAME language) — genuinely ambiguous, MRTR, never a silent
            # guess (the "wrong lang" trap below is for when lang picks out
            # NOTHING, not when it picks out too much).
            options = _mrtr_options(world, honest)
            return _err(
                _cost(spec, fields, 0), "bad_request",
                input_required={
                    "question": f"'{term}' co {len(honest)} nghia trong ngon ngu {lang!r} — ban muon nghia nao?",
                    "options": options,
                },
            )
        # else: lang matched nothing -> fall through to the trap below.

    if chosen is None and lang is not None:
        # Mechanic 7's OWN failure mode (FINAL-PLAN.md 4.2 #7): a wrong lang
        # silently returns *some* sense rather than erroring — never an
        # input_required here, that would defeat the trap. When hardmode.py
        # is engaged for this call it will very likely re-derive and
        # possibly re-shape this same substitution itself
        # (`HardMode._negotiate_lang`); this module's own version exists so
        # the tool is still correct standing alone.
        chosen = sorted(all_senses)[0]

    if chosen is None:
        # No sense arg, no single unambiguous sense, no lang at all: the
        # genuinely ambiguous case this task brief calls "the single most
        # distinctive thing in this surface".
        options = _mrtr_options(world, all_senses)
        return _err(
            _cost(spec, fields, 0), "bad_request",
            input_required={
                "question": f"'{term}' co {len(all_senses)} nghia — ban muon nghia nao?",
                "options": options,
            },
        )

    page = world.page(chosen)
    if page is None:
        return _err(_cost(spec, fields, 0), "not_found")
    row = _mask(_glossary_extractors(term), page, fields)
    return _ok(_cost(spec, fields, 1), [row], [page.anchor], ttl=30)


def _all_pages_by_ns(world: World, ns: str) -> tuple[Page, ...]:
    """The same reach-in as :func:`_all_term_keys` below (RESOLVED
    AMBIGUITY 6), for a second reason `world.search()` cannot cover:
    `.search()` matches `title`/`body` text ONLY, never `Page.meta` — so a
    lookup that needs to match a URL living in `meta["url"]`
    (`research.get_citation`'s `args["url"]` path) cannot be answered by a
    text query at all, not even in principle. Sorted, so callers that pick
    `matches[0]` are deterministic regardless of `pages.jsonl` iteration
    order."""
    try:
        anchors = sorted(a for a in world._offsets if a.startswith(f"{ns}:"))  # noqa: SLF001
    except AttributeError:  # pragma: no cover - defensive, World shape changed
        return ()
    pages = (world.page(a) for a in anchors)
    return tuple(p for p in pages if p is not None)


def _all_term_keys(world: World) -> tuple[str, ...]:
    """See RESOLVED AMBIGUITY 6 above: `World` has no public "every
    registered term" accessor. `# noqa`-worthy but precedented in this exact
    codebase (`kit/world/loader.py`'s own `__main__` demo does the same
    reach-in over `_drift`/`_truth`)."""
    try:
        return tuple(sorted(world._terms))  # noqa: SLF001
    except AttributeError:  # pragma: no cover - defensive, World shape changed
        return ()


def _h_glossary_list_terms(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    cursor_raw = call.args.get("cursor", "0")
    try:
        offset = int(cursor_raw)
        if offset < 0:
            raise ValueError
    except (TypeError, ValueError):
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.cursor must be a non-negative int string")
    limit = call.args.get("limit", 20)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.limit must be a positive int")

    keys = _all_term_keys(world)
    page_keys = keys[offset : offset + limit]
    more = len(keys) > offset + limit

    rows = []
    grounding: list[str] = []
    for term in page_keys:
        senses = [str(a) for a in world.terms(term)]
        grounding.extend(senses)
        primary = world.page(senses[0]) if senses else None
        row: dict[str, object] = {}
        for f in fields:
            if f == "term":
                row[f] = term
            elif f == "sense":
                row[f] = sorted(world.page(s).path_id for s in senses if world.page(s) is not None)
            elif f == "definition":
                row[f] = primary.body if primary is not None else ""
            elif f == "aliases":
                # terms.json's shape (CONTRACTS.md section 2: "{term_lower:
                # [anchor,...]}") does not distinguish an alias from a
                # primary sense — not modeled by this fixture, documented
                # rather than guessed at.
                row[f] = []
            elif f == "redirect":
                row[f] = None
        rows.append(row)

    cost = _cost(spec, fields, len(rows))
    return _ok(
        cost, rows, sorted(set(grounding)),
        partial=more, continuation=(str(offset + limit) if more else None),
    )


# ===========================================================================
# research
# ===========================================================================

_RESEARCH_EXTRACTORS: dict[str, Callable[[Page], object]] = {
    "anchor": lambda p: p.anchor,
    "host": lambda p: p.meta.get("host"),
    "snippet": lambda p: p.body[:160],
    "title": lambda p: p.title,
    "url": lambda p: p.meta.get("url"),
}


def _h_research_search(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    q = call.args.get("q")
    if not isinstance(q, str) or not q.strip():
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.q must be a non-empty string")
    limit = call.args.get("limit", 10)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.limit must be a positive int")
    pages = world.search(q, ns="Source", limit=limit)
    rows = [_mask(_RESEARCH_EXTRACTORS, p, fields) for p in pages]
    anchors = [p.anchor for p in pages]
    return _ok(_cost(spec, fields, len(rows)), rows, anchors)


def _h_research_get_citation(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    anchor = call.args.get("anchor")
    if anchor is not None and not isinstance(anchor, str):
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.anchor must be a string")
    page: Page | None = None
    if anchor:
        page = world.page(anchor)
        if page is None or page.ns != "Source":
            return _err(_cost(spec, fields, 0), "not_found")
    else:
        url = call.args.get("url")
        if not isinstance(url, str) or not url.strip():
            return _err(_cost(spec, fields, 0), "bad_request", reason="args.anchor or args.url is required")
        # world.search() cannot answer this: it matches title/body text
        # only, and a citation's URL lives in Page.meta["url"], which
        # .search() never inspects — see _all_pages_by_ns's docstring.
        needle = url.strip().lower()
        hits = [
            p for p in _all_pages_by_ns(world, "Source")
            if needle in str(p.meta.get("url", "")).lower()
        ]
        if not hits:
            return _err(_cost(spec, fields, 0), "not_found")
        page = hits[0]
    row = _mask(_RESEARCH_EXTRACTORS, page, fields)
    return _ok(_cost(spec, fields, 1), [row], [page.anchor], ttl=30)


_CITE_SOURCE_EXTRACTORS: dict[str, Callable[[Page], object]] = {
    "anchor": lambda p: p.anchor,
    "confidence": lambda p: p.confidence,
    "snippet": lambda p: p.body[:160],
    "url": lambda p: p.meta.get("url"),
}


def _h_research_cite_source(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    """research.cite_source (kit.mcp.specs.TOOL_SPECS-priced; ENGINE-REPORT.md
    D-5's fix — this row had no executor at all before). "Returns a RESEARCH
    URL with its anchor" (the task brief): the same Source: resolution
    research.get_citation (this module's sibling LOCAL-only tool) already
    uses — args.anchor for a direct Source: anchor, or args.url for the
    same meta["url"] substring lookup (world.search() cannot answer a URL
    lookup at all; see _all_pages_by_ns's docstring) — over the DIFFERENT
    field surface kit.mcp.specs.TOOL_SPECS actually prices this tool with
    (confidence instead of host/title)."""
    anchor = call.args.get("anchor")
    if anchor is not None and not isinstance(anchor, str):
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.anchor must be a string")
    page: Page | None = None
    if anchor:
        page = world.page(anchor)
        if page is None or page.ns != "Source":
            return _err(_cost(spec, fields, 0), "not_found")
    else:
        url = call.args.get("url")
        if not isinstance(url, str) or not url.strip():
            return _err(_cost(spec, fields, 0), "bad_request", reason="args.anchor or args.url is required")
        needle = url.strip().lower()
        hits = [
            p for p in _all_pages_by_ns(world, "Source")
            if needle in str(p.meta.get("url", "")).lower()
        ]
        if not hits:
            return _err(_cost(spec, fields, 0), "not_found")
        page = hits[0]
    row = _mask(_CITE_SOURCE_EXTRACTORS, page, fields)
    return _ok(_cost(spec, fields, 1), [row], [page.anchor], ttl=30)


# ===========================================================================
# labs
# ===========================================================================

_LABS_EXTRACTORS: dict[str, Callable[[Page], object]] = {
    "anchor": lambda p: p.anchor,
    "title": lambda p: p.title,
    "body": lambda p: p.body,
    "status": lambda p: p.status,
}


def _h_labs_get_readme(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    anchor = call.args.get("anchor")
    if not isinstance(anchor, str) or not anchor:
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.anchor must be a non-empty string")
    page = world.page(anchor)
    if page is None or page.ns != "Lab":
        return _err(_cost(spec, fields, 0), "not_found")
    row = _mask(_LABS_EXTRACTORS, page, fields)
    return _ok(_cost(spec, fields, 1), [row], [page.anchor], ttl=30)


def _h_labs_list_tasks(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    q = call.args.get("q")
    if not isinstance(q, str) or not q.strip():
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.q must be a non-empty string")
    limit = call.args.get("limit", 10)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.limit must be a positive int")
    pages = world.search(q, ns="Lab", limit=limit)
    rows = [_mask(_LABS_EXTRACTORS, p, fields) for p in pages]
    anchors = [p.anchor for p in pages]
    return _ok(_cost(spec, fields, len(rows)), rows, anchors)


def _h_labs_get_exercise(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    """labs.get_exercise (kit.mcp.specs.TOOL_SPECS-priced; ENGINE-REPORT.md
    D-5's fix). "Returns a lab task from the Lab: pages" (the task brief):
    a single-anchor lookup exactly like labs.get_readme (this module's
    sibling LOCAL-only tool), scoped to ns == "Lab" the same way, over the
    DIFFERENT field surface TOOL_SPECS prices this tool with
    (instructions/kc_refs/starter_code/summary, not title/body/status).
    kc_refs/starter_code are not modeled by any fixture world built so far
    (kit/world/fixture.py has no Lab: pages at all; the labsec supplementary
    world this module's own tests build has no kc_refs/starter_code meta
    either) — both default to their empty value rather than KeyError,
    documented here rather than guessed at (the same "not modeled" pattern
    glossary.list_terms already uses for its own aliases/redirect fields)."""
    anchor = call.args.get("anchor")
    if not isinstance(anchor, str) or not anchor:
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.anchor must be a non-empty string")
    page = world.page(anchor)
    if page is None or page.ns != "Lab":
        return _err(_cost(spec, fields, 0), "not_found")
    row: dict[str, object] = {}
    for f in fields:
        if f == "instructions":
            row[f] = page.body
        elif f == "summary":
            row[f] = page.title
        elif f == "kc_refs":
            row[f] = list(page.meta.get("kc_refs", ()))
        elif f == "starter_code":
            row[f] = page.meta.get("starter_code", "")
    return _ok(_cost(spec, fields, 1), [row], [page.anchor], ttl=30)


# ===========================================================================
# progress
# ===========================================================================


def _h_progress_get_mastery(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    learner = call.args.get("learner")
    if not isinstance(learner, str) or not learner:
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.learner must be a non-empty string")
    page = world.page(learner)
    if page is None or page.ns != "Learner":
        return _err(_cost(spec, fields, 0), "not_found")
    concept = call.args.get("concept")
    if concept is not None and not isinstance(concept, str):
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.concept must be a string or omitted")

    row: dict[str, object] = {}
    for f in fields:
        if f == "learner":
            row[f] = page.anchor
        elif f == "concept":
            row[f] = concept
        elif f == "summary":
            # `page.body` ONLY — `page.meta["private_fields"]` (email,
            # phone, grade...) is never read here, by construction, not by
            # a mask omission a future edit could quietly widen.
            row[f] = page.body
    anchors = [page.anchor]
    if isinstance(concept, str) and world.page(concept) is not None:
        anchors.append(concept)
    return _ok(_cost(spec, fields, 1), [row], sorted(set(anchors)))


def _h_progress_record_mastery(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    # RESOLVED AMBIGUITY (found by running the hardmode-engaged demo below):
    # `HardMode._check_precondition`/`record_after` generically key the
    # per-anchor issued-etag cache off `call.args["anchor"]` for EVERY
    # write, matching `flag_stale_slide`/`file_content_bug`/`pin` — but
    # CONTRACTS.md 7's `record_mastery` ask is phrased `{learner, concept}`,
    # with no `anchor` key. `args` is tool-specific wire shape (CONTRACTS.md
    # 3.1), not the ask schema, so this handler accepts BOTH: `anchor` (the
    # Learner page being written) is primary, `learner` is accepted as an
    # alias so a caller following CONTRACTS.md 7's ask-shape naming still
    # works standalone — but a caller that wants hardmode's own precondition
    # tracking to recognise this write's target (so a prior
    # `registry.provenance(anchor=<the learner>)` actually primes the
    # conflict check) MUST send it as `args["anchor"]`.
    learner = call.args.get("anchor") or call.args.get("learner")
    concept = call.args.get("concept")
    if not isinstance(learner, str) or not learner:
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.anchor (or args.learner) must be a non-empty string")
    if not isinstance(concept, str) or not concept:
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.concept must be a non-empty string")

    learner_page = world.page(learner)
    if learner_page is None or learner_page.ns != "Learner":
        return _err(_cost(spec, fields, 0), "not_found")

    if not trust_caller:
        headers = _lower_headers(call)
        if_match = headers.get("if-match")
        idem_key = headers.get("idempotency-key")
        if not if_match or not idem_key:
            return _err(_cost(spec, fields, 0), "precondition_missing")
        if if_match != learner_page.etag:
            return _err(_cost(spec, fields, 0), "conflict")

    mastery_level = call.args.get("mastery_level", "practiced")
    receipt = _receipt_id(learner, concept, _world_id(world))
    row: dict[str, object] = {"receipt_id": receipt}  # unconditional: TOOL_SPECS'
    # own comment calls this "a write's cheapest response is the bare
    # receipt" — receipt_id's field_weight is 0 everywhere it appears, so
    # this never changes cost, only what a caller who asked for nothing
    # still gets back.
    if "mastery_level" in fields:
        row["mastery_level"] = mastery_level

    anchors = [learner]
    if world.page(concept) is not None:
        anchors.append(concept)
    return _ok(_cost(spec, fields, 1), [row], sorted(set(anchors)))


# ===========================================================================
# content
# ===========================================================================


def _h_content_flag_stale_slide(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    anchor = call.args.get("anchor")
    if not isinstance(anchor, str) or not anchor:
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.anchor must be a non-empty string")
    page = world.page(anchor)
    if page is None:
        return _err(_cost(spec, fields, 0), "not_found")

    headers = _lower_headers(call)
    if not trust_caller:
        if_match = headers.get("if-match")
        idem_key = headers.get("idempotency-key")
        if not if_match or not idem_key:
            return _err(_cost(spec, fields, 0), "precondition_missing")
        if if_match != page.etag:
            return _err(_cost(spec, fields, 0), "conflict")

    idem = str(headers.get("idempotency-key", ""))
    receipt = _receipt_id(anchor, "flag_stale", _world_id(world), idem)
    row: dict[str, object] = {"receipt_id": receipt}
    if "prior_status" in fields:
        row["prior_status"] = page.status
    return _ok(_cost(spec, fields, 1), [row], [page.anchor])


def _h_content_file_content_bug(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    anchor = call.args.get("anchor")
    description = call.args.get("description")
    if not isinstance(anchor, str) or not anchor:
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.anchor must be a non-empty string")
    if not isinstance(description, str) or not description.strip():
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.description must be a non-empty string")
    page = world.page(anchor)
    if page is None:
        return _err(_cost(spec, fields, 0), "not_found")

    # Never covered by hardmode.py (a local-only tool) -> this module is
    # ALWAYS the sole precondition enforcer here, unconditionally.
    headers = _lower_headers(call)
    if_match = headers.get("if-match")
    idem_key = headers.get("idempotency-key")
    if not if_match or not idem_key:
        return _err(_cost(spec, fields, 0), "precondition_missing")
    if if_match != page.etag:
        return _err(_cost(spec, fields, 0), "conflict")

    idem = str(idem_key)
    receipt = _receipt_id(anchor, "file_bug", _world_id(world), idem)
    bug_id = "bug:" + hashlib.sha256(f"{anchor}|{description}|{_world_id(world)}|{idem}".encode("utf-8")).hexdigest()[:10]
    row: dict[str, object] = {"receipt_id": receipt}
    if "bug_id" in fields:
        row["bug_id"] = bug_id
    return _ok(_cost(spec, fields, 1), [row], [page.anchor])


# ===========================================================================
# registry
# ===========================================================================


def _h_registry_provenance(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    anchor = call.args.get("anchor")
    if not isinstance(anchor, str) or not anchor:
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.anchor must be a non-empty string")
    page = world.page(anchor)
    if page is None:
        return _err(_cost(spec, fields, 0), "not_found")

    row: dict[str, object] = {}
    for f in fields:
        if f == "etag":
            row[f] = page.etag
        elif f == "rev":
            row[f] = page.rev
        elif f == "last_writer":
            row[f] = "system:worldbuild"  # no write history is modeled by a frozen corpus index
        elif f == "checked_at":
            row[f] = None  # hard rule 4: no wall-clock in scored code; not modeled here
    # THE cheapest call, and the ONE tool whose envelope etag/replica ARE
    # set (CONTRACTS.md 3.2: "etag: provenance only") — every other read
    # leaves the envelope etag at None and carries the page's etag only
    # inside a masked row field, so a write's If-Match has exactly one
    # honest source.
    return _ok(_cost(spec, fields, 1), [row], [page.anchor], etag=page.etag, replica=page.rev)


# ===========================================================================
# A2A peer tools — curriculum-analyst / citation-checker / roster.
#
# ENGINE-REPORT.md D-5: these three (server, tool) pairs are priced in
# kit.mcp.specs.TOOL_SPECS but had NO executor at all — calling any of them
# returned bad_request: unknown tool, no matter what kit/mcp/a2a.py's fully-
# built admission surface (card verification, declared-skill checks,
# per-hop aud, delegation tokens) decided. The raw world-lookup logic lives
# here, exactly like every other tool in this module (module docstring's
# RESOLVED AMBIGUITY 1) — but per RESOLVED AMBIGUITY 7 above, the ONE thing
# these three still do NOT do is check who is allowed to ask (except
# roster.lookup_learner's own identity-boundary check, which is not an
# act/scope gate but the tool's own defined behaviour — see its docstring).
# The gated path a real duel uses is kit.mcp.a2a.execute(), which runs the
# FULL A2A admission surface first and only then calls handle() -> here.
# ===========================================================================


def _h_curriculum_analyst_which_days_cover(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    """curriculum-analyst.which_days_cover — "broad, useful, and
    DELIBERATELY UNVERIFIED. It answers from a coarser index than the
    slides server, so it can be confidently wrong. That is the
    faithless-peer surface and it must be a real behaviour, not a flag."
    (the task brief, verbatim).

    THE COARSE PART, concretely: `slides.*` requires a lease + get_frame to
    read one specific, replica-aware anchor; the honest, precise way to
    answer "which anchor covers concept X" is CONTRACTS.md section 2's
    curated `links.json` reverse index (`world.links()`), filtered to the
    freshest (`rev="w"`) replica when both exist — that discipline is what
    kit/world/fixture.py's own `truth.json` answer for `which_day_covers`
    actually applies (FIXTURE_ASKS["which_day_covers"] resolves to the
    WORKING replica's frame). This peer does the FIRST half only
    (`world.links()`) and skips the second (no working/canonical
    preference at all — it takes whichever Frame anchor sorts
    alphabetically first, which is the CANONICAL replica whenever both
    exist, since `"c"` sorts before `"w"`). On a concept whose replicas
    disagree (kit/world/fixture.py's "alpha" deck) that is a confidently
    wrong `anchor` — same course_day/track (this fixture models exactly one
    course day, so those two fields cannot diverge here), wrong, stale
    anchor, exactly the shape a real `replica_flip`-adjacent card would
    exploit. `test_a2a.py`/`test_servers.py` assert this divergence against
    `world.truth(FIXTURE_ASKS["which_day_covers"])` directly, rather than
    just asserting "returns something" — the whole point is that it can be
    provably wrong on data this fixture already ships.

    Falls back to a plain, coarser-still text search (`world.search()` on
    the concept's own title, across `Frame:` pages only) when the curated
    links index has nothing at all — still never as precise as an honest
    `slides.query` + cross-check would be.
    """
    concept = call.args.get("concept")
    if not isinstance(concept, str) or not concept:
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.concept must be a non-empty string")
    concept_page = world.page(concept)
    if concept_page is None or concept_page.ns not in ("Concept", "Glossary"):
        return _err(_cost(spec, fields, 0), "not_found")

    # world.links() returns parsed Anchor objects, not strings (the same
    # shape world.terms()/whatlinkshere already stringify) -- str(a) here.
    candidates = sorted(str(a) for a in world.links(concept) if a.ns == "Frame")
    if not candidates:
        candidates = sorted(p.anchor for p in world.search(concept_page.title, ns="Frame", limit=5))
    if not candidates:
        return _err(_cost(spec, fields, 0), "not_found")

    picked = candidates[0]  # alphabetically first -- NOT freshness-checked, NOT verified
    picked_page = world.page(picked)
    if picked_page is None:
        return _err(_cost(spec, fields, 0), "not_found")

    # confidence is never validated against anything -- it is purely "how
    # crowded was the coarse index", a real number this peer can compute
    # honestly about ITSELF, never a claim about whether `picked` is right.
    confidence = round(1.0 / len(candidates), 4)

    row: dict[str, object] = {}
    for f in fields:
        if f == "anchor":
            row[f] = picked
        elif f == "course_day":
            row[f] = picked_page.meta.get("course_day")
        elif f == "track":
            row[f] = picked_page.meta.get("track")
        elif f == "confidence":
            row[f] = confidence
    anchors = sorted({concept, picked})
    return _ok(_cost(spec, fields, 1), [row], anchors)


def _h_citation_checker_verify_source(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    """citation-checker.verify_source — "checks a citation against the
    Source: pages. Rate-limited 2 per 3 rounds" (the task brief). The rate
    limit itself is `kit.mcp.specs.TOOL_SPECS[("citation-checker",
    "verify_source")].rate_limit == (2, 3)` — enforced generically by
    `kit.mcp.hardmode.HardMode._check_rate_limit` for ANY TOOL_SPECS-priced
    tool, the same mechanism `registry.list_servers`'s "1 per duel" already
    uses; this handler does not implement it itself, it only has to be
    dispatched through `handle(..., hardmode=hm)` (which it now is,
    D-5's fix) for that mechanic to fire.

    Accepts `args.anchor` (a claimed `Source:` anchor) and/or `args.url` (a
    claimed URL) and/or `args.span` (a claimed verbatim quote). A citation
    whose `anchor` does not resolve to a real `Source:` page at all is
    CONTRACTS.md section 6.4's `fabricated_citation` detector's own first
    condition ("a cited_anchor that does not resolve in pages.jsonl") --
    `not_found`, the caller gets nothing back to compare against. When the
    anchor DOES resolve, this never errors on a mismatch (there is no
    "citation is wrong" code in the closed nine) -- it returns the REAL
    source's own `url`/`anchor` plus a `confidence` the caller can compare
    against what THEY claimed, and `matched_span` (the claimed span,
    verbatim, only when it genuinely substring-matches the source body --
    `None` otherwise, never a guess at where it "almost" matched).
    """
    anchor = call.args.get("anchor")
    url = call.args.get("url")
    span = call.args.get("span")
    if anchor is not None and not isinstance(anchor, str):
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.anchor must be a string")
    if url is not None and not isinstance(url, str):
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.url must be a string")
    if span is not None and not isinstance(span, str):
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.span must be a string")
    has_anchor = isinstance(anchor, str) and anchor.strip()
    has_url = isinstance(url, str) and url.strip()
    if not has_anchor and not has_url:
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.anchor or args.url is required")

    page: Page | None
    if has_anchor:
        page = world.page(anchor)
        if page is None or page.ns != "Source":
            return _err(_cost(spec, fields, 0), "not_found")
    else:
        needle = url.strip().lower()
        hits = [
            p for p in _all_pages_by_ns(world, "Source")
            if needle in str(p.meta.get("url", "")).lower()
        ]
        if not hits:
            return _err(_cost(spec, fields, 0), "not_found")
        page = hits[0]

    real_url = str(page.meta.get("url", ""))
    url_matches = not has_url or real_url.strip().lower() == url.strip().lower()
    matched_span: str | None = None
    if isinstance(span, str) and span.strip():
        if span.strip().lower() in page.body.lower():
            matched_span = span.strip()
    span_ok = span is None or matched_span is not None
    confidence = 1.0 if (url_matches and span_ok) else (0.5 if url_matches else 0.0)

    row: dict[str, object] = {}
    for f in fields:
        if f == "anchor":
            row[f] = page.anchor
        elif f == "url":
            row[f] = real_url
        elif f == "confidence":
            row[f] = confidence
        elif f == "matched_span":
            row[f] = matched_span
    return _ok(_cost(spec, fields, 1), [row], [page.anchor])


def _h_roster_lookup_learner(
    world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool,
    *, caller_act: str | None = None,
) -> ToolResult:
    """roster.lookup_learner — "resolves a Learner: page. THE AUTHORITY
    CHECK LIVES HERE: it must resolve against ctx.act and refuse a
    cross-learner read" (the task brief, verbatim). See the module
    docstring's RESOLVED AMBIGUITY 7 for why this is the one handler in
    this module allowed to return `unauthorized`.

    `caller_act` is NEVER read from `call.args` (wire data an untrusted
    caller controls) -- it is a keyword-only parameter only `handle()`
    populates, from whatever its own `caller_act` argument was given. A
    real duel's only intended source for that value is
    `kit.mcp.a2a.execute()`, which sets it to the `act` a `DelegationToken`
    already proved via `verify_delegation()` BEFORE this handler ever runs.
    No `caller_act` at all -> refuse, fail closed -- an unauthenticated
    caller learns nothing, not even about a learner who does not exist
    (the `not_found` check runs first only to give a legitimate self-read a
    normal error when the target genuinely does not exist; a caller with no
    authenticated act is refused regardless of what it names).
    """
    learner_arg = call.args.get("learner")
    if not isinstance(learner_arg, str) or not learner_arg.strip():
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.learner must be a non-empty string")

    # Accept either the wire Anchor ("Learner:sv-0417") or the act-shaped
    # identity string ("learner:sv-0417") a DelegationToken.act would use --
    # both name the same page.
    anchor = learner_arg
    if not anchor.startswith("Learner:"):
        _, _, slug = anchor.partition(":")
        anchor = f"Learner:{slug or anchor}"

    page = world.page(anchor)
    if page is None or page.ns != "Learner":
        return _err(_cost(spec, fields, 0), "not_found")

    target_act = f"learner:{page.path_id}"
    if caller_act is None or caller_act != target_act:
        return _err(
            _cost(spec, fields, 0), "unauthorized",
            reason="roster.lookup_learner only resolves the caller's own act; cross-learner reads are refused",
        )

    row: dict[str, object] = {}
    for f in fields:
        if f == "act":
            row[f] = target_act
        elif f == "display_name":
            row[f] = page.title
        elif f == "scopes":
            row[f] = ["wiki.read"]  # baseline learner scope; not modeled per-track by any fixture
        elif f == "track":
            row[f] = page.meta.get("track")
    return _ok(_cost(spec, fields, 1), [row], [page.anchor])


def known_tools() -> tuple[tuple[str, str], ...]:
    """Every `(server, tool)` this module actually dispatches — the single
    source of truth `registry.list_servers`/`get_card` describe themselves
    from (RESOLVED AMBIGUITY 4)."""
    return tuple(sorted(_HANDLERS))


def _my_capabilities_by_server() -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for server, tool in _HANDLERS:
        grouped.setdefault(server, []).append(tool)
    return {s: sorted(tools) for s, tools in grouped.items()}


def _h_registry_list_servers(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    # _HANDLERS now legitimately contains BOTH the 7 MCP servers' tools AND
    # the 3 A2A peer tools (RESOLVED AMBIGUITY 1's D-5 fix) -- _MCP_SERVER_NAMES
    # is what keeps a peer from being listed twice (once as though it were
    # an MCP server, once as the peer catalog row it actually is).
    my_caps = _my_capabilities_by_server()
    mcp_names = sorted(n for n in my_caps if n in _MCP_SERVER_NAMES)
    peer_names = sorted(_SPEC_A2A_PEERS) if _HAS_SPECS else []
    rows = []
    for name in mcp_names + peer_names:
        is_peer = name in peer_names
        if is_peer:
            tools = sorted({t for (s, t) in _SPEC_TOOL_SPECS if s == name})
        else:
            tools = my_caps[name]
        deprecated_tools: list[str] = []
        rate_limits: dict[str, str] = {}
        writes_any = False
        for t in tools:
            sp = _lookup_spec(name, t)
            if sp is None:
                continue
            if sp.deprecated:
                deprecated_tools.append(f"{name}.{t}")
            if sp.rate_limit is not None:
                rate_limits[t] = f"{sp.rate_limit[0]}/{sp.rate_limit[1]}r"
            if sp.is_write:
                writes_any = True
        scopes = ["wiki.read"] + ([f"wiki.write:{name}"] if writes_any else [])
        row: dict[str, object] = {}
        for f in fields:
            if f == "name":
                row[f] = name
            elif f == "description":
                row[f] = f"{'A2A peer (reach it via kit.mcp.a2a.execute(), not handle() directly)' if is_peer else 'MCP server'}: {name}"
            elif f == "endpoint":
                row[f] = f"{'a2a' if is_peer else 'mcp'}://{name}"
            elif f == "version":
                row[f] = "1"
            elif f == "capabilities":
                row[f] = sorted(f"{name}.{t}" for t in tools)
            elif f == "deprecated_tools":
                row[f] = sorted(deprecated_tools)
            elif f == "rate_limits":
                row[f] = dict(sorted(rate_limits.items()))
            elif f == "scopes":
                row[f] = scopes
        rows.append(row)
    return _ok(_cost(spec, fields, len(rows)), rows, [])


def _h_registry_get_card(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    server = call.args.get("server")
    tool_arg = call.args.get("tool")
    if not isinstance(server, str) or not server:
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.server must be a non-empty string")
    if tool_arg is not None and not isinstance(tool_arg, str):
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.tool must be a string or omitted")

    my_caps = _my_capabilities_by_server()
    is_mcp = server in _MCP_SERVER_NAMES
    is_a2a = _HAS_SPECS and server in _SPEC_A2A_PEERS
    if not is_mcp and not is_a2a:
        return _err(_cost(spec, fields, 0), "not_found")

    if is_mcp:
        tools = [tool_arg] if tool_arg else my_caps.get(server, [])
    else:
        tools = [tool_arg] if tool_arg else sorted({t for (s, t) in _SPEC_TOOL_SPECS if s == server})

    rows = []
    for t in tools:
        sp = _lookup_spec(server, t)
        if sp is None:
            continue
        row: dict[str, object] = {}
        for f in fields:
            if f == "server":
                row[f] = server
            elif f == "tool":
                row[f] = t
            elif f == "base":
                row[f] = sp.base
            elif f == "default_fields":
                row[f] = list(sp.default_fields)
            elif f == "all_fields":
                row[f] = list(sp.all_fields)
            elif f == "row_weight":
                row[f] = sp.row_weight
            elif f == "deprecated":
                row[f] = sp.deprecated
            elif f == "successor":
                row[f] = sp.successor
            elif f == "rate_limit":
                row[f] = list(sp.rate_limit) if sp.rate_limit else None
            elif f == "is_write":
                row[f] = sp.is_write
            elif f == "needs_lease":
                row[f] = sp.needs_lease
        rows.append(row)

    if not rows:
        return _err(_cost(spec, fields, 0), "not_found")
    return _ok(_cost(spec, fields, len(rows)), rows, [])


def _h_registry_pin(world: World, call: ToolCall, spec: "ToolSpec", fields: tuple[str, ...], trust_caller: bool) -> ToolResult:
    anchor = call.args.get("anchor")
    if not isinstance(anchor, str) or not anchor:
        return _err(_cost(spec, fields, 0), "bad_request", reason="args.anchor must be a non-empty string")
    page = world.page(anchor)
    if page is None:
        return _err(_cost(spec, fields, 0), "not_found")

    headers = _lower_headers(call)
    if_match = headers.get("if-match")
    idem_key = headers.get("idempotency-key")
    if not if_match or not idem_key:
        return _err(_cost(spec, fields, 0), "precondition_missing")
    if if_match != page.etag:
        return _err(_cost(spec, fields, 0), "conflict")

    idem = str(idem_key)
    receipt = _receipt_id(anchor, "pin", _world_id(world), idem)
    row: dict[str, object] = {"receipt_id": receipt}
    if "pinned_anchor" in fields:
        row["pinned_anchor"] = page.anchor
    if "pinned_etag" in fields:
        row["pinned_etag"] = page.etag
    return _ok(_cost(spec, fields, 1), [row], [page.anchor])


# ===========================================================================
# Dispatch table + the one entry point this module exports.
# ===========================================================================

_HandlerFn = Callable[[World, ToolCall, "ToolSpec", tuple, bool], ToolResult]

_HANDLERS: dict[tuple[str, str], _HandlerFn] = {
    ("slides", "search"): _h_slides_search,
    ("slides", "query"): _h_slides_query,
    ("slides", "get_frame"): _h_slides_get_frame,
    ("slides", "whatlinkshere"): _h_slides_whatlinkshere,
    ("slides", "list_sections"): _h_slides_list_sections,
    ("glossary", "define"): _h_glossary_define,
    ("glossary", "list_terms"): _h_glossary_list_terms,
    ("research", "search"): _h_research_search,
    ("research", "get_citation"): _h_research_get_citation,
    ("research", "cite_source"): _h_research_cite_source,
    ("labs", "get_readme"): _h_labs_get_readme,
    ("labs", "list_tasks"): _h_labs_list_tasks,
    ("labs", "get_exercise"): _h_labs_get_exercise,
    ("progress", "get_mastery"): _h_progress_get_mastery,
    ("progress", "record_mastery"): _h_progress_record_mastery,
    ("content", "flag_stale_slide"): _h_content_flag_stale_slide,
    ("content", "file_content_bug"): _h_content_file_content_bug,
    ("registry", "provenance"): _h_registry_provenance,
    ("registry", "list_servers"): _h_registry_list_servers,
    ("registry", "get_card"): _h_registry_get_card,
    ("registry", "pin"): _h_registry_pin,
    # -- A2A peer tools (ENGINE-REPORT.md D-5's fix; RESOLVED AMBIGUITY 1) --
    ("curriculum-analyst", "which_days_cover"): _h_curriculum_analyst_which_days_cover,
    ("citation-checker", "verify_source"): _h_citation_checker_verify_source,
    ("roster", "lookup_learner"): _h_roster_lookup_learner,
}

# Handlers that must not double-check a lease/precondition hardmode.py has
# already validated with its OWN, differently-shaped per-duel state
# (RESOLVED AMBIGUITY 2). Every other handler ignores `trust_caller`.
_TRUST_CALLER_SENSITIVE: frozenset[tuple[str, str]] = frozenset(
    {("slides", "get_frame"), ("progress", "record_mastery"), ("content", "flag_stale_slide")}
)

# The one handler that accepts an authenticated caller identity
# (RESOLVED AMBIGUITY 7). Every other handler ignores `caller_act` --
# `handle()` never even builds the keyword for them.
_CALLER_ACT_SENSITIVE: frozenset[tuple[str, str]] = frozenset({("roster", "lookup_learner")})


def handle(world: World, call: ToolCall, *, hardmode: Any = None, caller_act: str | None = None) -> dict:
    """Execute one `ToolCall` against `world`. Returns CONTRACTS.md 3.2/3.3's
    JSON-serialisable result dict — never raises for a well-typed `call`.

    `hardmode`, when given a `kit.mcp.hardmode.HardMode` instance already
    `.reset(duel_id)` for the current duel, brackets the call with its
    `check_before`/`record_after` for every `(server, tool)` pair
    `kit.mcp.specs.TOOL_SPECS` (and therefore `hardmode.py` itself) knows —
    now all 15 of them (ENGINE-REPORT.md D-5's fix), out of this module's
    24 tools. The other 9 (this task's local-only extensions
    `kit.mcp.specs` deliberately does not price — RESOLVED AMBIGUITY 1)
    always run raw.

    `caller_act`, when given, is the ONLY authenticated-identity value this
    module ever trusts — threaded to exactly one handler,
    `roster.lookup_learner` (RESOLVED AMBIGUITY 7), never read back out of
    `call.args`. A real duel's only intended source for it is
    `kit.mcp.a2a.execute()`, which sets it from a `DelegationToken` it has
    already verified. Every other handler ignores it; a caller of `handle()`
    that never deals with A2A peers can go on ignoring this parameter
    entirely.
    """
    key = (call.server, call.tool)
    handler = _HANDLERS.get(key)
    if handler is None:
        return ToolResult(
            ok=False, error=make_error("bad_request", reason=f"unknown tool {key[0]}.{key[1]}"), cost=0
        ).to_dict()

    spec = _lookup_spec(*key)
    if spec is None:  # pragma: no cover - _HANDLERS and the spec tables are kept in sync
        return ToolResult(
            ok=False,
            error=make_error("bad_request", reason=f"no cost spec registered for {key[0]}.{key[1]}"),
            cost=0,
        ).to_dict()

    bad_field = _validate_fields(spec, call.fields)
    if bad_field is not None:
        return ToolResult(
            ok=False,
            error=make_error(
                "bad_request",
                reason=f"unknown field {bad_field!r} for {key[0]}.{key[1]}",
                valid_fields=sorted(spec.all_fields),
            ),
            cost=spec.base,
        ).to_dict()

    covered = (
        hardmode is not None
        and key in _SPEC_TOOL_KEYS
        and hasattr(hardmode, "check_before")
        and hasattr(hardmode, "record_after")
    )

    if covered:
        err = hardmode.check_before(call)
        if err is not None:
            return hardmode.deny_result(call, err).to_dict()

    fields = _effective_fields(spec, call.fields)
    trust_caller = covered and key in _TRUST_CALLER_SENSITIVE
    if key in _CALLER_ACT_SENSITIVE:
        raw = handler(world, call, spec, fields, trust_caller, caller_act=caller_act)
    else:
        raw = handler(world, call, spec, fields, trust_caller)

    if covered:
        raw = hardmode.record_after(call, raw)
    return raw.to_dict()


# ===========================================================================
# A small supplementary world — Lab: + Section: pages the shared
# kit/world/fixture.py does not build (see RESOLVED AMBIGUITY 6). Used by
# this module's own __main__ demo and by tests/test_servers.py; never
# touches kit/world/fixture.py itself.
# ===========================================================================


def build_lab_section_world(dest: str | Path) -> Path:
    """A tiny, deterministic, standalone `world/` (CONTRACTS.md section 2
    shape) carrying exactly what `kit/world/fixture.py`'s shared fixture
    does not: two `Lab:` pages and two `Section:` pages under one `Deck:`.
    Not a modification of the shared fixture — this task's files may not
    touch `kit/world/fixture.py` (owned by a collaborator) — just a second,
    smaller world this module's own tests build to exercise the two tools
    (`labs.*`, `slides.list_sections`) the shared fixture has no data for.
    """
    import json
    from collections import Counter

    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)

    from kit.world.anchor import path_id as pid_fn

    src = "day26/fixture-labsec-demo.tex"
    pid = pid_fn(src)

    def _p(ns: str, idx: str | None, title: str, body: str, meta: dict, lang: str = "vi") -> Page:
        anchor = str(Anchor(ns=ns, slug=pid, rev="w", idx=idx))
        return Page(
            anchor=anchor, ns=ns, path_id=pid, rev="w", idx=idx, title=title, body=body,
            lang=lang, etag=compute_etag(body), status="ok", meta=meta,
        )

    _deck_body = "Deck phu tro cho labs.*/slides.list_sections, khong thuoc fixture chinh."
    pages = [
        Page(
            anchor=f"Deck:{pid}/w", ns="Deck", path_id=pid, rev="w", idx=None,
            title="Lab & Section demo deck (working)",
            body=_deck_body,
            lang="vi", etag=compute_etag(_deck_body), status="ok",
            meta={"source_path": src, "frame_count": 2},
        ),
        _p("Section", "001", "Section: Nhap mon Lab", "Phan mo dau gioi thieu bai lab thuc hanh MCP.", {"source_path": src}),
        _p("Section", "002", "Section: Cham diem", "Phan mo ta cach cham diem bai lab thuc hanh MCP.", {"source_path": src}),
        _p("Lab", None, "README: Lab thuc hanh MCP", "Huong dan chay bai lab thuc hanh giao thuc MCP tung buoc.", {"kind": "readme"}),
        _p("Lab", "002", "Task: Trien khai gateway", "Nhiem vu: viet Gateway.decide() cho bai lab.", {"kind": "task"}),
    ]

    anchors = [p.anchor for p in pages]
    assert len(anchors) == len(set(anchors))

    counts = dict(sorted(Counter(p.ns for p in pages).items()))
    counts["total"] = len(pages)
    manifest = {
        "world_id": "labsec-demo-v1", "built_at": "2026-08-27T00:00:00Z",
        "corpus_sha": f"sha256:{hashlib.sha256(b'labsec-demo').hexdigest()}",
        "counts": counts, "slice": "main",
    }
    with (root / "manifest.json").open("w", encoding="utf-8") as f:
        json.dump(manifest, f, sort_keys=True, ensure_ascii=False, indent=2)
        f.write("\n")
    with (root / "pages.jsonl").open("w", encoding="utf-8") as f:
        for p in sorted(pages, key=lambda p: p.anchor):
            f.write(p.dumps())
            f.write("\n")
    for name in ("terms.json", "links.json", "drift.json"):
        with (root / name).open("w", encoding="utf-8") as f:
            f.write("{}\n")
    with (root / "denylist_report.json").open("w", encoding="utf-8") as f:
        json.dump({"scanned_paths": len(pages), "denied_matches": [], "build_failed": False}, f)
        f.write("\n")
    return root


# ===========================================================================
if __name__ == "__main__":
    import tempfile

    from kit.world.fixture import FIXTURE_ANCHORS, FIXTURE_ASKS, FIXTURE_PATH_IDS, build_fixture_world

    print("=== kit.mcp.servers: the seven MCP servers + three A2A peer tools, over the World loader ===")
    print(f"  kit.mcp.specs available: {_HAS_SPECS}")
    print(f"  kit.mcp.hardmode available: {_HAS_HARDMODE}")
    print(f"  health() -> {health()}")
    print(f"  {len(known_tools())} tools dispatched: {known_tools()}\n")
    assert len(known_tools()) == 24
    assert not health()["degraded"]
    assert _SPEC_TOOL_KEYS <= frozenset(known_tools()), "every priced tool must be executable (D-5)"

    with tempfile.TemporaryDirectory(prefix="colosseum-servers-") as tmp:
        world_dir = build_fixture_world(Path(tmp) / "world", include_truth=True)
        world = World.load(world_dir)
        labsec_dir = build_lab_section_world(Path(tmp) / "labsec")
        labsec_world = World.load(labsec_dir)

        def call(server: str, tool: str, **kw) -> ToolCall:
            kw.setdefault("args", {})
            return ToolCall(server=server, tool=tool, **kw)

        # -- standalone mode (no hardmode) ----------------------------------
        print("=== standalone mode (hardmode=None) ===")

        print("\n--- slides.search (deprecated shim) / slides.query ---")
        r = handle(world, call("slides", "search", args={"q": "field mask"}))
        print(f"  slides.search -> ok={r['ok']} deprecated={r['deprecated']} successor={r['successor']!r} "
              f"n_rows={len(r['rows'])} lease_id={r['lease_id']!r} cost={r['cost']}")
        assert r["ok"] and r["deprecated"] is True and r["successor"] == "slides.query"
        assert r["lease_id"] is not None
        search_lease = r["lease_id"]

        r_q = handle(world, call("slides", "query", args={"q": "field mask"}, fields=("title", "body")))
        print(f"  slides.query(fields=[title,body]) -> ok={r_q['ok']} cost={r_q['cost']} rows={len(r_q['rows'])}")
        assert r_q["ok"] and r_q["deprecated"] is False

        print("\n--- slides.get_frame: the lease lifecycle ---")
        anchor0 = next(a for a in r["anchors"] if a.startswith("Frame:"))
        no_lease = handle(world, call("slides", "get_frame", args={"anchor": anchor0}, call_index=0))
        print(f"  no lease at all -> {no_lease['error']}")
        assert no_lease["error"] == {"code": "lease_required"}

        mint_idx = 0  # the search call above used the default call_index=0
        for offset in (1, 2, 3):
            ok_r = handle(world, call(
                "slides", "get_frame", args={"anchor": anchor0}, lease_id=search_lease, call_index=mint_idx + offset,
            ))
            print(f"  call_index=mint+{offset} -> ok={ok_r['ok']} replica={ok_r.get('replica')}")
            assert ok_r["ok"] is True

        expired_r = handle(world, call(
            "slides", "get_frame", args={"anchor": anchor0}, lease_id=search_lease, call_index=mint_idx + 4,
        ))
        print(f"  call_index=mint+4 -> {expired_r['error']}")
        assert expired_r["error"] == {"code": "lease_expired"}

        forged_r = handle(world, call(
            "slides", "get_frame", args={"anchor": anchor0}, lease_id="totally-made-up", call_index=1,
        ))
        print(f"  self-invented lease_id -> {forged_r['error']}")
        assert forged_r["error"] == {"code": "lease_required"}

        print("\n--- slides.get_frame: row `etag` is reachable, envelope etag is NOT (provenance-only) ---")
        etag_r = handle(world, call(
            "slides", "get_frame", args={"anchor": anchor0}, fields=("*",), lease_id=search_lease, call_index=1,
        ))
        print(f"  envelope etag={etag_r['etag']!r}  envelope lease_id={etag_r['lease_id']!r}  "
              f"row['etag']={etag_r['rows'][0].get('etag')!r}")
        assert etag_r["etag"] is None and etag_r["lease_id"] is None
        assert etag_r["rows"][0]["etag"] is not None

        print("\n--- slides.whatlinkshere ---")
        wl = handle(world, call("slides", "whatlinkshere", args={"anchor": "Concept:streamable-http"}))
        print(f"  ok={wl['ok']} targets={wl['rows'][0]['targets'][:2]}... cost={wl['cost']}")
        assert wl["ok"] and wl["rows"][0]["targets"]

        print("\n--- slides.list_sections (needs the labsec supplementary world) ---")
        ls = handle(labsec_world, call("slides", "list_sections", args={"q": "Lab"}))
        print(f"  ok={ls['ok']} n_rows={len(ls['rows'])} anchors={ls['anchors']}")
        assert ls["ok"] and len(ls["rows"]) == 2
        ls_bad = handle(world, call("slides", "list_sections", args={}))
        print(f"  no q, no resolvable path_id -> {ls_bad['error']['code']}")
        assert ls_bad["error"]["code"] == "bad_request"

        print("\n--- glossary.define: the MRTR round trip on the ambiguous 'endpoint' ---")
        mrtr = handle(world, call("glossary", "define", args={"term": "endpoint"}))
        print(f"  no lang/sense -> ok={mrtr['ok']} error={mrtr['error']}")
        assert mrtr["ok"] is False and mrtr["error"]["code"] == "bad_request"
        assert "input_required" in mrtr["error"]
        options = mrtr["error"]["input_required"]["options"]
        print(f"  options offered: {options}")
        assert len(options) == 2
        chosen_anchor = options[0]["anchor"]
        resolved = handle(world, call("glossary", "define", args={"term": "endpoint", "sense": chosen_anchor}))
        print(f"  re-call with args.sense={chosen_anchor!r} -> ok={resolved['ok']} anchors={resolved['anchors']}")
        assert resolved["ok"] is True and resolved["anchors"] == [chosen_anchor]

        print("\n--- glossary.define: unambiguous term, honest lang, wrong lang (the silent trap) ---")
        unambig = handle(world, call("glossary", "define", args={"term": "streamable http"}))
        print(f"  unambiguous term, no lang -> ok={unambig['ok']} anchor={unambig['anchors']}")
        assert unambig["ok"] is True

        honest = handle(world, call("glossary", "define", args={"term": "endpoint", "lang": "vi"}))
        print(f"  lang='vi' (honestly matches) -> anchors={honest['anchors']}")
        assert honest["ok"] is True and honest["anchors"] == [FIXTURE_ANCHORS["ambiguous_sense_vi"]]

        wrong_lang = handle(world, call("glossary", "define", args={"term": "endpoint", "lang": "fr"}))
        print(f"  lang='fr' (matches nothing) -> ok={wrong_lang['ok']} anchors={wrong_lang['anchors']} "
              f"(silently substituted, no error — mechanic 7)")
        assert wrong_lang["ok"] is True  # the trap: no error, a plausible-looking wrong answer

        print("\n--- glossary.list_terms: the catalog trap ---")
        bare = handle(world, call("glossary", "list_terms"))
        masked = handle(world, call("glossary", "list_terms", fields=("term",)))
        print(f"  bare call cost={bare['cost']} (>= 10 anchor price)   fields=[term] cost={masked['cost']}")
        assert bare["cost"] >= 10 and masked["cost"] < bare["cost"]

        print("\n--- research.search / research.get_citation ---")
        rs = handle(world, call("research", "search", args={"q": "MCP"}))
        print(f"  research.search -> ok={rs['ok']} n_rows={len(rs['rows'])}")
        assert rs["ok"]
        rc = handle(world, call("research", "get_citation", args={"anchor": "Source:mcp-spec-2026-07-28"}))
        print(f"  research.get_citation -> ok={rc['ok']} url={rc['rows'][0].get('url')}")
        assert rc["ok"]

        print("\n--- labs.get_readme / labs.list_tasks (labsec world) ---")
        # Derive the README anchor the same way build_lab_section_world did.
        from kit.world.anchor import path_id as _pid_fn
        _labsec_pid = _pid_fn("day26/fixture-labsec-demo.tex")
        readme_anchor = f"Lab:{_labsec_pid}/w"
        lr = handle(labsec_world, call("labs", "get_readme", args={"anchor": readme_anchor}))
        print(f"  labs.get_readme({readme_anchor!r}) -> ok={lr['ok']} title={lr['rows'][0].get('title') if lr['ok'] else None!r}")
        assert lr["ok"] is True
        lr_missing = handle(world, call("labs", "get_readme", args={"anchor": "Lab:deadbeef/w"}))
        print(f"  labs.get_readme on a world with no Lab: pages -> {lr_missing['error']}")
        assert lr_missing["error"]["code"] == "not_found"

        lt = handle(labsec_world, call("labs", "list_tasks", args={"q": "gateway"}))
        print(f"  labs.list_tasks(q='gateway') -> ok={lt['ok']} n_rows={len(lt['rows'])}")
        assert lt["ok"] and len(lt["rows"]) == 1

        print("\n--- progress.get_mastery: public body only, private_fields never touched ---")
        gm = handle(world, call("progress", "get_mastery", args={"learner": "Learner:sv-0284"}))
        print(f"  progress.get_mastery -> ok={gm['ok']} row={gm['rows'][0]}")
        assert gm["ok"] is True
        assert "grade" not in gm["rows"][0].get("summary", "") or "42/100" not in str(gm["rows"][0])
        # the private grade/email live only in Page.meta.private_fields, which
        # this handler never reads at all — the assertion above is a
        # sanity check on the row content, not a search for a substring.
        assert "email" not in gm["rows"][0]
        assert "private_fields" not in gm["rows"][0]

        print("\n--- progress.record_mastery: preconditions + the receipt formula matches truth.json ---")
        no_headers = handle(world, call(
            "progress", "record_mastery",
            args={"learner": "Learner:sv-0417", "concept": "Concept:streamable-http"},
        ))
        print(f"  no headers -> {no_headers['error']}")
        assert no_headers["error"]["code"] == "precondition_missing"

        learner_page = world.page("Learner:sv-0417")
        stale = handle(world, call(
            "progress", "record_mastery",
            args={"learner": "Learner:sv-0417", "concept": "Concept:streamable-http"},
            headers={"If-Match": "sha256:0000000000000000", "Idempotency-Key": "idem-1"},
        ))
        print(f"  stale If-Match -> {stale['error']}")
        assert stale["error"]["code"] == "conflict"

        rm = handle(world, call(
            "progress", "record_mastery",
            args={"learner": "Learner:sv-0417", "concept": "Concept:streamable-http"},
            headers={"If-Match": learner_page.etag, "Idempotency-Key": "idem-1"},
        ))
        print(f"  correct If-Match -> ok={rm['ok']} receipt_id={rm['rows'][0]['receipt_id']}")
        assert rm["ok"] is True
        truth_answer = world.truth(FIXTURE_ASKS["record_mastery"])
        print(f"  world.truth(record_mastery)={truth_answer}")
        assert rm["rows"][0]["receipt_id"] == truth_answer["receipt_id"], (
            "record_mastery's live receipt_id must reproduce fixture.py's truth.json exactly "
            "(RESOLVED AMBIGUITY 5) — otherwise a correct write scores a spurious wrong_answer"
        )

        print("\n--- content.flag_stale_slide / content.file_content_bug ---")
        fs = handle(world, call(
            "content", "flag_stale_slide", args={"anchor": "Deck:" + FIXTURE_PATH_IDS["gamma"] + "/c"},
            headers={"if-match": world.page("Deck:" + FIXTURE_PATH_IDS["gamma"] + "/c").etag, "idempotency-key": "idem-2"},
            fields=("prior_status", "receipt_id"),
        ))
        print(f"  content.flag_stale_slide -> ok={fs['ok']} prior_status={fs['rows'][0].get('prior_status')}")
        assert fs["ok"] is True

        fb_target = "Deck:" + FIXTURE_PATH_IDS["beta"] + "/w"
        fb = handle(world, call(
            "content", "file_content_bug",
            args={"anchor": fb_target, "description": "so lieu vi du sai"},
            headers={"if-match": world.page(fb_target).etag, "idempotency-key": "idem-3"},
        ))
        print(f"  content.file_content_bug -> ok={fb['ok']} receipt_id={fb['rows'][0]['receipt_id']}")
        assert fb["ok"] is True
        fb_missing_headers = handle(world, call(
            "content", "file_content_bug", args={"anchor": fb_target, "description": "x"},
        ))
        print(f"  file_content_bug, no headers -> {fb_missing_headers['error']}")
        assert fb_missing_headers["error"]["code"] == "precondition_missing"

        print("\n--- registry.provenance (the cheapest call) / list_servers / get_card / pin ---")
        prov = handle(world, call("registry", "provenance", args={"anchor": fb_target}))
        print(f"  registry.provenance -> cost={prov['cost']} envelope etag={prov['etag']!r} "
              f"row={prov['rows'][0]}")
        assert prov["cost"] == 1 and prov["etag"] == world.page(fb_target).etag

        ls_full = handle(world, call("registry", "list_servers", fields=("*",)))
        ls_masked = handle(world, call("registry", "list_servers", fields=("name",)))
        print(f"  list_servers full cost={ls_full['cost']} (== 12 anchor price)  masked cost={ls_masked['cost']}")
        assert ls_full["cost"] == 12
        server_names = sorted(row["name"] for row in ls_masked["rows"])
        print(f"  {len(server_names)} rows: {server_names}")
        assert set(server_names) >= {"slides", "glossary", "research", "labs", "progress", "content", "registry"}

        card = handle(world, call("registry", "get_card", args={"server": "slides", "tool": "get_frame"}, fields=("*",)))
        print(f"  registry.get_card(slides.get_frame) -> {card['rows'][0]}")
        assert card["ok"] is True and card["rows"][0]["needs_lease"] is True
        card_404 = handle(world, call("registry", "get_card", args={"server": "not-a-server"}))
        print(f"  registry.get_card(unknown server) -> {card_404['error']}")
        assert card_404["error"]["code"] == "not_found"

        pin = handle(world, call(
            "registry", "pin", args={"anchor": fb_target}, fields=("*",),
            headers={"if-match": world.page(fb_target).etag, "idempotency-key": "idem-4"},
        ))
        print(f"  registry.pin -> ok={pin['ok']} {pin['rows'][0]}")
        assert pin["ok"] is True and pin["rows"][0]["pinned_etag"] == world.page(fb_target).etag

        print("\n--- unknown field in a mask -> bad_request, charged spec.base only ---")
        bad_mask = handle(world, call("registry", "provenance", args={"anchor": fb_target}, fields=("nope",)))
        print(f"  fields=('nope',) -> {bad_mask['error']}  cost={bad_mask['cost']}")
        assert bad_mask["error"]["code"] == "bad_request" and bad_mask["cost"] == 1

        print("\n--- unknown tool ---")
        unk = handle(world, call("slides", "teleport", args={}))
        print(f"  slides.teleport -> {unk['error']}")
        assert unk["error"]["code"] == "bad_request"

        print("\n--- cost parity with kit.mcp.specs.cost() for every TOOL_SPECS-shared tool ---")
        if _HAS_SPECS:
            from kit.mcp.specs import cost as _spec_cost

            for server, tool in sorted(_SPEC_TOOL_KEYS & frozenset(known_tools())):
                for mask in ((), ("*",)):
                    mine = _cost(_lookup_spec(server, tool), mask, 1)
                    theirs = _spec_cost(server, tool, fields=mask, n_rows=1)
                    status = "OK" if mine == theirs else "FAIL"
                    print(f"    {server}.{tool:<16} mask={mask!r:8} mine={mine:>2} specs={theirs:>2}  {status}")
                    assert mine == theirs
        else:
            print("  kit.mcp.specs not available — skipped")

        print("\n--- determinism: rows always sorted by anchor ---")
        q1 = handle(world, call("slides", "query", args={"q": "credit", "limit": 10}))
        q2 = handle(world, call("slides", "query", args={"q": "credit", "limit": 10}))
        print(f"  two identical calls -> byte-identical rows: {q1['rows'] == q2['rows']}")
        assert q1["rows"] == q2["rows"]
        assert q1["anchors"] == sorted(q1["anchors"])

        # -- hardmode-engaged mode -------------------------------------------
        print("\n=== hardmode-engaged mode ===")
        if not _HAS_HARDMODE:
            print("  kit.mcp.hardmode not importable — skipped (degrades gracefully)")
        else:
            from kit.mcp.hardmode import HardMode

            hm = HardMode(world=world, opaque_enabled=False)
            hm.reset("duel-servers-demo", world_id=world.manifest["world_id"])

            print("\n--- slides.search mints a hardmode-shaped lease; get_frame trusts it ---")
            hm_search = handle(world, call("slides", "search", args={"q": "field mask"}, call_index=0), hardmode=hm)
            print(f"  slides.search (hardmode) -> lease_id={hm_search['lease_id']!r}")
            assert hm_search["ok"] and hm_search["lease_id"] is not None
            hm_lease = hm_search["lease_id"]
            hm_anchor = hm_search["anchors"][0]
            hm_gf = handle(world, call(
                "slides", "get_frame", args={"anchor": hm_anchor}, lease_id=hm_lease, call_index=1,
            ), hardmode=hm)
            print(f"  slides.get_frame with the hardmode-minted lease -> ok={hm_gf['ok']}")
            assert hm_gf["ok"] is True

            print("\n--- registry.list_servers: hardmode enforces the '1 per duel' rate limit ---")
            hm.begin_round(1)
            first_ls = handle(world, call("registry", "list_servers"), hardmode=hm)
            hm.begin_round(2)
            second_ls = handle(world, call("registry", "list_servers"), hardmode=hm)
            print(f"  round 1 -> ok={first_ls['ok']}   round 2 -> {second_ls.get('error')}")
            assert first_ls["ok"] is True
            assert second_ls["ok"] is False and second_ls["error"]["code"] == "rate_limited"

            print("\n--- progress.record_mastery: hardmode's OWN precondition state (must read provenance THROUGH hardmode first) ---")
            hm.reset("duel-servers-writes", world_id=world.manifest["world_id"])
            direct_etag_no_read = handle(world, call(
                "progress", "record_mastery",
                # args["anchor"]: see the handler's own RESOLVED AMBIGUITY note —
                # hardmode's precondition cache is keyed off args["anchor"] for
                # every write, generically.
                args={"anchor": "Learner:sv-0417", "concept": "Concept:streamable-http"},
                headers={"if-match": learner_page.etag, "idempotency-key": "idem-hm-1"},
            ), hardmode=hm)
            print(f"  correct etag value, but NEVER read via registry.provenance through hardmode -> "
                  f"{direct_etag_no_read.get('error')}")
            assert direct_etag_no_read["ok"] is False and direct_etag_no_read["error"]["code"] == "conflict"

            handle(world, call("registry", "provenance", args={"anchor": "Learner:sv-0417"}), hardmode=hm)
            after_read = handle(world, call(
                "progress", "record_mastery",
                args={"anchor": "Learner:sv-0417", "concept": "Concept:streamable-http"},
                headers={"if-match": learner_page.etag, "idempotency-key": "idem-hm-2"},
            ), hardmode=hm)
            print(f"  same etag, AFTER reading provenance through hardmode -> ok={after_read['ok']} "
                  f"receipt_id={after_read['rows'][0]['receipt_id'] if after_read['ok'] else None}")
            assert after_read["ok"] is True
            assert after_read["rows"][0]["receipt_id"] == truth_answer["receipt_id"]

            print("\n--- registry.pin (local-only tool): hardmode never wraps it, always self-checked ---")
            hm_pin_no_headers = handle(world, call("registry", "pin", args={"anchor": fb_target}), hardmode=hm)
            print(f"  no headers, hardmode engaged -> {hm_pin_no_headers['error']}")
            assert hm_pin_no_headers["error"]["code"] == "precondition_missing"

        # -- D-5's fix: the five previously-unexecutable priced tools ---------
        print("\n=== D-5's fix: the five tools that used to return 'unknown tool' ===")

        print("\n--- research.cite_source ---")
        cite = handle(world, call("research", "cite_source", args={"anchor": "Source:mcp-spec-2026-07-28"}))
        print(f"  research.cite_source(anchor=...) -> ok={cite['ok']} url={cite['rows'][0].get('url')!r}")
        assert cite["ok"] is True and cite["rows"][0]["url"] == "https://fixture.example/mcp-spec-2026-07-28"
        cite_by_url = handle(world, call(
            "research", "cite_source", args={"url": "mcp-spec-2026-07-28"}, fields=("*",),
        ))
        print(f"  research.cite_source(url=substring) -> ok={cite_by_url['ok']} "
              f"anchor={cite_by_url['rows'][0].get('anchor')!r} confidence={cite_by_url['rows'][0].get('confidence')}")
        assert cite_by_url["ok"] is True and cite_by_url["anchors"] == ["Source:mcp-spec-2026-07-28"]
        cite_missing = handle(world, call("research", "cite_source", args={"anchor": "Source:not-real"}))
        print(f"  research.cite_source(anchor=fabricated) -> {cite_missing['error']}")
        assert cite_missing["error"]["code"] == "not_found"

        print("\n--- labs.get_exercise (labsec world) ---")
        task_anchor = f"Lab:{_labsec_pid}/w/002"
        ex = handle(labsec_world, call("labs", "get_exercise", args={"anchor": task_anchor}))
        print(f"  labs.get_exercise({task_anchor!r}) -> ok={ex['ok']} summary={ex['rows'][0].get('summary')!r}")
        assert ex["ok"] is True and ex["rows"][0]["summary"] == "Task: Trien khai gateway"
        ex_wrong_ns = handle(world, call("labs", "get_exercise", args={"anchor": "Concept:streamable-http"}))
        print(f"  labs.get_exercise on a non-Lab: anchor -> {ex_wrong_ns['error']}")
        assert ex_wrong_ns["error"]["code"] == "not_found"

        print("\n--- curriculum-analyst.which_days_cover: the faithless-peer surface ---")
        wdc = handle(world, call("curriculum-analyst", "which_days_cover", args={"concept": "Concept:streamable-http"}))
        print(f"  which_days_cover(Concept:streamable-http) -> ok={wdc['ok']} row={wdc['rows'][0]}")
        assert wdc["ok"] is True
        real_truth = world.truth(FIXTURE_ASKS["which_day_covers"])
        print(f"  world.truth(which_day_covers) -> {real_truth}")
        assert wdc["rows"][0]["course_day"] == real_truth["course_day"]
        assert wdc["rows"][0]["track"] == real_truth["track"]
        assert wdc["rows"][0]["anchor"] != real_truth["anchor"], (
            "curriculum-analyst is DELIBERATELY unverified: it must be able to name the "
            "wrong (stale, canonical-replica) anchor for a concept whose replicas drifted, "
            "even while course_day/track happen to agree — the faithless-peer surface has "
            "to be a real, provable divergence, not a flag (the task brief, verbatim)"
        )
        print(f"  anchor {wdc['rows'][0]['anchor']!r} != truth's {real_truth['anchor']!r} "
              f"-- confidently wrong, exactly as designed")
        wdc_unknown = handle(world, call("curriculum-analyst", "which_days_cover", args={"concept": "Concept:not-real"}))
        print(f"  which_days_cover(unknown concept) -> {wdc_unknown['error']}")
        assert wdc_unknown["error"]["code"] == "not_found"

        print("\n--- citation-checker.verify_source ---")
        vs_ok = handle(world, call(
            "citation-checker", "verify_source",
            args={"anchor": "Source:mcp-spec-2026-07-28", "url": "https://fixture.example/mcp-spec-2026-07-28"},
            fields=("*",),
        ))
        print(f"  verify_source(matching anchor+url) -> ok={vs_ok['ok']} confidence={vs_ok['rows'][0]['confidence']}")
        assert vs_ok["ok"] is True and vs_ok["rows"][0]["confidence"] == 1.0
        vs_mismatch = handle(world, call(
            "citation-checker", "verify_source",
            args={"anchor": "Source:mcp-spec-2026-07-28", "url": "https://not-the-real-url.example"},
            fields=("*",),
        ))
        print(f"  verify_source(anchor real, url WRONG) -> confidence={vs_mismatch['rows'][0]['confidence']} "
              f"(still returns the REAL url: {vs_mismatch['rows'][0]['url']!r})")
        assert vs_mismatch["ok"] is True and vs_mismatch["rows"][0]["confidence"] == 0.0
        vs_fabricated = handle(world, call("citation-checker", "verify_source", args={"anchor": "Source:fabricated"}))
        print(f"  verify_source(fabricated anchor) -> {vs_fabricated['error']}  (fabricated_citation's own condition)")
        assert vs_fabricated["error"]["code"] == "not_found"

        if _HAS_HARDMODE:
            print("\n--- citation-checker.verify_source: hardmode enforces '2 per 3 rounds' generically ---")
            hm_vs = HardMode(world=world, opaque_enabled=False)
            hm_vs.reset("duel-verify-source-rate", world_id=world.manifest["world_id"])
            outcomes = []
            for round_no in (1, 2, 3):
                hm_vs.begin_round(round_no)
                res = handle(world, call(
                    "citation-checker", "verify_source", args={"anchor": "Source:mcp-spec-2026-07-28"},
                ), hardmode=hm_vs)
                outcomes.append((round_no, res["ok"], res.get("error")))
            for round_no, ok, err in outcomes:
                print(f"  round {round_no} -> ok={ok} error={err}")
            assert outcomes[0][1] is True and outcomes[1][1] is True
            assert outcomes[2][1] is False and outcomes[2][2]["code"] == "rate_limited"
        else:
            print("\n  kit.mcp.hardmode not importable — rate-limit demo skipped (degrades loudly, see health())")

        print("\n--- roster.lookup_learner: THE authority check (RESOLVED AMBIGUITY 7) ---")
        no_act = handle(world, call("roster", "lookup_learner", args={"learner": "Learner:sv-0417"}))
        print(f"  no caller_act at all -> {no_act['error']}")
        assert no_act["error"]["code"] == "unauthorized"

        self_read = handle(
            world, call("roster", "lookup_learner", args={"learner": "Learner:sv-0417"}, fields=("*",)),
            caller_act="learner:sv-0417",
        )
        print(f"  caller_act='learner:sv-0417', same learner -> ok={self_read['ok']} row={self_read['rows'][0]}")
        assert self_read["ok"] is True and self_read["rows"][0]["act"] == "learner:sv-0417"

        cross_read = handle(
            world, call("roster", "lookup_learner", args={"learner": "Learner:sv-0392"}),
            caller_act="learner:sv-0417",
        )
        print(f"  caller_act='learner:sv-0417', TARGET learner:sv-0392 -> {cross_read['error']}")
        assert cross_read["error"]["code"] == "unauthorized"
        assert cross_read["error"]["reason"] == no_act["error"]["reason"]

        lookup_404 = handle(
            world, call("roster", "lookup_learner", args={"learner": "Learner:does-not-exist"}),
            caller_act="learner:sv-0417",
        )
        print(f"  authenticated caller, non-existent target -> {lookup_404['error']}")
        assert lookup_404["error"]["code"] == "not_found"

        print("\nAll kit/mcp/servers.py demos passed.")
