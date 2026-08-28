"""kit/mcp/specs.py — TOOL_SPECS, the tool economy as DATA (CONTRACTS.md section 3.4).

FINAL-PLAN.md section 4: "Under flat pricing an audit showed `list_servers` and
`list_terms` each exceeded a whole round's sustainable allowance — punishment
buttons, not decisions — and `curriculum-analyst` was strictly dominated, which
killed the A2A half of the lesson. With field masks no tool is dominated; only
careless calls are." This module is the fix: one dict literal (:data:`TOOL_SPECS`)
mapping every ``(server, tool)`` pair to a :class:`ToolSpec`, so the economy can be
retuned by editing numbers here — never by touching the cost formula, the gateway,
or the referee.

The cost formula itself (CONTRACTS.md 3.4, reproduced exactly)::

    cost = base + sum(field_weight[f] for f in effective_fields) + n_rows * row_weight

where ``effective_fields`` is ``spec.default_fields`` when the caller passed no
mask, or ``spec.all_fields`` when the caller passed ``("*",)``, or the caller's
own mask otherwise.

Seven MCP servers (``slides`` · ``glossary`` · ``research`` · ``labs`` ·
``progress`` · ``content`` · ``registry``) and three A2A peers
(``curriculum-analyst`` · ``citation-checker`` · ``roster``) — every one of the
ten gets at least one tool below.

Stdlib only. No network, no randomness, no wall-clock.

RESOLVED AMBIGUITY — where ``cost_of`` lives: CONTRACTS.md 3.4 states only that
"``TOOL_SPECS`` lives in ``kit/mcp/specs.py``" and then shows the ``cost_of()``
code block immediately underneath, without separately naming a file for the
function. ``kit/mcp/__init__.py`` (a collaborator's file, written concurrently)
settles it explicitly: it does ``from kit.mcp.specs import TOOL_SPECS, cost_of``
inside a ``try/except (ImportError, AttributeError)`` — so this module owns both
the data and the one pure function that reads it. ``cost_of`` takes its ``call``
argument duck-typed (only ``.server``/``.tool``/``.fields`` are read) rather than
importing ``kit.mcp.types.ToolCall`` — per this workspace's hard rule 2, a
collaborator's file may not exist yet, and even now that it does, a real
``ToolCall`` instance satisfies the duck type without this module ever importing
it, so retuning this file never risks breaking on someone else's class shape.

D-10 FIX (ENGINE-REPORT.md): the disciplined round used to price out at 11 cr
under this table (query[title,body]=6 + get_frame(default)=4 + provenance=1).
11 x 10 rounds = 110 > the 100-credit duel budget — FINAL-PLAN.md 4.3's "sustainable
across 10 rounds on 100" was argued (lease/rate-limit amortization) but never
measured, and the argument doesn't even reach this specific worst-case round (it
has no lease or rate-limited call to amortize). ``slides.query`` is retuned below
so the disciplined round is 9 cr, 10 rounds cost 90 of 100, and the round-total
still sits under ``referee/detectors.py``'s independently-declared
``ROUND_ALLOWANCE = 11`` (the ``wasteful`` ceiling), which this module does not
import and was not touched. Full arithmetic, the rookie line, and the
no-tool-dominated proof are in ``Day26-Colosseum-Agent-Arena/ECONOMY.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

__all__ = [
    "ROUNDS_PER_DUEL",
    "WRITE_HEADERS",
    "MCP_SERVERS",
    "A2A_PEERS",
    "ToolSpec",
    "TOOL_SPECS",
    "cost_of",
    "cost",
    "cheapest_owners_of",
]

# CONTRACTS.md section 0 / FINAL-PLAN.md section 3: a duel is exactly 10
# simultaneous rounds, always — never a variable "match length".
ROUNDS_PER_DUEL = 10

# CONTRACTS.md section 4.1: a Command's headers arrive with "already
# lowercased keys". Writes need both of these (FINAL-PLAN.md section 4.2,
# mechanic 3; this module's task brief).
WRITE_HEADERS: tuple[str, ...] = ("idempotency-key", "if-match")

MCP_SERVERS: frozenset[str] = frozenset(
    {"slides", "glossary", "research", "labs", "progress", "content", "registry"}
)
A2A_PEERS: frozenset[str] = frozenset({"curriculum-analyst", "citation-checker", "roster"})

_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One row of the tool economy: everything :func:`cost_of` needs for one
    ``(server, tool)`` pair, plus the hard-mode metadata (CONTRACTS.md 3.3 /
    4.2, FINAL-PLAN.md 4.2) that other modules key their own logic on —
    leases, preconditions, rate windows, deprecation.

    Every field is validated on construction so a retune that breaks an
    invariant (a typo'd field name, a default not present in ``all_fields``,
    a write missing its required headers) fails immediately at import time,
    not three files downstream in a duel replay.
    """

    server: str
    tool: str
    base: int
    field_weight: Mapping[str, int]
    default_fields: tuple[str, ...]
    all_fields: tuple[str, ...]
    row_weight: int = 0
    deprecated: bool = False
    successor: str | None = None  # "server.tool" dotted form; required iff deprecated
    rate_limit: tuple[int, int] | None = None  # (calls, per_rounds); None = credit-bounded only
    is_write: bool = False
    required_headers: tuple[str, ...] = ()
    needs_lease: bool = False  # CONTRACTS.md 4.2 mechanic 2: reserved for get_frame

    def __post_init__(self) -> None:
        ident = f"{self.server}.{self.tool}"

        if not isinstance(self.server, str) or not self.server:
            raise ValueError(f"ToolSpec.server must be a non-empty str, got {self.server!r}")
        if not isinstance(self.tool, str) or not self.tool:
            raise ValueError(f"ToolSpec.tool must be a non-empty str, got {self.tool!r}")
        if not isinstance(self.base, int) or isinstance(self.base, bool) or self.base < 0:
            raise ValueError(f"{ident}: base must be a non-negative int, got {self.base!r}")
        if (
            not isinstance(self.row_weight, int)
            or isinstance(self.row_weight, bool)
            or self.row_weight < 0
        ):
            raise ValueError(f"{ident}: row_weight must be a non-negative int, got {self.row_weight!r}")

        weight_map = dict(self.field_weight)
        for name, weight in weight_map.items():
            if not isinstance(name, str) or not _FIELD_NAME_RE.match(name):
                raise ValueError(f"{ident}: malformed field name {name!r} in field_weight")
            if not isinstance(weight, int) or isinstance(weight, bool) or weight < 0:
                raise ValueError(f"{ident}: field_weight[{name!r}] must be a non-negative int, got {weight!r}")
        object.__setattr__(self, "field_weight", MappingProxyType(weight_map))

        all_set = set(self.all_fields)
        if len(all_set) != len(self.all_fields):
            raise ValueError(f"{ident}: all_fields has duplicate entries: {self.all_fields}")
        if tuple(sorted(self.all_fields)) != self.all_fields:
            raise ValueError(f"{ident}: all_fields must be sorted for reproducibility, got {self.all_fields}")
        if all_set != set(weight_map):
            raise ValueError(
                f"{ident}: all_fields {sorted(all_set)} must exactly match "
                f"field_weight's keys {sorted(weight_map)}"
            )

        default_set = set(self.default_fields)
        if len(default_set) != len(self.default_fields):
            raise ValueError(f"{ident}: default_fields has duplicate entries: {self.default_fields}")
        if tuple(sorted(self.default_fields)) != self.default_fields:
            raise ValueError(
                f"{ident}: default_fields must be sorted for reproducibility, got {self.default_fields}"
            )
        if not default_set <= all_set:
            raise ValueError(
                f"{ident}: default_fields {self.default_fields} is not a subset of all_fields {self.all_fields}"
            )

        if "*" in all_set:
            raise ValueError(f"{ident}: '*' is the reserved wildcard sentinel, not a real field name")

        if self.deprecated and self.successor is None:
            raise ValueError(f"{ident}: deprecated tool must name a successor")
        if not self.deprecated and self.successor is not None:
            raise ValueError(f"{ident}: non-deprecated tool must not name a successor, got {self.successor!r}")
        if self.successor is not None and "." not in self.successor:
            raise ValueError(f"{ident}: successor {self.successor!r} must be dotted 'server.tool'")

        if self.rate_limit is not None:
            if (
                not isinstance(self.rate_limit, tuple)
                or len(self.rate_limit) != 2
                or not all(isinstance(x, int) and not isinstance(x, bool) for x in self.rate_limit)
            ):
                raise ValueError(f"{ident}: rate_limit must be a (calls, per_rounds) int pair, got {self.rate_limit!r}")
            calls, per_rounds = self.rate_limit
            if calls < 1 or per_rounds < 1:
                raise ValueError(f"{ident}: rate_limit entries must be >= 1, got {self.rate_limit}")
            if per_rounds > ROUNDS_PER_DUEL:
                raise ValueError(
                    f"{ident}: rate_limit window {per_rounds} exceeds a duel's {ROUNDS_PER_DUEL} rounds"
                )

        if self.is_write:
            if self.required_headers != WRITE_HEADERS:
                raise ValueError(
                    f"{ident}: a write must require exactly {WRITE_HEADERS}, got {self.required_headers}"
                )
        elif self.required_headers:
            raise ValueError(f"{ident}: a read-only tool must not require headers, got {self.required_headers}")

        if self.needs_lease and self.tool != "get_frame":
            raise ValueError(
                f"{ident}: needs_lease=True is reserved for get_frame (CONTRACTS.md 4.2 mechanic 2)"
            )


