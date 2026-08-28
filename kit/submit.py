"""kit/submit.py — package your three artifacts into a sealed bundle.

    python -m kit.submit --team team-03

Produces `submissions/<team>.bundle` (a zip) containing `agent/`, `deck/`, `eval/`, a
manifest, and the sha256 of every `kit/` file exactly as you received it.

IT REFUSES RATHER THAN PENALISES. A rejection the day before is fixable; a penalty on
the day is not. Every failure below prints the file, the rule, and what to change — a
rejection you cannot act on is a support ticket, not a gate.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
import zipfile
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parent.parent
OWNED = ("agent", "deck", "eval")

# RULES.md section 2. Denied because they can reach the network, the filesystem outside
# your scratch, or another process. Most are also kernel-denied by the sandbox at run
# time; this gate exists so you find out now instead of mid-duel.
DENIED_IMPORTS = {
    "socket", "ssl", "http.client", "urllib.request", "urllib3", "requests", "httpx",
    "subprocess", "multiprocessing", "ctypes", "cffi", "importlib.util",
}
DENIED_CALLS = {"os.system", "os.execv", "os.spawnv", "eval", "exec", "compile"}


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _scan_imports(path: Path) -> list[str]:
    """AST, not regex: `import socket` in a docstring is prose, not an import."""
    try:
        tree = ast.parse(path.read_text(encoding="utf8"))
    except SyntaxError as exc:
        return [f"{path.name}:{exc.lineno} SYNTAX ERROR: {exc.msg}"]
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if alias.name in DENIED_IMPORTS or root in DENIED_IMPORTS:
                    bad.append(f"{path.name}:{node.lineno} denied import: {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if node.module in DENIED_IMPORTS or root in DENIED_IMPORTS:
                bad.append(f"{path.name}:{node.lineno} denied import: from {node.module}")
        elif isinstance(node, ast.Call):
            fn = node.func
            name = (f"{getattr(fn.value, 'id', '')}.{fn.attr}" if isinstance(fn, ast.Attribute)
                    else getattr(fn, "id", ""))
            if name in DENIED_CALLS:
                bad.append(f"{path.name}:{node.lineno} denied call: {name}(...)")
    return bad


def _kit_hashes() -> dict[str, str]:
    out = {}
    for p in sorted((KIT_ROOT / "kit").rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        out[p.relative_to(KIT_ROOT).as_posix()] = _sha(p)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--team", required=True)
    ap.add_argument("--force", action="store_true", help="bundle despite warnings (not errors)")
    a = ap.parse_args(argv)

    problems: list[str] = []

    for d in OWNED:
        if not (KIT_ROOT / d).is_dir():
            problems.append(f"missing directory: {d}/")
    for required in ("agent/gateway.py", "eval/prosecute.py", "deck/deck.json",
                     "deck/lineup.json"):
        if not (KIT_ROOT / required).is_file():
            problems.append(f"missing required file: {required}")

    for d in OWNED:
        for p in sorted((KIT_ROOT / d).rglob("*.py")):
            if "__pycache__" not in p.parts:
                problems.extend(_scan_imports(p))

    # Deck legality is the validator's job — do not duplicate its rules here, run it.
    sys.path.insert(0, str(KIT_ROOT))
    try:
        import validate_deck  # noqa: F401
        worlds = sorted((KIT_ROOT / "kit" / "world").glob("*/manifest.json"))
        if not worlds:
            problems.append("no world in kit/world/ — cannot validate the deck")
        else:
            import contextlib
            import io as _io

            # Run the real validator, silently. Do NOT reimplement its rules here — a
            # second copy of the deck rules is a second copy that can disagree, which is
            # exactly how the nine detectors ended up implemented twice in this project.
            buf = _io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = validate_deck.main([  # type: ignore[attr-defined]
                    str(KIT_ROOT / "deck" / "deck.json"),
                    str(KIT_ROOT / "deck" / "lineup.json"),
                    "--world", str(worlds[-1].parent)])
            if rc not in (0, None):
                fails = [ln for ln in buf.getvalue().splitlines() if ln.startswith("FAIL")]
                problems.append("deck failed validate_deck.py:")
                problems.extend(f"    {ln}" for ln in fails[:6])
    except SystemExit as exc:
        if exc.code not in (0, None):
            problems.append("deck failed validate_deck.py — run `make validate` to see why")
    except Exception as exc:
        problems.append(f"could not run validate_deck.py: {type(exc).__name__}: {exc}")

    if problems:
        print("SUBMISSION REJECTED — fix these and run again:\n")
        for p in problems:
            print(f"  · {p}")
        print(f"\n{len(problems)} problem(s). Nothing was written.")
        return 1

    out_dir = KIT_ROOT / "submissions"
    out_dir.mkdir(exist_ok=True)
    bundle = out_dir / f"{a.team}.bundle"
    manifest = {
        "team": a.team,
        "kit_hashes": _kit_hashes(),
        "files": [],
    }
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
        for d in OWNED:
            for p in sorted((KIT_ROOT / d).rglob("*")):
                if p.is_file() and "__pycache__" not in p.parts:
                    rel = p.relative_to(KIT_ROOT).as_posix()
                    z.write(p, rel)
                    manifest["files"].append({"path": rel, "sha256": _sha(p)})
        z.writestr("MANIFEST.json", json.dumps(manifest, indent=1, sort_keys=True))

    print(f"  wrote {bundle}  ({bundle.stat().st_size:,} bytes, "
          f"{len(manifest['files'])} files)")
    print(f"  kit/ hashes recorded: {len(manifest['kit_hashes'])} files")
    print("\n  agent/ and deck/ lock at session start. eval/ you keep working on.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
