# `adversary` — HARD

**You are meant to learn this one by probing it.** That is not a limitation of the
documentation; it is the skill the duel actually scores. Fire cards at it, read what came
back, form a theory, test the theory.

There is a `STRATEGY.md` next to this file. **Opening it before the tournament trades the
exercise for the answer.** It will still be there afterwards.

What is fair to tell you up front, because you would find it in one duel anyway:

- it holds **four separate identity checks**, because `identity`, `forged_card` and
  `faithless_peer` are three different attacks and no single check stops all three
- it routes on the **header**, never the body
- its writes are **exactly once**, with an `If-Match` and an idempotency key
- it **abstains** when the evidence is thin rather than guessing
- its quarantine is **calibrated, not twitchy**: refusing costs 0 credits, but being
  prosecuted into a false positive on a blank costs 8, so the threshold is set by that
  arithmetic

Its prosecutor files across all five families and has one thing worth stealing outright:
**a confidence gate at 44.4%**. A false claim costs `0.8 × weight`, so filing a claim of
weight *w* that is right with probability *p* is worth `w × (1.8p − 0.8)` — positive only
above `p > 0.8/1.8`. Below that line, filing **loses** points on average. Adversary stays
silent there. Operator has no gate and files whatever it noticed.

**Target:** a strong team reaches about 45–55.

> **Measured, after a real calibration bug was found and fixed.** An earlier harness
> admitted no A2A peer, so every card-checking gateway denied the one A2A call at
> *admission* — and the authorization check sitting behind admission never ran. Operator
> and Adversary then looked nearly identical (52 vs 49 damage), because the very check
> that distinguishes them was unreachable. Paired evaluation over 10 rounds x 3 seeds,
> after the fix:
>
> | bot | damage | denies | **authz** | **aud** | route | guard |
> |---|--:|--:|--:|--:|--:|--:|
> | rookie | 237 | 0 | 0 | 0 | 0 | 0 |
> | operator | 156 | 30 | **0** | **0** | 21 | 9 |
> | adversary | 111 | 39 | **6** | **3** | 21 | 9 |
>
> Route and guard are *identical*. The entire separation is the two checks Operator is
> designed to lack. Run it yourself: `python ladder.py --rounds 10 --seeds 3`.