# ---------------------------------------------------------------------------
# The table. Anchor prices (FINAL-PLAN.md section 4 + this task's brief) hold
# EXACTLY at the marked lines; every other number is a local tuning decision.
# See the module docstring's numbers walkthrough in test_cost.py for how each
# was derived.
# ---------------------------------------------------------------------------

TOOL_SPECS: dict[tuple[str, str], ToolSpec] = {
    # -- slides ---------------------------------------------------------
    ("slides", "search"): ToolSpec(
        server="slides",
        tool="search",
        base=1,
        field_weight={"anchor": 0, "snippet": 1, "title": 1},
        default_fields=("anchor", "title"),  # cost 2  <- anchor price
        all_fields=("anchor", "snippet", "title"),
        row_weight=0,
        deprecated=True,
        successor="slides.query",
    ),
    ("slides", "query"): ToolSpec(
        server="slides",
        tool="query",
        base=1,
        # D-10 retune (ENGINE-REPORT.md; full arithmetic in ECONOMY.md): title
        # dropped 1->0 to match get_frame's own title=0 ("titles are free
        # everywhere; body is the payload you pay for"). That takes the
        # disciplined round's query[title,body] leg from 6cr to 4cr, which
        # takes the whole disciplined round from 11cr/round (110 over 10
        # rounds — negative headroom, D-10) to 9cr/round (90 over 10 rounds,
        # 10cr of real headroom). body is unchanged at 2 (same per-field rate
        # get_frame already charges for body) — this creates exactly one tie
        # (query and get_frame both cost 4 for fields=[body]) where get_frame
        # used to win outright by 1cr; see ECONOMY.md "the one tie this
        # retune creates" for why that tie does not dominate get_frame.
        field_weight={"body": 2, "score": 2, "title": 0},
        default_fields=("title",),  # cost 1
        all_fields=("body", "score", "title"),
        row_weight=1,  # the one tool that exercises n_rows in the disciplined round
    ),
    ("slides", "get_frame"): ToolSpec(
        server="slides",
        tool="get_frame",
        base=2,
        field_weight={
            "body": 2,
            "confidence": 0,
            "etag": 0,
            "extraction_tier": 0,
            "lang": 1,
            "links": 1,
            "meta": 2,
            "status": 1,
            "title": 0,
        },
        default_fields=("body", "title"),  # cost 4
        all_fields=(
            "body", "confidence", "etag", "extraction_tier", "lang", "links", "meta", "status", "title",
        ),  # cost 9  <- fields=["*"]
        row_weight=0,
        needs_lease=True,
    ),
    ("slides", "whatlinkshere"): ToolSpec(
        server="slides",
        tool="whatlinkshere",
        base=2,
        field_weight={"count": 1, "targets": 0},
        default_fields=("targets",),  # cost 2  <- anchor price
        all_fields=("count", "targets"),
        row_weight=0,
    ),
    # -- glossary ---------------------------------------------------------
    ("glossary", "define"): ToolSpec(
        server="glossary",
        tool="define",
        base=1,
        field_weight={"definition": 0, "examples": 1, "sense": 1, "source_term": 1},
        default_fields=("definition",),  # cost 1  <- anchor price
        all_fields=("definition", "examples", "sense", "source_term"),
        row_weight=0,
    ),
    ("glossary", "list_terms"): ToolSpec(
        server="glossary",
        tool="list_terms",
        base=1,
        field_weight={"aliases": 1, "definition": 4, "redirect": 1, "sense": 2, "term": 1},
        # default == full: the bare call is the "punishment button" FINAL-PLAN
        # 4.1 describes; a narrow mask (e.g. fields=["term"], cost 2) is what
        # turns it into a decision instead.
        default_fields=("aliases", "definition", "redirect", "sense", "term"),  # cost 10  <- anchor price
        all_fields=("aliases", "definition", "redirect", "sense", "term"),
        row_weight=0,
    ),
    # -- research ---------------------------------------------------------
    ("research", "cite_source"): ToolSpec(
        server="research",
        tool="cite_source",
        base=2,
        field_weight={"anchor": 1, "confidence": 1, "snippet": 2, "url": 2},
        default_fields=("anchor", "url"),
        all_fields=("anchor", "confidence", "snippet", "url"),
        row_weight=0,
    ),
    # -- labs ---------------------------------------------------------
    ("labs", "get_exercise"): ToolSpec(
        server="labs",
        tool="get_exercise",
        base=2,
        field_weight={"instructions": 3, "kc_refs": 1, "starter_code": 2, "summary": 1},
        default_fields=("instructions", "summary"),
        all_fields=("instructions", "kc_refs", "starter_code", "summary"),
        row_weight=0,
    ),
    # -- progress ---------------------------------------------------------
    ("progress", "record_mastery"): ToolSpec(
        server="progress",
        tool="record_mastery",
        base=4,
        field_weight={"mastery_level": 1, "receipt_id": 0},
        default_fields=(),  # a write's cheapest response is the bare receipt
        all_fields=("mastery_level", "receipt_id"),
        row_weight=0,
        is_write=True,
        required_headers=WRITE_HEADERS,
    ),
    # -- content ---------------------------------------------------------
    ("content", "flag_stale_slide"): ToolSpec(
        server="content",
        tool="flag_stale_slide",
        base=3,
        field_weight={"prior_status": 1, "receipt_id": 0},
        default_fields=(),  # cost 3  <- anchor price
        all_fields=("prior_status", "receipt_id"),
        row_weight=0,
        is_write=True,
        required_headers=WRITE_HEADERS,
    ),
    # -- registry ---------------------------------------------------------
    ("registry", "provenance"): ToolSpec(
        server="registry",
        tool="provenance",
        base=1,
        field_weight={"checked_at": 1, "etag": 0, "last_writer": 1, "rev": 0},
        default_fields=("etag",),  # cost 1  <- anchor price, deliberately the cheapest call
        all_fields=("checked_at", "etag", "last_writer", "rev"),
        row_weight=0,
    ),
    ("registry", "list_servers"): ToolSpec(
        server="registry",
        tool="list_servers",
        base=1,
        field_weight={
            "capabilities": 2,
            "deprecated_tools": 2,
            "description": 1,
            "endpoint": 1,
            "name": 1,
            "rate_limits": 2,
            "scopes": 1,
            "version": 1,
        },
        # default == full, same "punishment button -> decision" reasoning as
        # list_terms above; fields=["name"] (cost 2) is the disciplined path.
        default_fields=(
            "capabilities", "deprecated_tools", "description", "endpoint", "name", "rate_limits", "scopes",
            "version",
        ),  # cost 12  <- anchor price, "the full dump"
        all_fields=(
            "capabilities", "deprecated_tools", "description", "endpoint", "name", "rate_limits", "scopes",
            "version",
        ),
        row_weight=0,
        rate_limit=(1, ROUNDS_PER_DUEL),  # "1 per duel"
    ),
    # -- A2A peers ---------------------------------------------------------
    ("curriculum-analyst", "which_days_cover"): ToolSpec(
        server="curriculum-analyst",
        tool="which_days_cover",
        base=3,
        field_weight={"anchor": 1, "confidence": 1, "course_day": 2, "track": 2},
        # matches the which_day_covers ask's own `require` list (CONTRACTS 7)
        default_fields=("anchor", "course_day", "track"),  # cost 8  <- anchor price
        all_fields=("anchor", "confidence", "course_day", "track"),
        row_weight=0,
    ),
    ("citation-checker", "verify_source"): ToolSpec(
        server="citation-checker",
        tool="verify_source",
        base=2,
        field_weight={"anchor": 2, "confidence": 1, "matched_span": 1, "url": 2},
        default_fields=("anchor", "url"),  # cost 6  <- anchor price
        all_fields=("anchor", "confidence", "matched_span", "url"),
        row_weight=0,
        rate_limit=(2, 3),  # "2 per 3 rounds"
    ),
    ("roster", "lookup_learner"): ToolSpec(
        server="roster",
        tool="lookup_learner",
        base=2,
        field_weight={"act": 1, "display_name": 1, "scopes": 2, "track": 1},
        default_fields=("act", "scopes"),
        all_fields=("act", "display_name", "scopes", "track"),
        row_weight=0,
    ),
}


