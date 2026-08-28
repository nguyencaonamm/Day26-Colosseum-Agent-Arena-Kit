"""kit/loop/limits.py — the loop's own termination limits (this task's brief;
not a CONTRACTS.md-numbered section, but built to sit under CONTRACTS.md
section 0's "no wall-clock in scored code" / section 11's determinism gate).

Four limits, four reasons an exchange ends without an answer:

  step_limit                   4 model iterations per exchange (FINAL-PLAN.md
                                section 5.1: "a 4-iteration exchange").
  cost_limit                   the duel credit pool (100 per side, across all
                                10 rounds — CONTRACTS.md section 4.2's
                                `GatewayContext.credits`, FINAL-PLAN.md
                                section 4 / 4.1). The loop's own copy of this
                                number is a SAFETY NET, not the authority:
                                the arena's trusted envelope is what actually
                                meters credits (CONTRACTS.md section 4), and
                                each `Observation` it hands back may report
                                the true `credits_left` — kit/loop/agent.py
                                trusts that figure when present and only
                                falls back to summing observed `cost` when it
                                is absent (e.g. a bare-bones test Environment).
                                100 is deliberately generous for a SINGLE
                                exchange (a disciplined round costs ~8-11 cr
                                per FINAL-PLAN.md 4.3) — it exists to catch a
                                runaway or malformed Environment, not to bind
                                in ordinary play.
  wall_time_limit               20 s exchange deadline (FINAL-PLAN.md section
                                10: "The exchange deadline drops from 45 s to
                                20 s"). Measured via `time.monotonic()` deltas
                                from exchange start, per an explicit ban on
                                wall-clock reads in scored code.
  max_consecutive_format_errors 1 — one repair prompt, then fail. The FIRST
                                unparseable model turn earns a nudge back
                                toward the grammar; a SECOND unparseable turn
                                in a row (the counter resets on any valid
                                action) ends the exchange.

Each limit's violation raises a distinct, named exception rather than
returning a sentinel — "Termination raises LimitsExceeded / TimeExceeded /
FormatError, each stamping an exit_status that is clean to score against"
(this task's brief). `exit_status` is a short, closed, machine-readable
string — a scorer switches on it, never parses the message text.

Stdlib only. No network, no randomness, no wall-clock (this module doesn't
read a clock itself — it validates and carries caps; kit/loop/agent.py does
the actual `time.monotonic()` reads, injectable for deterministic tests).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

__all__ = [
    "Limits",
    "LoopTerminated",
    "LimitsExceeded",
    "TimeExceeded",
    "FormatError",
]


@dataclass(frozen=True, slots=True)
class Limits:
    """The four caps, together. Every field has the default named in this
    task's brief; a caller (a test, a bot difficulty tier, a future
    reconfiguration) may override any subset via keyword arguments —
    validated identically either way."""

    step_limit: int = 4
    cost_limit: int = 100
    wall_time_limit: float = 20.0
    max_consecutive_format_errors: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.step_limit, int)
            or isinstance(self.step_limit, bool)
            or self.step_limit < 1
        ):
            raise ValueError(f"Limits.step_limit must be a positive int, got {self.step_limit!r}")
        if (
            not isinstance(self.cost_limit, int)
            or isinstance(self.cost_limit, bool)
            or self.cost_limit < 0
        ):
            raise ValueError(f"Limits.cost_limit must be a non-negative int, got {self.cost_limit!r}")
        if (
            not isinstance(self.wall_time_limit, (int, float))
            or isinstance(self.wall_time_limit, bool)
            or self.wall_time_limit <= 0
        ):
            raise ValueError(
                f"Limits.wall_time_limit must be a positive number of seconds, got {self.wall_time_limit!r}"
            )
        if (
            not isinstance(self.max_consecutive_format_errors, int)
            or isinstance(self.max_consecutive_format_errors, bool)
            or self.max_consecutive_format_errors < 0
        ):
            raise ValueError(
                "Limits.max_consecutive_format_errors must be a non-negative int, "
                f"got {self.max_consecutive_format_errors!r}"
            )
        # Normalise wall_time_limit to float so downstream f"{...:.3f}"
        # formatting and comparisons against time.monotonic() deltas never
        # trip over an int/float mismatch.
        object.__setattr__(self, "wall_time_limit", float(self.wall_time_limit))


class LoopTerminated(Exception):
    """Base class for every exception that ends an exchange without the
    model completing it on its own terms. `exit_status` is the closed,
    stable string a scorer keys on; `detail` carries whatever numbers
    explain the call (limit name, observed value, cap, elapsed seconds,
    ...) for logging or an `integrity`/terminal L1 event — never free text
    baked into a message a scorer would have to re-parse.
    """

    exit_status: ClassVar[str] = "error"

    def __init__(self, message: str, **detail: object) -> None:
        super().__init__(message)
        self.detail: dict[str, object] = dict(detail)

    def to_dict(self) -> dict[str, object]:
        """A flat, JSON-serialisable summary: `{"exit_status", "message",
        **detail}`. Convenient for a caller that wants to log or fold this
        termination into a trace event without touching the exception
        object's Python-only attributes."""
        return {"exit_status": self.exit_status, "message": str(self), **self.detail}


