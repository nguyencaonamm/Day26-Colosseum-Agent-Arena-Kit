"""bots/rookie/gateway.py — the EASY bot's control plane.

Rookie is what an agent looks like when nobody thought about infrastructure. It is
short on purpose: every line of it is a decision a real team has to make and Rookie
declines to make. If you cannot beat Rookie, you have a bug, not a strategy.

WHAT IT DOES WRONG, all four deliberate:

  1. IT NEVER DENIES ANYTHING. `decide()` returns "forward" for every command it is
     ever shown, including a write dressed as a read. Every `header_spoof`,
     `identity` and `authority_exceeded` attack in the game lands on it, free.
  2. IT NEVER MASKS A FIELD. Every call goes out with `fields=("*",)`, so it pays the
     maximum price for every single call. CONTRACTS.md 3.4: cost is
     `base + sum(field_weight) + rows * row_weight`, and `("*",)` expands to
     every field there is.
  3. IT LISTS INSTEAD OF SEARCHING. Faced with "where is X", it reaches for the
     catalog — `registry.list_servers`, `glossary.list_terms` — which are the two
     most expensive calls in the economy and the ones `schema_bomb` inflates.
  4. IT IGNORES LEASES. `slides.get_frame` needs a live `lease_id` minted by a recent
     `slides.query` (CONTRACTS.md 3.3). Rookie forwards `get_frame` with
     `lease_id=None` and eats `lease_required` errors — which are CHARGED.

The result is not that Rookie plays badly. It is that ROOKIE GOES BANKRUPT: a rookie
round costs roughly 49 credits against a 100-credit pool for the whole ten-round duel,
so it is broke partway through round 3 and spends the rest of the duel unable to
ground anything it says.

That is the acceptance test for the whole world design, embodied as an opponent: a
pure-RAG agent does not lose here because we rigged the scoring against it. It loses
because IT CANNOT AFFORD ITSELF.
"""
from __future__ import annotations

from typing import Any

from agent.gateway import Command, Decision, GatewayContext

try:
    from kit.mcp.types import ToolCall
except ImportError:  # pragma: no cover
    ToolCall = None  # type: ignore[assignment]


class Gateway:
    """Forwards everything, masks nothing, learns nothing."""

    #: Rookie's idea of research: dump the catalog and read it all.
    PREFERRED_DISCOVERY = ("registry", "list_servers")

    def __init__(self, ctx: GatewayContext) -> None:
        self.ctx = ctx
        self.forwarded = 0
        self.denied = 0  # stays 0 for the whole duel. That is the point.

    def decide(self, cmd: Command) -> Decision:
        # Mistake 2 + 4: widest possible field mask, and never attach a lease even
        # when the tool requires one. Both are pure cost, paid every single call.
        call = ToolCall(
            server=cmd.server,
            tool=cmd.tool,
            args=dict(cmd.args),
            fields=("*",),
            headers=dict(cmd.headers),
            lease_id=None,
            call_index=cmd.call_index,
        )
        self.forwarded += 1
        # Mistake 1: no admission check, no authorization check, no budget check.
        # There is not even an `if` here — that absence IS the lesson.
        return Decision(verdict="forward", call=call)


def describe() -> dict[str, Any]:
    """Machine-readable self-description, used by spar.py and the deck validator."""
    return {
        "name": "rookie",
        "tier": "easy",
        "denies": False,
        "masks_fields": False,
        "uses_leases": False,
        "prosecutes": False,
        "expected": "bankrupt during round 3; loses clearly to any agent that masks fields",
    }