def _validate_specs(specs: Mapping[tuple[str, str], ToolSpec]) -> None:
    """Whole-table invariants no single :class:`ToolSpec` can check alone:
    identity matches its dict key, every server/peer has >= 1 tool, and every
    ``successor`` resolves to a real, non-deprecated tool."""
    seen_mcp: set[str] = set()
    seen_a2a: set[str] = set()
    for key, spec in specs.items():
        if (spec.server, spec.tool) != key:
            raise AssertionError(f"TOOL_SPECS key {key} does not match spec identity {(spec.server, spec.tool)}")
        if spec.server in MCP_SERVERS:
            seen_mcp.add(spec.server)
        elif spec.server in A2A_PEERS:
            seen_a2a.add(spec.server)
        else:
            raise AssertionError(
                f"{spec.server!r} is neither a known MCP server {sorted(MCP_SERVERS)} "
                f"nor A2A peer {sorted(A2A_PEERS)}"
            )
        if spec.successor is not None:
            succ_server, _, succ_tool = spec.successor.partition(".")
            succ_key = (succ_server, succ_tool)
            if succ_key not in specs:
                raise AssertionError(f"{key}: successor {spec.successor!r} does not resolve to a real tool")
            if specs[succ_key].deprecated:
                raise AssertionError(f"{key}: successor {spec.successor!r} is itself deprecated")

    missing_mcp = MCP_SERVERS - seen_mcp
    if missing_mcp:
        raise AssertionError(f"MCP servers with no tool defined: {sorted(missing_mcp)}")
    missing_a2a = A2A_PEERS - seen_a2a
    if missing_a2a:
        raise AssertionError(f"A2A peers with no tool defined: {sorted(missing_a2a)}")