class LimitsExceeded(LoopTerminated):
    """`step_limit` or `cost_limit` was hit. `detail["limit"]` is
    `"step"` or `"cost"` so a consumer can tell the two apart without
    string-matching the message; `detail["value"]`/`detail["cap"]` are the
    observed count and the configured cap."""

    exit_status: ClassVar[str] = "limits_exceeded"


class TimeExceeded(LoopTerminated):
    """`wall_time_limit` was hit. `detail["elapsed"]`/`detail["cap"]` are
    both `time.monotonic()`-based seconds, never wall-clock."""

    exit_status: ClassVar[str] = "time_exceeded"


class FormatError(LoopTerminated):
    """`max_consecutive_format_errors` was exceeded — the model failed to
    produce a parseable action twice in a row (after its one repair
    prompt). `detail["consecutive"]`/`detail["cap"]` name the run length
    and the configured tolerance."""

    exit_status: ClassVar[str] = "format_error"


if __name__ == "__main__":
    print("=== kit.loop.limits: defaults (this task's brief) ===")
    defaults = Limits()
    print(f"  {defaults}")
    assert defaults.step_limit == 4
    assert defaults.cost_limit == 100
    assert defaults.wall_time_limit == 20.0
    assert defaults.max_consecutive_format_errors == 1

    print("\n=== Overriding a subset (e.g. a tighter test/bot profile) ===")
    tight = Limits(step_limit=2, wall_time_limit=5.0)
    print(f"  {tight}")
    assert tight.step_limit == 2 and tight.cost_limit == 100 and tight.wall_time_limit == 5.0

    print("\n=== Rejection demo (each must raise ValueError) ===")

    def _expect_value_error(label: str, fn) -> None:
        try:
            fn()
        except ValueError as exc:
            print(f"  [{label:32}] -> ValueError: {exc}")
        else:
            raise AssertionError(f"expected ValueError for case {label!r}")

    _expect_value_error("step_limit == 0", lambda: Limits(step_limit=0))
    _expect_value_error("step_limit is a bool", lambda: Limits(step_limit=True))
    _expect_value_error("cost_limit < 0", lambda: Limits(cost_limit=-1))
    _expect_value_error("wall_time_limit <= 0", lambda: Limits(wall_time_limit=0.0))
    _expect_value_error(
        "max_consecutive_format_errors < 0", lambda: Limits(max_consecutive_format_errors=-1)
    )

    print("\n=== The three termination exceptions: exit_status + to_dict() ===")
    cases = [
        LimitsExceeded("step budget exhausted: 4 >= 4", limit="step", value=4, cap=4),
        LimitsExceeded("duel credit pool exhausted: spent 132 > 100", limit="cost", value=132, cap=100),
        TimeExceeded("exchange wall-clock deadline exceeded: 21.4s > 20.0s", elapsed=21.4, cap=20.0),
        FormatError("2 consecutive malformed turns (cap 1)", consecutive=2, cap=1),
    ]
    for exc in cases:
        d = exc.to_dict()
        print(f"  {type(exc).__name__:14} exit_status={d['exit_status']!r:18} detail={exc.detail}")
        assert d["exit_status"] == exc.exit_status
        assert d["message"] == str(exc)
        assert set(d) == {"exit_status", "message", *exc.detail}

    assert LimitsExceeded.exit_status == "limits_exceeded"
    assert TimeExceeded.exit_status == "time_exceeded"
    assert FormatError.exit_status == "format_error"
    assert issubclass(LimitsExceeded, LoopTerminated)
    assert issubclass(TimeExceeded, LoopTerminated)
    assert issubclass(FormatError, LoopTerminated)

    print("\nAll limits.py demos passed.")
