# adversary — STRATEGY (spoilers)

**Do not read this before the tournament.** Probing it is the exercise.

---

## The four identity checks, and why one is not enough

| # | Check | Stops |
|---|---|---|
| 1 | Agent Card admitted by the registry | `forged_card`, `shadow` |
| 2 | Skill actually declared on that card | a real peer invoked for a skill it never advertised |
| 3 | Delegation `aud` matches the server being called | `identity` (replayed or mis-scoped delegation) |
| 4 | **Target owned by `ctx.act`** | `authority_exceeded` — the confused deputy |

Check 4 is the one `operator` gets wrong, and it is the whole authorization lesson.
Authority derives from **`act`** (whom you serve), never **`sub`** (what you are).

## Budget as strategy, not hygiene

Round scale is ×1.0 (r1–3), ×1.25 (r4–7), ×1.5 (r8–10). A credit saved for round 9 buys
**1.5× the damage prevention** of one spent in round 2, so Adversary's per-round allowance
rises: 8 early, 12 late. Spending evenly is a mistake that looks like discipline.

## The confidence gate

`E = p·w − (1−p)·0.8w = w(1.8p − 0.8)`, positive iff `p > 44.4%` — **the same threshold
for every class**, which is exactly why the penalty scales with weight. Under a flat
penalty the break-even for a weight-10 class would be 28.6% and shotgunning the heavy
classes would be rational. It is not. There is no weight to shop for.

## What it deliberately does NOT do

It does not deny its way out of `schema_bomb`, `drift`, `poisoned_result` or
`faithless_peer` — those are beaten by narrow querying, pinning, guardrail refusal and
independent cross-checking respectively. A gateway that denied a `schema_bomb` would
simply be refusing to do its job. Check each card's declared `defense_event`: it tells
you what winning against that class actually looks like.
