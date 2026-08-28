"""kit.mcp — the tool contract (CONTRACTS.md section 3): the closed error
taxonomy, the shared request/result types, and (once a collaborator has
written it) the per-tool cost table.

Public surface, re-exported for convenience:

    from kit.mcp import ErrorCode, ErrorSpec, ERROR_SPECS, make_error
    from kit.mcp import ToolCall, ToolResult, canonicalise_fields

``errors.py`` and ``types.py`` are this package's own files and always
import cleanly. ``specs.py`` (CONTRACTS.md 3.4's ``TOOL_SPECS`` data table
and ``cost_of()``) belongs to a collaborator and may not exist yet while
this file is read — per the workspace's file-ownership rule, that import
is best-effort: if it is missing (or only partially written), this
package degrades gracefully rather than failing to import at all.
"""

from __future__ import annotations

import logging

from kit.mcp.errors import ErrorCode, ErrorSpec, ERROR_SPECS, make_error
from kit.mcp.types import ToolCall, ToolResult, canonicalise_fields

__all__ = [
    "ErrorCode",
    "ErrorSpec",
    "ERROR_SPECS",
    "make_error",
    "ToolCall",
    "ToolResult",
    "canonicalise_fields",
]

try:
    from kit.mcp.specs import TOOL_SPECS, cost_of  # type: ignore[import-not-found]
except (ImportError, AttributeError) as exc:  # pragma: no cover - collaborator file
    logging.getLogger(__name__).debug(
        "kit.mcp.specs not available yet (%s) — TOOL_SPECS/cost_of omitted from kit.mcp", exc
    )
else:
    TOOL_SPECS  # noqa: B018 - referenced only to satisfy static checkers
    __all__ += ["TOOL_SPECS", "cost_of"]


if __name__ == "__main__":
    print("=== kit.mcp public surface ===")
    for name in __all__:
        print(f"  {name}")
    assert {"ErrorCode", "ErrorSpec", "ERROR_SPECS", "make_error"} <= set(__all__)
    assert {"ToolCall", "ToolResult", "canonicalise_fields"} <= set(__all__)
    if "TOOL_SPECS" in __all__:
        print("\n  kit.mcp.specs is available: TOOL_SPECS/cost_of are exported too.")
    else:
        print(
            "\n  kit.mcp.specs is not available yet (a collaborator's file) — "
            "degraded gracefully, everything else still imports."
        )
    print("\nkit/mcp/__init__.py import-and-export check passed.")