_validate_specs(TOOL_SPECS)


def _effective_fields(spec: ToolSpec, fields: tuple[str, ...]) -> tuple[str, ...]:
    effective = spec.default_fields if not fields else fields
    if effective == ("*",):
        effective = spec.all_fields
    return effective


def _cost_from_spec(spec: ToolSpec, fields: tuple[str, ...], n_rows: int) -> int:
    effective = _effective_fields(spec, fields)
    try:
        field_sum = sum(spec.field_weight[f] for f in effective)
    except KeyError as exc:
        raise KeyError(
            f"{spec.server}.{spec.tool}: unknown field {exc.args[0]!r} in mask "
            f"(valid fields: {sorted(spec.all_fields)})"
        ) from exc
    return spec.base + field_sum + n_rows * spec.row_weight


def cost_of(call: Any, n_rows: int) -> int:
    """CONTRACTS.md 3.4, verbatim:

        cost = base + sum(field_weight[f] for f in mask) + n_rows * row_weight

    ``call`` is duck-typed — anything exposing ``.server``, ``.tool``,
    ``.fields`` (the shape CONTRACTS.md 3.1 gives ``ToolCall``, and what
    ``kit/mcp/types.py``'s real ``ToolCall`` provides) works here without
    this module importing that class. ``n_rows`` is supplied by the caller
    (the executor knows how many rows a call actually returned; this table
    does not)."""
    spec = TOOL_SPECS[(call.server, call.tool)]
    fields = tuple(call.fields) if call.fields else ()
    return _cost_from_spec(spec, fields, n_rows)


