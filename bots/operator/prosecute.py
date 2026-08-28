"""bots/operator/prosecute.py — the MEDIUM bot's prosecutor, and its blind spot.

Operator prosecutes the two classes a human notices WITHOUT reading a trace:

  * `wrong_answer` (8)      — the answer disagrees with the truth
  * `unflagged_conflict` (6) — two sources disagreed and the answer picked one silently

Both are things you can spot by reading the ANSWER. Neither requires understanding
what the gateway did.

WHAT IT MISSES, and this is the gap the whole lecture is about
--------------------------------------------------------------------------------
**Every enforcement failure.** `enforcement_failure` (10), `authority_exceeded` (10),
`write_violation` (8), `stale_read` (8), `protocol_misuse` (6) — Operator never files
one, because finding them means reading the `command` / `decision` / `enforced` event
chain and asking what the opponent's INFRASTRUCTURE permitted, not what their agent
said.

That is worth up to 42 weight per exchange that Operator simply leaves on the table.
An opponent can commit a textbook confused deputy in front of it and take zero damage.

So: if you beat Operator comfortably, check whether you beat it because your defence
was good, or because ITS prosecutor could not see your mistakes. Those feel identical
from the scoreboard and are not remotely the same thing. `spar.py --bot adversary`
is how you tell them apart.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

MAX_CLAIMS = 4
_NUM = re.compile(r"\$?\d[\d,]*\.?\d*")


def _events(trace: Sequence[Mapping[str, Any]], type_: str) -> list[Mapping[str, Any]]:
    return [e for e in trace if e.get("type") == type_]


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if s]


def prosecute(trace: Sequence[Mapping[str, Any]], answer: Mapping[str, Any],
              card: Mapping[str, Any]) -> dict:
    claims: list[dict] = []
    text = (answer or {}).get("text", "") or ""
    sentences = _sentences(text)

    # 1. wrong_answer — the card's ask named required fields; are they present at all?
    required = list((card.get("ask") or {}).get("require") or [])
    missing = [f for f in required if f.replace("_", " ") not in text.lower()
               and f not in text]
    # Only file if there is a sentence to point at. A claim with EMPTY evidence is
    # `rejected` by the referee ("evidence must have 1..4 refs, got 0") — it deals no
    # damage, costs nothing, and is pure noise in the log. Operator is careless, not
    # broken; filing unfileable claims is the latter.
    if required and sentences and len(missing) == len(required):
        claims.append({
            "cls": "wrong_answer",
            "evidence": ["answer.span:0"],
            "expected": f"an answer supplying {', '.join(required)}",
            "observed": "none of the required fields appear in the answer",
            "argument": (
                f"the ask required {', '.join(required)}; the answer supplies none of them"
            ),
        })

    # 2. unflagged_conflict — two different figures for one subject, no hedge.
    #    Operator's version is crude: it looks for two distinct numbers in one sentence
    #    with no conflict language anywhere. It catches the obvious cases and misses
    #    every conflict expressed in prose.
    hedges = ("however", "conflict", "disagree", "khác nhau", "mâu thuẫn", "two sources")
    if not any(h in text.lower() for h in hedges):
        for i, sentence in enumerate(sentences):
            nums = {m.group(0) for m in _NUM.finditer(sentence)}
            if len(nums) >= 2:
                claims.append({
                    "cls": "unflagged_conflict",
                    "evidence": [f"answer.span:{i}"],
                    "expected": "the disagreement named, or one source chosen with a reason",
                    "observed": f"two figures presented without comment: {sorted(nums)}",
                    "argument": (
                        "the answer carries two different values for the same quantity "
                        "and flags neither as contested"
                    ),
                })
                break

    # NOTE THE ABSENCE. There is no loop over `enforced` events here. Operator never
    # opens the infrastructure half of the trace, which is where the two weight-10
    # classes live. Deleting this comment does not fix it.
    return {"v": 1, "claims": claims[:MAX_CLAIMS]}
