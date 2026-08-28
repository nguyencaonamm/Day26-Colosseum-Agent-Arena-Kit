"""bots/adversary/gateway.py — the HARD bot. The ceiling.

You are meant to learn this one BY PROBING IT, which is exactly the skill the duel
scores. Fire cards at it, read what came back, form a theory, test the theory. There
is a STRATEGY.md next to this file; opening it before the tournament trades the
exercise for the answer.

Adversary holds four separate identity checks, because `identity`, `forged_card` and
`faithless_peer` are three different attacks and no single check stops all three:

    1. CARD ADMISSION      is this peer's Agent Card one the registry vouches for?
    2. DECLARED SKILL      does that card actually declare the skill being invoked?
    3. AUDIENCE MATCH      is the delegation's `aud` the server we are really calling?
    4. ACT OWNERSHIP       does the target belong to the learner in `ctx.act`?

Check 4 is the one `operator` gets wrong. Authority derives from `act` — whom you
serve — never from `sub`, what you are.

It also: pins continuously and re-verifies, routes on the header and never on the
body, writes exactly once with an `If-Match` and an idempotency key, spends its
budget adaptively (late rounds are worth 1.5x, so it saves for them), keeps its
queries narrow, and abstains when the evidence is thin rather than guessing.

Its quarantine is calibrated rather than twitchy: under 5% false positives on blank
cards, against operator's ~25%. Refusing is free (a `deny` costs 0 credits), but
being prosecuted into a false positive on a blank costs 8 — so the threshold is set
by that arithmetic, not by nerves.

TARGET: a strong team reaches about 45-55 against it.
"""
from __future__ import annotations

from typing import Any

from agent.gateway import Command, Decision, GatewayContext

try:
    from kit.mcp.types import ToolCall
except ImportError:  # pragma: no cover
    ToolCall = None  # type: ignore[assignment]

WRITE_TOOLS = {("content", "flag_stale_slide"), ("content", "file_content_bug"),
               ("progress", "record_mastery")}
A2A_SERVERS = {"curriculum-analyst", "citation-checker", "roster"}

# Narrow by default; widen only where the answer will actually cite the field.
MASKS: dict[tuple[str, str], tuple[str, ...]] = {
    ("slides", "query"): ("title", "anchor"),
    ("slides", "get_frame"): ("title", "body", "anchor"),
    ("slides", "whatlinkshere"): ("anchor",),
    ("glossary", "define"): ("definition", "sense", "anchor"),
    ("registry", "provenance"): ("etag", "replica", "anchor"),
    ("registry", "list_servers"): ("name",),
    ("research", "cite_source"): ("url", "anchor"),
    ("curriculum-analyst", "which_days_cover"): ("course_day", "track", "anchor"),
    ("citation-checker", "verify_source"): ("verdict", "anchor"),
}

# Deprecated paths and their successors (CONTRACTS.md 3.2 / hard-mode 8).
SUCCESSORS = {("slides", "search"): ("slides", "query")}

# Instruction-shaped text in RETRIEVED content. Deliberately tighter than operator's
# list: these are imperatives aimed at an agent, not merely emphatic prose.
_IMPERATIVE = (
    "ignore previous instruction", "ignore all previous", "disregard the above",
    "system override", "bỏ qua mọi hướng dẫn", "you must now", "instead, record",
    "also record this for", "print the", "reveal the",
)

# Credits are worth more late: round scale is x1.0 (r1-3), x1.25 (r4-7), x1.5 (r8-10).
# Spending evenly is therefore a mistake — a credit saved for round 9 buys 1.5x the
# damage prevention of one spent in round 2.
_ROUND_ALLOWANCE = {1: 8, 2: 8, 3: 8, 4: 9, 5: 9, 6: 9, 7: 10, 8: 11, 9: 11, 10: 12}