def cost(server: str, tool: str, fields: tuple[str, ...] = (), n_rows: int = 1) -> int:
    """Primitive convenience wrapper around :func:`cost_of` that needs no
    ``ToolCall``-shaped object — what ``tests/test_cost.py`` (and any other
    caller that just wants "what would this call cost") uses."""
    spec = TOOL_SPECS[(server, tool)]
    return _cost_from_spec(spec, tuple(fields), n_rows)


def cheapest_owners_of(field: str, *, n_rows: int = 1) -> tuple[tuple[str, str], ...]:
    """``(server, tool)`` pairs that expose ``field`` in their ``all_fields``,
    tied for the lowest cost of a singleton-field call ``fields=(field,)`` at
    the given (pinned, for comparability) ``n_rows``. Empty if no tool in the
    table exposes a field of this name. This is the "no tool is dominated"
    yardstick: a tool is undominated iff it owns (weakly) at least one of its
    own fields here — see ``test_no_tool_dominated`` in tests/test_cost.py
    for the full definition and worked example."""
    candidates = [
        (key, cost(key[0], key[1], fields=(field,), n_rows=n_rows))
        for key, spec in TOOL_SPECS.items()
        if field in spec.all_fields
    ]
    if not candidates:
        return ()
    best = min(c for _, c in candidates)
    return tuple(sorted(key for key, c in candidates if c == best))


