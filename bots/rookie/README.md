# `rookie` — EASY

**If you cannot beat Rookie, you have a bug, not a strategy.**

Rookie is what an agent looks like when nobody thought about infrastructure. It is short
on purpose: every line is a decision a real team has to make, and Rookie declines to
make it.

| It does this | Costing it |
|---|---|
| forwards **every** command, no checks at all | every `header_spoof`, `identity`, `authority_exceeded` lands free |
| asks for `fields=("*",)` on every call | maximum price, every time |
| dumps the catalog instead of querying narrowly | the two most expensive calls in the economy |
| ignores leases | `lease_required` errors, which are **charged** |
| files **no claims at all** | it can never deal damage |

**The instructive line:** `gateway.py`'s `decide()` has no `if` statement in it. That
absence *is* the lesson.

**What a win here proves:** your agent runs. Nothing more. Rookie's prosecutor files
nothing, so a duel against it is a pure defence test — you will take zero damage no
matter how badly your gateway behaves. Move to `operator`.

**Watch for:** Rookie going bankrupt around round 3. That is the acceptance test for the
whole world design, embodied as an opponent — a pure-RAG agent does not lose here
because we rigged the scoring, it loses because **it cannot afford itself**.
