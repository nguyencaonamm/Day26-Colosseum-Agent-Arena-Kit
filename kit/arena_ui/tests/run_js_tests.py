"""kit/arena_ui/tests/run_js_tests.py — runs reduce.test.js under Node and
reports its real output.

COLOSSEUM — core/decode.js and core/reduce.js are native ES modules with
no test framework of their own (vanilla JS: no npm, no build step — see
core/theme.js's header). This script is the bridge that lets the *Python*
test/CI story ("run everything with one command") also exercise the JS
core, without the shipped UI ever depending on Node: **Node is for JS TESTS
ONLY.** spar.html / projector.html run in a browser with the wifi down;
nothing under kit/arena_ui/core/ may require this script, and nothing this
script does is imported by anything else in the kit.

Discovery note: this repo's `pyproject.toml` scopes `[tool.pytest.ini_options]
testpaths = ["tests"]` to the top-level `tests/` directory, so a bare
`pytest` / `make test` will NOT sweep this file up on its own (it lives
under `kit/arena_ui/tests/`, deliberately alongside the JS tests it runs,
not the top-level Python suite). It is still a normal pytest module —
`pytest kit/arena_ui/tests/run_js_tests.py` (or `-k js_tests`) collects and
runs `test_js_tests_pass` exactly like any other test file named on the
command line. It is also directly runnable as a script:

    python kit/arena_ui/tests/run_js_tests.py

Node binary resolution order (first one found wins):
  1. the `NODE_BIN` environment variable, if set — an explicit override.
  2. the interpreter named in this lab's own task brief
     (`/Users/kites/.nvm/versions/node/v24.1.0/bin/node`), so the exact
     environment this suite was authored and verified against is tried
     next, before falling back to whatever a different machine happens to
     have on PATH.
  3. `node` resolved from PATH (`shutil.which`) — the portable case for
     anyone else's machine, including the public kit's own CI.
  4. Nothing found: SKIP (pytest) / print a clear message and exit 0
     (script mode) — a missing dev-only tool must never fail a build that
     has nothing to do with it; only Python is required to run this kit.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

try:
    import pytest
except ImportError:  # pragma: no cover - pytest is a dev dependency only
    pytest = None

_HERE = Path(__file__).resolve().parent
#: EVERY `*.test.js` beside this file, not a hardcoded one. The layout suite
#: was written after a screenshot caught the prosecution cut-in drawing over the
#: five-stage strip -- a defect no Python test could see -- and a runner pinned
#: to a single filename would have silently ignored it.
_TEST_FILES = sorted(_HERE.glob("*.test.js"))

# The exact interpreter this suite was authored and verified against
# (this lab's task brief: "Node (for JS TESTS ONLY, never required by the
# shipped UI)"). Tried before a bare `node` on PATH so a fresh clone on THIS
# box reproduces the same run without needing NODE_BIN set by hand.
_KNOWN_NODE = "/Users/kites/.nvm/versions/node/v24.1.0/bin/node"

_TIMEOUT_S = 60


def resolve_node_bin() -> str | None:
    """Return a usable `node` executable path, or None if none can be found."""
    import os

    env_bin = os.environ.get("NODE_BIN")
    if env_bin:
        # shutil.which() checks executability directly when the value
        # already contains a path separator (an absolute/relative path),
        # and searches PATH when it does not (a bare command name) — either
        # way this one call covers both forms $NODE_BIN might take.
        if shutil.which(env_bin):
            return env_bin
        # Still accept a real, if not-marked-executable, file rather than
        # silently falling through to a DIFFERENT interpreter than the one
        # the caller explicitly asked for.
        if Path(env_bin).is_file():
            return env_bin

    if Path(_KNOWN_NODE).is_file():
        return _KNOWN_NODE

    return shutil.which("node")


def run_js_tests() -> tuple[int, str, str, str | None]:
    """Run reduce.test.js under Node.

    Returns (returncode, stdout, stderr, node_bin_used). `node_bin_used` is
    None when no Node interpreter could be found at all — the caller treats
    that as "skip", not "fail": a missing dev-only tool is not a bug in the
    JS it would have tested.
    """
    node_bin = resolve_node_bin()
    if node_bin is None:
        return (0, "", "", None)

    if not _TEST_FILES:
        raise FileNotFoundError(f"no *.test.js found in {_HERE}")

    rc, out, err = 0, [], []
    for f in _TEST_FILES:
        proc = subprocess.run(
            [node_bin, str(f)],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            cwd=str(_HERE),
        )
        out.append(f"--- {f.name} ---\n{proc.stdout}")
        if proc.stderr:
            err.append(f"--- {f.name} ---\n{proc.stderr}")
        # Keep going after a failure: which suites fail is the useful signal.
        rc = rc or proc.returncode
    return (rc, "".join(out), "".join(err), node_bin)


def test_js_tests_pass():
    """pytest entry point: run the JS suite, print its real output, and fail
    the Python test if the JS suite reported any failure."""
    returncode, stdout, stderr, node_bin = run_js_tests()

    if node_bin is None:
        msg = (
            "no Node interpreter found (checked $NODE_BIN, "
            f"{_KNOWN_NODE!r}, and PATH) — skipping JS tests. "
            "The shipped UI never needs Node; this only affects running "
            "the JS suites in this dev environment."
        )
        if pytest is not None:
            pytest.skip(msg)
        else:  # pragma: no cover - exercised only without pytest installed
            print(f"SKIP: {msg}")
            return

    print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)

    assert returncode == 0, (
        f"a JS suite exited {returncode} under {node_bin} "
        f"— see the printed output above for which assertion failed"
    )


if __name__ == "__main__":
    returncode, stdout, stderr, node_bin = run_js_tests()
    if node_bin is None:
        print(
            "SKIP: no Node interpreter found (checked $NODE_BIN, "
            f"{_KNOWN_NODE!r}, and PATH). The shipped UI never needs Node; "
            "this only affects running the JS suites in this dev environment."
        )
        sys.exit(0)

    print(f"# running {len(_TEST_FILES)} JS suite(s) under {node_bin}\n")
    print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)
    sys.exit(returncode)