if __name__ == "__main__":
    print(f"=== kit.mcp.specs: {len(TOOL_SPECS)} tools across "
          f"{len(MCP_SERVERS)} MCP servers + {len(A2A_PEERS)} A2A peers ===\n")
    for key in sorted(TOOL_SPECS):
        spec = TOOL_SPECS[key]
        default_cost = cost(*key)
        star_cost = cost(*key, fields=("*",))
        flags = []
        if spec.deprecated:
            flags.append(f"DEPRECATED->{spec.successor}")
        if spec.is_write:
            flags.append("write")
        if spec.needs_lease:
            flags.append("needs_lease")
        if spec.rate_limit:
            flags.append(f"rate={spec.rate_limit[0]}/{spec.rate_limit[1]}r")
        flag_str = f"  [{', '.join(flags)}]" if flags else ""
        print(
            f"  {key[0]}.{key[1]:<20} base={spec.base}  default={default_cost:>2}cr  "
            f"all={star_cost:>2}cr  fields={spec.all_fields}{flag_str}"
        )

    print("\n=== field-mask examples that must hold exactly (CONTRACTS.md 3.4) ===")
    checks = [
        ("slides.get_frame(fields=[title])", cost("slides", "get_frame", fields=("title",)), 2),
        ("slides.get_frame(fields=[*])", cost("slides", "get_frame", fields=("*",)), 9),
        ("registry.list_servers(fields=[name])", cost("registry", "list_servers", fields=("name",)), 2),
        ("registry.list_servers(fields=[*]) (full dump)", cost("registry", "list_servers", fields=("*",)), 12),
    ]
    for label, got, want in checks:
        status = "OK" if got == want else "FAIL"
        print(f"  {label:<48} = {got:>2}  (want {want})  {status}")
        assert got == want, f"{label}: got {got}, want {want}"

    print("\n=== named anchor prices (default fields, n_rows=1) ===")
    anchors = [
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
    for server, tool, want in anchors:
        got = cost(server, tool)
        status = "OK" if got == want else "FAIL"
        print(f"  {server}.{tool:<20} = {got:>2}  (want {want})  {status}")
        assert got == want, f"{server}.{tool}: got {got}, want {want}"

    print("\n=== acceptance arithmetic (FINAL-PLAN.md 4.3, retuned per D-10) ===")
    disciplined = (
        cost("slides", "query", fields=("title", "body"), n_rows=1)
        + cost("slides", "get_frame")
        + cost("registry", "provenance")
    )
    print(f"  disciplined round: query[title,body] + get_frame(default) + provenance = {disciplined} cr (<= 11)")
    assert disciplined <= 11, disciplined
    assert disciplined == 9, disciplined  # D-10: this table's tuned value

    disciplined_ten = disciplined * ROUNDS_PER_DUEL
    headroom = 100 - disciplined_ten
    print(f"  disciplined x{ROUNDS_PER_DUEL} rounds = {disciplined_ten} cr of 100 cr budget "
          f"(headroom {headroom} cr, was -10 cr before D-10)")
    assert disciplined_ten <= 100, disciplined_ten
    assert headroom == 10, headroom

    rookie = (
        cost("registry", "list_servers", fields=("*",))
        + cost("glossary", "list_terms")
        + 3 * cost("slides", "get_frame", fields=("*",))
    )
    print(f"  rookie round: list_servers(*) + list_terms() + 3x get_frame(*) = {rookie} cr (>= 45)")
    assert rookie >= 45, rookie
    print(f"  rookie bankrupt: 3 x {rookie} = {3 * rookie} cr > 100 cr budget (dies round 3)")
    assert 3 * rookie > 100, rookie

    print("\n=== no tool is dominated: each tool's winning field ===")
    for key in sorted(TOOL_SPECS):
        spec = TOOL_SPECS[key]
        winning = [f for f in spec.all_fields if key in cheapest_owners_of(f)]
        assert winning, f"{key} is dominated on every field it exposes"
        print(f"  {key[0]}.{key[1]:<20} wins/ties on: {winning}")

    print("\nAll kit/mcp/specs.py demos passed.")
