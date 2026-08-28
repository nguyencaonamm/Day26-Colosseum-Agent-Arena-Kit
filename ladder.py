#!/usr/bin/env python3
"""ladder.py — paired defensive evaluation of the three bots. The G-LADDER calibration.

    python ladder.py             # the table
    python ladder.py --rounds 20 --seeds 5

WHY NOT JUST "DAMAGE TAKEN"
--------------------------------------------------------------------------------
Total damage over ten rounds is a fine player-facing headline and a bad ladder metric.
It mixes defender behaviour with the attacker's deck and lineup, the attacker's
prosecutor recall, false-claim recoil, round scaling, the 25-damage cap, and whether a
mutation happened to trigger at all. When the intended distinction between two tiers
rests on one or two cards, that noise buries it.

A ladder should measure **marginal protection**: which specific attacks does one policy
stop that the weaker policy does not? So this holds the cards, the asks, the prosecutor
and the seeds constant across all three bots and reports denials BY CATEGORY.

WHAT THIS FOUND, and it was a harness bug rather than a bot bug
--------------------------------------------------------------------------------
Before `spar.py` admitted the A2A peer, every card-checking gateway denied the single
A2A call at ADMISSION — so the authorization check sitting behind admission never ran.
Operator's whole designed flaw is that it authorizes from `ctx.sub` instead of
`ctx.act`, and a harness that stops the command one check earlier makes a correct
gateway and a confused one indistinguishable. Measured then: operator 52, adversary 49.

The fix was not to weaken adversary or bolt arbitrary denials onto operator — that
separates the numbers while corrupting the lesson. It was to let the attack REACH the
check it was written to test.
"""
from __future__ import annotations

import argparse
import collections
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import spar  # noqa: E402

CATEGORIES = ("authz", "aud", "route", "guard", "budget", "other")


def _category(reason: str) -> str:
    r = (reason or "").lower()
    if "owned by the learner" in r or "act" in r and "target" in r:
        return "authz"
    if "aud" in r:
        return "aud"
    if "body" in r or "header" in r:
        return "route"
    if "instruction" in r or "suspicious" in r:
        return "guard"
    if "allowance" in r or "budget" in r:
        return "budget"
    return "other"


def evaluate(bot: str, rounds: int, seeds: int) -> dict:
    world = spar._load_world()
    _, you_pr, you_deck, you_lineup = spar._load_side("you")
    cards = {c["id"]: c for c in you_deck["cards"]}
    gw, _, _, _ = spar._load_side(bot)

    totals = {"damage": 0, "denies": 0, **{c: 0 for c in CATEGORIES}}
    for seed in range(1, seeds + 1):
        for r in range(1, rounds + 1):
            card = cards[you_lineup[(r - 1) % len(you_lineup)]]
            res = spar._exchange("you", bot, gw, you_pr, card, world, r,
                                 random.Random(seed * 1000 + r), "learner:sv-0417")
            totals["damage"] += res["damage"]
            for e in res["trace"]:
                if e["type"] == "enforced" and e["p"].get("verdict_applied") == "deny":
                    totals["denies"] += 1
                    totals[_category(e["p"].get("reason"))] += 1
    return totals


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--seeds", type=int, default=1)
    a = ap.parse_args(argv)

    print(f"\n  PAIRED DEFENSIVE EVALUATION — identical cards, prosecutor and seeds")
    print(f"  {a.rounds} rounds x {a.seeds} seed(s)\n")
    hdr = f"  {'bot':<11}{'damage':>7}{'denies':>8}" + "".join(f"{c:>8}" for c in CATEGORIES)
    print(hdr); print("  " + "-" * (len(hdr) - 2))
    rows = {}
    for bot in ("rookie", "operator", "adversary"):
        t = evaluate(bot, a.rounds, a.seeds)
        rows[bot] = t
        print(f"  {bot:<11}{t['damage']:>7}{t['denies']:>8}"
              + "".join(f"{t[c]:>8}" for c in CATEGORIES))

    print("\n  MARGINAL PROTECTION — what adversary stops that operator does not:")
    op, ad = rows["operator"], rows["adversary"]
    for c in CATEGORIES:
        if ad[c] != op[c]:
            print(f"     {c:<8} operator {op[c]:>3}   adversary {ad[c]:>3}   "
                  f"({ad[c] - op[c]:+d})")
    monotone = rows["rookie"]["damage"] > op["damage"] > ad["damage"]
    print(f"\n  ladder monotone (rookie > operator > adversary damage): "
          f"{'YES' if monotone else 'NO — G-LADDER FAILS'}")
    return 0 if monotone else 1


if __name__ == "__main__":
    raise SystemExit(main())
