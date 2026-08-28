"""kit.isolation — the real OS isolation boundary (CONTRACTS.md section 12).

**A monkey-patch is not a sandbox.** Codex review A2 was right that
wrapping ``open``/``pathlib.Path`` lets ``os.open``, ``subprocess``,
``ctypes``, and any alias captured before patching walk straight past the
patch. This package instead drives macOS's *kernel* sandbox
(``sandbox-exec``), measured on this exact machine (Darwin 25.6.0 /
Python 3.12.10) to deny every escape vector codex named — see
``kit.isolation.sandbox.probe_sandbox()``, which re-measures it live
rather than reporting a cached claim.

Public surface, re-exported for convenience::

    from kit.isolation import (
        # rpc.py — the wire protocol + the closed integrity taxonomy
        IntegrityKind, make_integrity, RpcRequest, RpcResponse,
        RpcFramingError, MethodNotAllowed, ALLOWED_METHODS,
        check_method, read_frame, write_frame,
        # sandbox.py — the OS boundary itself
        SandboxUnavailable, sandbox_exec_path, build_profile,
        run_sandboxed, classify_run, probe_sandbox,
        # child_driver.py — the process that runs INSIDE the sandbox
        run_probe_vectors, setup_probe_fixture, serve, load_target,
    )

Three files, one boundary:

- ``rpc.py`` — length-prefixed JSON frames over stdin/stdout, plus
  ``ALLOWED_METHODS = set(TOOL_SPECS)`` (CONTRACTS.md 12.2 mechanic 2):
  anything not a metered toolkit call is rejected, never executed.
- ``sandbox.py`` — builds the deny-model ``.sb`` profile (CONTRACTS.md
  12.1) and runs a child under it; ``probe_sandbox()`` is the runtime
  self-test that must pass before a ranked match is allowed to start.
- ``child_driver.py`` — the process ``sandbox-exec`` actually execs: the
  hostile self-test body (``--probe``) and the RPC serve loop that runs
  one team's artifact (``--serve``).

Stdlib only. No network, no randomness, no wall-clock.
"""

from __future__ import annotations

from kit.isolation.rpc import (
    ALLOWED_METHODS,
    IntegrityKind,
    MethodNotAllowed,
    RpcFramingError,
    RpcRequest,
    RpcResponse,
    check_method,
    make_integrity,
    read_frame,
    reject,
    write_frame,
)
from kit.isolation.sandbox import (
    SandboxUnavailable,
    TIMEOUT_RETURNCODE,
    build_profile,
    classify_run,
    probe_sandbox,
    run_sandboxed,
    sandbox_exec_path,
)
from kit.isolation.child_driver import (
    CORPUS_FILE,
    EXPECTED_DENIED,
    RUNS_FILE,
    SUBMISSIONS_FILE,
    load_target,
    run_probe_vectors,
    serve,
    setup_probe_fixture,
)

__all__ = [
    # rpc.py
    "ALLOWED_METHODS",
    "IntegrityKind",
    "MethodNotAllowed",
    "RpcFramingError",
    "RpcRequest",
    "RpcResponse",
    "check_method",
    "make_integrity",
    "read_frame",
    "reject",
    "write_frame",
    # sandbox.py
    "SandboxUnavailable",
    "TIMEOUT_RETURNCODE",
    "build_profile",
    "classify_run",
    "probe_sandbox",
    "run_sandboxed",
    "sandbox_exec_path",
    # child_driver.py
    "CORPUS_FILE",
    "EXPECTED_DENIED",
    "RUNS_FILE",
    "SUBMISSIONS_FILE",
    "load_target",
    "run_probe_vectors",
    "serve",
    "setup_probe_fixture",
]


if __name__ == "__main__":
    print(f"kit.isolation: {len(__all__)} public names across rpc.py / sandbox.py / child_driver.py\n")
    for name in __all__:
        print(f"  {name}")

    print(f"\nALLOWED_METHODS ({len(ALLOWED_METHODS)} metered toolkit calls):")
    for pair in sorted(ALLOWED_METHODS):
        print(f"  {pair[0]}.{pair[1]}")

    print("\nsandbox-exec on this machine:", sandbox_exec_path() or "NOT FOUND")

    print("\nRunning the live probe_sandbox() self-test (see kit.isolation.sandbox for the full table)...")
    report = probe_sandbox()
    print(f"  ok={report['ok']}  reason={report['reason']}")
    assert set(EXPECTED_DENIED) == set(report["vectors"]) or report["sandbox_exec"] is None, (
        "probe_sandbox() must report every vector child_driver.EXPECTED_DENIED names"
    )

    print("\nkit/isolation/__init__.py import-and-export check passed.")
    raise SystemExit(0 if (report["sandbox_exec"] is None or report["ok"]) else 1)