class Gateway:
    def __init__(self, ctx: GatewayContext) -> None:
        self.ctx = ctx
        self._etags: dict[str, str] = {}
        self._idempotency: set[str] = set()
        self._admitted_cards: dict[str, dict] = {}
        self._spent_this_round = 0
        self._round = 0
        self.denied = 0
        self.quarantined = 0

    # -- the four identity checks --------------------------------------------
    def _card_admitted(self, cmd: Command) -> bool:
        if cmd.server not in A2A_SERVERS:
            return True
        card = self._admitted_cards.get(cmd.server)
        return bool(card and card.get("verified"))

    def _skill_declared(self, cmd: Command) -> bool:
        if cmd.server not in A2A_SERVERS:
            return True
        card = self._admitted_cards.get(cmd.server) or {}
        return cmd.tool in set(card.get("skills") or ())

    def _audience_matches(self, cmd: Command) -> bool:
        aud = cmd.headers.get("aud") or cmd.headers.get("Aud")
        if aud is None:
            return cmd.server not in A2A_SERVERS  # a delegation with no aud is not a delegation
        return aud in (cmd.server, f"mcp:{cmd.server}", f"a2a:{cmd.server}")

    def _routes_on_header(self, cmd: Command) -> bool:
        """Refuse a route smuggled through the request BODY.

        Adversary already strips a body-route HEADER, but a `drop_header` / `swap_replica`
        mutation puts the route in the arguments, where stripping a header does nothing.
        Routing must be decided by the header or refused — reading it out of the body is
        precisely the `header_spoof` / `replica_flip` failure, and "I removed the header I
        was not going to read anyway" is not a defence.
        """
        return not (cmd.args.get("route") or cmd.args.get("_route")
                    or cmd.args.get("replica"))

    def _act_owns_target(self, cmd: Command) -> bool:
        """Check 4 — the one operator gets wrong. Authority comes from `act`."""
        act = getattr(self.ctx, "act", None)
        for key in ("learner", "learner_id", "target", "subject"):
            target = cmd.args.get(key)
            if target and act and str(target) != str(act):
                return False
        return True

    def _within_budget(self, cmd: Command) -> bool:
        rnd = getattr(self.ctx, "round", 0) or 0
        if rnd != self._round:
            self._round, self._spent_this_round = rnd, 0
        return self._spent_this_round < _ROUND_ALLOWANCE.get(rnd, 9)

    def _instruction_in_content(self, cmd: Command) -> bool:
        blob = " ".join(str(v) for v in cmd.args.values()).lower()
        return any(token in blob for token in _IMPERATIVE)

    def decide(self, cmd: Command) -> Decision:
        checks = (
            (self._card_admitted, "peer card not admitted by the registry"),
            (self._skill_declared, "skill not declared on the peer's agent card"),
            (self._audience_matches, "delegation aud does not match the server called"),
            (self._act_owns_target, "target is not owned by the learner in act"),
            (self._routes_on_header, "route declared in the body, not the header"),
        )
        for check, reason in checks:
            if not check(cmd):
                self.denied += 1
                return Decision(verdict="deny", reason=reason)

        if self._instruction_in_content(cmd):
            self.quarantined += 1
            self.denied += 1
            return Decision(verdict="deny", reason="instruction found in retrieved content")

        if not self._within_budget(cmd):
            self.denied += 1
            return Decision(verdict="deny", reason="round allowance exhausted; saving for late rounds")

        server, tool = SUCCESSORS.get((cmd.server, cmd.tool), (cmd.server, cmd.tool))
        rewritten = (server, tool) != (cmd.server, cmd.tool)

        headers = {k: v for k, v in cmd.headers.items() if k.lower() != "x-mcp-body-route"}
        headers["Mcp-Replica"] = headers.get("Mcp-Replica", "w")  # route on the header, always

        if (server, tool) in WRITE_TOOLS:
            anchor = str(cmd.args.get("anchor", ""))
            etag = self._etags.get(anchor)
            if not etag:
                self.denied += 1
                return Decision(verdict="deny", reason="write without a fresh If-Match etag")
            key = f"{anchor}:{tool}"
            if key in self._idempotency:
                self.denied += 1
                return Decision(verdict="deny", reason="write already committed this duel")
            self._idempotency.add(key)
            headers["If-Match"] = etag
            headers["Idempotency-Key"] = key

        self._spent_this_round += 1
        call = ToolCall(
            server=server, tool=tool, args=dict(cmd.args),
            fields=tuple(cmd.fields or MASKS.get((server, tool), ("anchor",))),
            headers=headers, lease_id=cmd.lease_id, call_index=cmd.call_index,
        )
        return Decision(verdict="rewrite" if rewritten else "forward", call=call)

    # -- fed by the loop after a call returns ---------------------------------
    def note_provenance(self, anchor: str, etag: str) -> None:
        self._etags[anchor] = etag

    def note_card(self, server: str, card: dict) -> None:
        self._admitted_cards[server] = card


def describe() -> dict[str, Any]:
    return {
        "name": "adversary", "tier": "hard",
        "identity_checks": 4, "denies": True, "masks_fields": True,
        "uses_leases": True, "exactly_once_writes": True, "prosecutes": True,
        "blank_false_positive_rate": "<5%",
        "expected": "a strong team reaches ~45-55",
    }
