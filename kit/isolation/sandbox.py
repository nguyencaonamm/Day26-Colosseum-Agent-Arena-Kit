"""kit/isolation/sandbox.py — the real OS isolation boundary (CONTRACTS.md
section 12, FINAL-PLAN.md section 11).

**Why this exists instead of a Python-level wrapper.** Codex review A2 was
right: wrapping ``open``/``pathlib.Path`` is not a sandbox. ``os.open``,
``subprocess``, ``ctypes``, and any alias captured before patching all walk
straight past a monkey-patch, so a "G-JAIL" test built on one would pass
its own name while the property it claims stayed false. macOS ships a
*kernel* sandbox — ``sandbox-exec`` — and CONTRACTS.md 12 records it
measured, on this exact machine (Darwin 25.6.0 / Python 3.12.10), blocking
every one of those vectors. This module is that measurement made
re-runnable: :func:`probe_sandbox` does not report a cached claim, it
re-executes the hostile child (``kit.isolation.child_driver --probe``)
under a freshly generated profile and reports what actually happened,
right now, on whatever machine is running it.

**Deny-model, not allow-model** (CONTRACTS.md 12.1, quoted): "an allow-model
profile could not start CPython without enumerating the whole runtime, and
a profile that fails to start is a profile someone disables on the day."
:func:`build_profile` opens with ``(allow default)`` and layers exactly the
denials CONTRACTS.md 12.1 names on top — nothing more, nothing less. That
literal profile text is a frozen interface; this module is a producer of
it, not a place to redesign it.

**What still must hold, per CONTRACTS.md 12.2:**

1. a scratch working copy per duel — this module's ``duel_scratch``
   parameter, matched by ``file-write*`` allow;
2. the RPC allowlist — enforced entirely in ``kit.isolation.rpc``, this
   module never re-implements it;
3. every denial is a structured record, never a silent success — every
   function here that can observe a denial returns or raises something
   carrying one of ``kit.isolation.rpc``'s five closed ``IntegrityKind``
   values, via :func:`kit.isolation.rpc.make_integrity`.

**If ``sandbox-exec`` is missing**, :func:`probe_sandbox` reports
``ok=False`` with a named reason instead of silently downgrading to a
weaker guarantee — CONTRACTS.md 12.2.4: "the honest fallback is reviewed
submissions and no anti-cheat claim — never a Python wrapper pretending to
be a sandbox." The caller (the arena's match runner) is expected to refuse
to start a ranked match on ``ok=False``.

Stdlib only. No network, no unseeded randomness, no wall-clock (elapsed
time is measured with ``time.monotonic()`` deltas only, per workspace hard
rule 4 / CONTRACTS.md 0).
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

from kit.isolation import child_driver
from kit.isolation.rpc import IntegrityKind, make_integrity

__all__ = [
    "TIMEOUT_RETURNCODE",
    "SandboxUnavailable",
    "sandbox_exec_path",
    "build_profile",
    "run_sandboxed",
    "classify_run",
    "probe_sandbox",
]

# kit/isolation/sandbox.py -> kit/isolation -> kit -> Day26-Colosseum-Agent-Arena-Kit
REPO_ROOT = Path(__file__).resolve().parents[2]

# The canonical location on every macOS install this lab targets; PATH is
# checked as a fallback for a nonstandard install.
_CANONICAL_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

#: Sentinel returncode :func:`run_sandboxed` uses to signal "the child was
#: killed for exceeding its timeout" through the ``subprocess.CompletedProcess``
#: return type the function's contract promises — real exit codes are
#: 0-255 (or a negated signal number, i.e. as low as -31 on this platform),
#: so this value can never collide with a genuine one.
TIMEOUT_RETURNCODE = -9999


class SandboxUnavailable(RuntimeError):
    """Raised by :func:`run_sandboxed` when no ``sandbox-exec`` binary can
    be found at all. :func:`probe_sandbox` catches this itself and turns it
    into a structured ``ok=False`` report rather than letting it propagate,
    but a caller invoking :func:`run_sandboxed` directly sees it raised."""


def sandbox_exec_path() -> str | None:
    """The ``sandbox-exec`` binary to use, or ``None`` if this machine has
    none. Checks the canonical ``/usr/bin/sandbox-exec`` first (that is the
    path CONTRACTS.md 12 measured against), then falls back to ``PATH``."""
    canonical = Path(_CANONICAL_SANDBOX_EXEC)
    if canonical.is_file() and canonical.stat().st_mode & 0o111:
        return str(canonical)
    found = shutil.which("sandbox-exec")
    return found


def build_profile(arena_root: str | Path, duel_scratch: str | Path) -> str:
    """The ``.sb`` profile source, deny-model, exactly CONTRACTS.md 12.1's
    six rules with real resolved paths substituted for ``<ARENA_ROOT>`` /
    ``<DUEL_SCRATCH>``.

    ``duel_scratch`` need not be nested under ``arena_root`` — the two
    paths are independent ``subpath`` filters — but when it *is* nested
    (the expected layout: a per-duel scratch directory living under the
    arena root), the more specific ``allow file-write*`` on
    ``duel_scratch`` wins over the broader ``deny file-write*`` on
    ``arena_root`` regardless of declaration order; this was verified
    empirically on this machine (Seatbelt resolves path filters by
    specificity, not by rule order) before this function was written to
    rely on it.
    """
    resolved_arena = str(Path(arena_root).resolve())
    resolved_scratch = str(Path(duel_scratch).resolve())
    for label, p in (("arena_root", resolved_arena), ("duel_scratch", resolved_scratch)):
        if '"' in p:
            raise ValueError(f"{label} path must not contain a double-quote character (breaks the .sb string literal): {p!r}")

    return textwrap.dedent(
        f"""\
        ; Generated by kit.isolation.sandbox.build_profile — CONTRACTS.md 12.1.
        ; Deny-model: (allow default) first, then exactly the denials this duel needs.
        (version 1)
        (allow default)
        (deny network*)
        (deny file-write* (subpath "{resolved_arena}"))
        (allow file-write* (subpath "{resolved_scratch}"))
        (deny file-read* (subpath "{resolved_arena}/submissions"))
        (deny file-read* (subpath "{resolved_arena}/corpus_snapshot"))
        (deny file-read* (subpath "{resolved_arena}/runs"))
        """
    )


def run_sandboxed(
    profile_path: str | Path,
    argv: list[str],
    cwd: str | Path | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Run ``argv`` under ``sandbox-exec -f profile_path``, capturing text
    stdout/stderr. Always returns a ``subprocess.CompletedProcess`` — a
    real timeout is turned into one with :data:`TIMEOUT_RETURNCODE` rather
    than letting ``subprocess.TimeoutExpired`` escape, so every caller gets
    the one documented return type and checks ``.returncode`` (or calls
    :func:`classify_run`) instead of also needing a ``try/except`` for the
    timeout case specifically.

    Raises :class:`SandboxUnavailable` if no ``sandbox-exec`` binary exists
    on this machine — that failure mode is not disguised as a
    ``CompletedProcess``, because "the boundary tool itself is missing" is
    categorically different from "the boundary ran and something inside it
    timed out."
    """
    exe = sandbox_exec_path()
    if exe is None:
        raise SandboxUnavailable(
            "sandbox-exec was not found on this machine (checked "
            f"{_CANONICAL_SANDBOX_EXEC!r} and PATH). Per CONTRACTS.md 12.2.4, the honest "
            "fallback is reviewed submissions and no anti-cheat claim — never a weaker "
            "Python-level substitute."
        )
    full_argv = [exe, "-f", str(profile_path), *argv]
    try:
        return subprocess.run(
            full_argv,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            timeout=timeout,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        def _decode(b: object) -> str:
            if isinstance(b, (bytes, bytearray)):
                return b.decode("utf-8", "replace")
            return b or ""

        return subprocess.CompletedProcess(
            args=full_argv,
            returncode=TIMEOUT_RETURNCODE,
            stdout=_decode(exc.stdout),
            stderr=_decode(exc.stderr),
        )


def classify_run(cp: subprocess.CompletedProcess) -> dict | None:
    """``None`` unless ``cp`` is the synthetic timeout result
    :func:`run_sandboxed` returns, in which case the ``timeout``
    :class:`kit.isolation.rpc.IntegrityKind` record for it."""
    if cp.returncode == TIMEOUT_RETURNCODE:
        return make_integrity(IntegrityKind.TIMEOUT, f"child exceeded its timeout; argv={cp.args!r}")
    return None


def probe_sandbox(*, timeout: float = 25.0, keep_tmp: bool = False) -> dict:
    """CONTRACTS.md 12: "self-test: verify at runtime that the boundary
    actually holds on THIS machine, returning a per-vector pass/fail
    table." Builds a fresh throwaway ``arena_root``/``duel_scratch`` pair,
    execs ``python -m kit.isolation.child_driver --probe`` under a
    freshly-generated profile via :func:`run_sandboxed`, and compares what
    came back against :data:`kit.isolation.child_driver.EXPECTED_DENIED`.

    Returns::

        {"ok": bool,                 # True iff EVERY vector matched EXPECTED_DENIED
         "reason": str,              # human-readable — why ok is False, or a confirmation
         "sandbox_exec": str | None, # the binary used, or None if missing
         "darwin_release": str, "python_version": str,
         "vectors": {name: {"denied": bool, "detail": str, "kind": str|None}, ...},
         "integrity_events": [ {"kind": ..., "detail": ...}, ... ],  # one per denied vector
         "elapsed_s": float}

    ``ok=False`` (including "sandbox-exec is not installed here") is the
    signal a match runner checks before refusing to start a ranked match —
    this function never softens that into a warning.

    The socket vector is compared but never *alone* fails ``ok``: an
    offline grading machine can raise a non-``PermissionError`` ``OSError``
    for reasons that have nothing to do with the sandbox (see
    ``child_driver._probe_socket_connect``'s docstring), and conflating
    "no route to 1.1.1.1" with "the kernel boundary is broken" would be
    exactly the kind of softened, non-reproducible check CONTRACTS.md 12
    was written to rule out. Every other vector is load-bearing.
    """
    report: dict = {
        "ok": False,
        "reason": None,
        "sandbox_exec": sandbox_exec_path(),
        "darwin_release": platform.release(),
        "python_version": platform.python_version(),
        "vectors": {},
        "integrity_events": [],
        "elapsed_s": None,
    }

    if report["sandbox_exec"] is None:
        report["reason"] = (
            "sandbox-exec was not found on this machine — the OS isolation boundary CANNOT be "
            "verified. Per CONTRACTS.md 12.2.4: refuse to run a ranked match, do not fall back to "
            "a Python-level wrapper pretending to be a sandbox."
        )
        return report

    started = time.monotonic()
    tmp_root = Path(tempfile.mkdtemp(prefix="duel-probe-arena-"))
    try:
        arena_root = tmp_root / "arena"
        duel_scratch = arena_root / "scratch" / "probe-duel"
        child_driver.setup_probe_fixture(arena_root, duel_scratch)

        profile_src = build_profile(arena_root, duel_scratch)
        profile_path = tmp_root / "duel.sb"
        profile_path.write_text(profile_src, encoding="utf-8")

        argv = [
            sys.executable,
            "-m",
            "kit.isolation.child_driver",
            "--probe",
            "--arena-root",
            str(arena_root),
            "--duel-scratch",
            str(duel_scratch),
        ]
        cp = run_sandboxed(profile_path, argv, cwd=str(REPO_ROOT), timeout=timeout)

        timeout_record = classify_run(cp)
        if timeout_record is not None:
            report["reason"] = f"the sandboxed probe child exceeded its {timeout}s timeout"
            report["integrity_events"].append(timeout_record)
            return report

        if cp.returncode != 0:
            report["reason"] = (
                f"the sandboxed probe child exited {cp.returncode} instead of 0 — the profile "
                f"likely failed to even start CPython (CONTRACTS.md 12.1's warning about "
                f"allow-model profiles). stderr tail: {cp.stderr[-800:]!r}"
            )
            return report

        try:
            vectors: dict = json.loads(cp.stdout)
        except json.JSONDecodeError as exc:
            report["reason"] = f"could not parse the probe child's stdout as JSON: {exc}. stdout={cp.stdout[:800]!r}"
            return report

        report["vectors"] = vectors

        expected = child_driver.EXPECTED_DENIED
        missing = sorted(set(expected) - set(vectors))
        if missing:
            report["reason"] = f"the probe child did not report vector(s): {missing}"
            return report

        # Every vector except the network one is load-bearing (see docstring).
        mismatches = sorted(
            name
            for name, want in expected.items()
            if name != "socket_connect_denied" and vectors[name].get("denied") != want
        )
        for name, want in sorted(expected.items()):
            got_vector = vectors[name]
            if got_vector.get("denied") and got_vector.get("kind"):
                report["integrity_events"].append(
                    make_integrity(got_vector["kind"], f"{name}: {got_vector.get('detail', '')}")
                )

        socket_vector = vectors["socket_connect_denied"]
        socket_as_expected = socket_vector.get("denied") == expected["socket_connect_denied"]

        report["ok"] = not mismatches
        if report["ok"]:
            note = "" if socket_as_expected else " (socket vector was ambiguous on this machine — advisory only, not load-bearing)"
            report["reason"] = f"every load-bearing vector matched CONTRACTS.md section 12{note}"
        else:
            report["reason"] = f"vector(s) did NOT match CONTRACTS.md section 12's table: {mismatches}"
        return report
    finally:
        report["elapsed_s"] = time.monotonic() - started
        if not keep_tmp:
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    print("=== kit.isolation.sandbox: sandbox_exec_path() ===")
    exe = sandbox_exec_path()
    print(f"  {exe!r}")

    print("\n=== build_profile(): the generated .sb source ===")
    with tempfile.TemporaryDirectory() as td:
        demo_arena = Path(td) / "arena"
        demo_scratch = demo_arena / "scratch" / "demo-duel"
        demo_arena.mkdir()
        demo_scratch.mkdir(parents=True)
        profile = build_profile(demo_arena, demo_scratch)
        print(textwrap.indent(profile, "  "))
        assert "(deny network*)" in profile
        assert "(allow default)" in profile
        assert f'(allow file-write* (subpath "{demo_scratch.resolve()}"))' in profile
        for sub in ("submissions", "corpus_snapshot", "runs"):
            assert f'(deny file-read* (subpath "{demo_arena.resolve()}/{sub}"))' in profile
        print("  profile shape assertions passed")

        print("\n=== build_profile() rejects a path containing a double-quote ===")
        try:
            build_profile('/tmp/evil"; (allow default)', demo_scratch)
        except ValueError as exc:
            print(f"  ValueError: {exc}")
        else:
            raise AssertionError("expected ValueError for a double-quote-bearing path")

    print("\n=== probe_sandbox(): the real, live measurement on THIS machine ===")
    report = probe_sandbox()
    print(f"  sandbox_exec   = {report['sandbox_exec']}")
    print(f"  darwin_release = {report['darwin_release']}")
    print(f"  python_version = {report['python_version']}")
    print(f"  elapsed_s      = {report['elapsed_s']:.3f}" if report["elapsed_s"] is not None else "  elapsed_s      = None")
    print(f"  ok             = {report['ok']}")
    print(f"  reason         = {report['reason']}")
    print("\n  | vector | expected denied | got denied | detail |")
    print("  |---|---|---|---|")
    for name in sorted(report["vectors"]):
        v = report["vectors"][name]
        want = child_driver.EXPECTED_DENIED.get(name)
        mark = "OK" if v.get("denied") == want else "MISMATCH"
        print(f"  | {name} | {want} | {v.get('denied')} | {v.get('detail')!r}  [{mark}] |")
    print(f"\n  {len(report['integrity_events'])} integrity event(s) recorded:")
    for rec in report["integrity_events"]:
        print(f"    {rec}")

    if report["sandbox_exec"] is None:
        print(
            "\n*** sandbox-exec IS NOT AVAILABLE ON THIS MACHINE. The OS isolation boundary "
            "CANNOT be verified here. This is reported loudly, not passed as a weaker test. ***"
        )
        raise SystemExit(1)

    print(f"\n=== run_sandboxed(): a deliberate timeout, classified ===")
    with tempfile.TemporaryDirectory() as td:
        demo_arena = Path(td) / "arena"
        demo_scratch = demo_arena / "scratch" / "timeout-duel"
        child_driver.setup_probe_fixture(demo_arena, demo_scratch)
        profile_path = Path(td) / "duel.sb"
        profile_path.write_text(build_profile(demo_arena, demo_scratch), encoding="utf-8")
        cp = run_sandboxed(
            profile_path,
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=str(REPO_ROOT),
            timeout=1.0,
        )
        record = classify_run(cp)
        print(f"  returncode={cp.returncode}  classify_run() -> {record}")
        assert cp.returncode == TIMEOUT_RETURNCODE
        assert record is not None and record["kind"] == "timeout"

    print("\nAll kit/isolation/sandbox.py demos passed.")
    raise SystemExit(0 if report["ok"] else 1)
