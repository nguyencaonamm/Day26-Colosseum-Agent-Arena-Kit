"""referee — the 17-class rubric and Gate 1, the deterministic claim
verifier (CONTRACTS.md section 6, FINAL-PLAN.md section 6).

This package is under active construction by several agents in parallel
(workspace hard rule 2): import only what you need directly from its
submodules (e.g. `from kit.referee.rubric import weight_of` or
`from kit.referee.verify import verify_claims`) rather than relying on
repackaged names here -- re-exporting sibling submodules at package-init
time would make `import referee.rubric` fail the moment any *other*
not-yet-written submodule under this package (a gate-2 adjudicator, a
ledger) has a transient import error. This mirrors `arena/__init__.py`'s own
documented reasoning in this repo.

Ships so far:
  - `referee.rubric` -- the 17 classes / 5 families / weights, `family_of`,
    `weight_of`, `DETERMINISTIC`, `NEEDS_ADJUDICATION`, and the false-claim
    penalty economics.
  - `referee.verify` -- Gate 1: schema validity, dedup, quota, evidence
    existence, and full deterministic resolution for the nine classes that
    never touch a model, plus a `DETECTORS` registry and
    `latent_violations()` for a collaborator's ledger to reuse rather than
    re-implement CONTRACTS.md section 6.4 a second time.
"""

from __future__ import annotations

__all__: list[str] = []
