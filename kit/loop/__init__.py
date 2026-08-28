"""kit.loop — the vendored control loop (this task's brief: "the provided
agent control loop — vendored in the spirit of mini-swe-agent (MIT, ~220
readable lines)"). PROVIDED. Read it, do not edit it — RULES.md section 1's
hash gate rejects a submission that has.

Public surface, re-exported for convenience:

    from kit.loop import Model, Environment, Agent, RunResult
    from kit.loop import ActionParseError, canonicalise_action, extract_action_fence
    from kit.loop import Limits, LoopTerminated, LimitsExceeded, TimeExceeded, FormatError
    from kit.loop import SYSTEM_PROMPT, render_system_prompt

Three files, one seam (CONTRACTS.md section 4):

  agent.py    the loop itself (`Agent`), the `Model`/`Environment` duck-typed
              protocols, and the canonicaliser (raw model text -> a
              Command-shaped dict). DELETE-BY-DESIGN: no shell, no
              subprocess, no LocalEnvironment — see agent.py's module
              docstring before touching this package.
  limits.py   `Limits` (step/cost/wall-time/format-error caps) and the three
              termination exceptions, each stamping a closed `exit_status`.
  prompt.py   the harness's own system prompt template, describing the text
              action grammar and tool surface — never a JSON tool-calling
              schema (deliberate; see prompt.py's module docstring).
"""

from __future__ import annotations

from kit.loop.agent import (
    A2A_PEERS,
    ACTION_GRAMMAR,
    DISCOVERY_TOOLS,
    VERBS,
    ActionParseError,
    Agent,
    Environment,
    Model,
    RunResult,
    canonicalise_action,
    extract_action_fence,
    render_observation,
)
from kit.loop.limits import (
    FormatError,
    Limits,
    LimitsExceeded,
    LoopTerminated,
    TimeExceeded,
)
from kit.loop.prompt import EXAMPLE_ACTIONS, SYSTEM_PROMPT, render_system_prompt

__all__ = [
    # agent.py
    "Model",
    "Environment",
    "ActionParseError",
    "RunResult",
    "Agent",
    "A2A_PEERS",
    "DISCOVERY_TOOLS",
    "VERBS",
    "ACTION_GRAMMAR",
    "extract_action_fence",
    "canonicalise_action",
    "render_observation",
    # limits.py
    "Limits",
    "LoopTerminated",
    "LimitsExceeded",
    "TimeExceeded",
    "FormatError",
    # prompt.py
    "EXAMPLE_ACTIONS",
    "SYSTEM_PROMPT",
    "render_system_prompt",
]


if __name__ == "__main__":
    print("=== kit.loop public surface ===")
    for name in __all__:
        print(f"  {name}")
    assert {"Model", "Environment", "Agent", "RunResult"} <= set(__all__)
    assert {"Limits", "LoopTerminated", "LimitsExceeded", "TimeExceeded", "FormatError"} <= set(__all__)
    assert {"SYSTEM_PROMPT", "render_system_prompt"} <= set(__all__)
    print(f"\n  {len(__all__)} public names, all import cleanly.")

    print("\n=== one canonicalisation + one Limits(), through the package's own surface ===")
    action = canonicalise_action("DISCOVER registry.list_servers fields=name", call_index=0)
    print(f"  canonicalise_action(...) -> {action}")
    assert action["kind"] == "discover"
    limits = Limits()
    print(f"  Limits() -> {limits}")
    assert limits.step_limit == 4

    print("\nkit/loop/__init__.py import-and-export check passed.")
