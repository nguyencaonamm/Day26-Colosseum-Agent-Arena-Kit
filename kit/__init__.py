"""kit — the COLOSSEUM student kit (Day 26, AI20K).

The public package the student-facing loop, gateway, and referee-facing
harness import from. Subpackages own their own public surface:

    kit.world   — the frozen VLearnPedia index artifact (CONTRACTS.md 2)
    kit.mcp     — the tool contract: errors, request/result types, cost
                  table (CONTRACTS.md 3)
    kit.broker  — the A2A broker
    kit.isolation — the OS sandbox boundary (CONTRACTS.md 12)
    kit.loop    — the reference agent loop
    kit.referee — trace/claim/verdict machinery (CONTRACTS.md 5, 6)
    kit.arena_ui — the spectator-facing rendering

This top-level ``__init__.py`` deliberately does not import any of them
eagerly: several are still being written by other agents in parallel, and
a partially-written sibling subpackage must never make `import kit` itself
fail. Import the subpackage you need directly, e.g. ``from kit.mcp import
ToolCall`` — each subpackage is responsible for its own graceful
degradation when one of *its* dependencies is not ready yet.
"""

from __future__ import annotations

__all__: list[str] = []


if __name__ == "__main__":
    import importlib
    import pkgutil

    print("=== kit package: subpackage availability probe ===")
    import kit as _kit

    for info in sorted(pkgutil.iter_modules(_kit.__path__), key=lambda i: i.name):
        name = f"kit.{info.name}"
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - this is a diagnostic probe, not scored code
            print(f"  {name:16} NOT YET IMPORTABLE ({type(exc).__name__}: {exc})")
        else:
            print(f"  {name:16} OK")

    print("\n`import kit` itself never depends on any subpackage being ready.")
    print("kit/__init__.py check passed.")
