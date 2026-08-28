"""kit.broker — MockBroker + FrozenBroker ONLY. No live client here.

CONTRACTS.md section 9 names three ``Broker`` implementations sharing one
``query(messages, **kw) -> dict`` interface: ``LiveBroker`` (arena only, a
real DeepSeek client — never present in this package or anywhere in this
repository), ``FrozenBroker`` (replay by canonical prompt hash), and
``MockBroker`` (deterministic scripted policies, kit only, zero-key).

THE INVARIANT THIS PACKAGE IS BUILT TO SATISFY (FINAL-PLAN.md section 2.1):

    The student kit contains no HTTP client that can reach a model
    endpoint, and no code path that reads an API key. ``spar.py`` runs
    entirely on ``MockBroker``. A CI gate greps both repos for ``sk-`` and
    asserts ``kit/`` imports no live broker.

``tests/test_brokers.py`` enforces the stronger form of that gate
specifically for this package: an AST walk over every module here proving
none imports ``socket``/``ssl``/``http``/``urllib`` (or anything named
``live``), and a source scan proving no code path reads
``os.environ``/``os.getenv`` at all — not merely that ``DEEPSEEK_API_KEY``
does not appear as a literal, which a renamed lookup could dodge.

Public surface::

    from kit.broker import Broker, MockBroker, FrozenBroker, FrozenMissError
    from kit.broker import PERSONAS, select_persona
    from kit.broker import canonical_prompt_hash

Each submodule is self-contained and already complete (this package does
not depend on any sibling that might still be under construction), so no
graceful-degradation import guard is needed here — unlike ``kit/__init__.py``
and ``kit/mcp/__init__.py``, which do guard against collaborators' files
that may not exist yet.
"""

from __future__ import annotations

from kit.broker.base import (
    Broker,
    canonical_prompt_hash,
    canonicalize_message,
    final_message,
    make_tool_call,
    tool_call_message,
    validate_broker_message,
)
from kit.broker.frozen import BundleFormatError, FrozenBroker, FrozenMissError
from kit.broker.mock import PERSONAS, MockBroker, select_persona

__all__ = [
    "Broker",
    "validate_broker_message",
    "final_message",
    "tool_call_message",
    "make_tool_call",
    "canonical_prompt_hash",
    "canonicalize_message",
    "MockBroker",
    "PERSONAS",
    "select_persona",
    "FrozenBroker",
    "FrozenMissError",
    "BundleFormatError",
]


if __name__ == "__main__":
    print("=== kit.broker public surface ===")
    for name in __all__:
        print(f"  {name}")
    assert {"Broker", "MockBroker", "FrozenBroker", "FrozenMissError"} <= set(__all__)

    print("\n=== the invariant, checked live: no live-broker CODE anywhere in this package ===")
    print("  (AST-based — a docstring merely explaining the invariant, as this very file's")
    print("   module docstring does, must not itself trip the check; only real class")
    print("   definitions / imports count)")
    import ast
    import pathlib

    pkg_dir = pathlib.Path(__file__).parent
    py_files = sorted(pkg_dir.glob("*.py"))
    print(f"  modules: {[p.name for p in py_files]}")
    assert not any(p.name == "live.py" for p in py_files), "kit/broker/ must never contain a live.py"
    for p in py_files:
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                assert node.name != "LiveBroker", f"{p.name} defines a class named LiveBroker"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "live" not in alias.name.split("."), f"{p.name} imports {alias.name!r}"
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "live" not in node.module.split("."), f"{p.name} imports from {node.module!r}"
    print("  no live.py file, no LiveBroker class, no import naming 'live': OK")

    print("\nkit/broker/__init__.py import-and-export check passed.")
