"""kit/arena_ui/serve.py — serve the pixel arena over the byte-offset cursor.

    python -m kit.arena_ui.serve                      # latest run in runs/
    python -m kit.arena_ui.serve --run spar-operator-1
    python -m kit.arena_ui.serve --port 8899 --no-open

Same transport contract as the arena's (CONTRACTS.md 10.1), so the view you watch here
is byte-identical to the one on the projector:

    GET /                     the built spar.html
    GET /events?...&after=N   COMPLETE LINES ONLY, plus next_offset and eof
    GET /state                the L3 snapshot

`after` is a BYTE OFFSET, never a line number — no reader ever rescans from the start,
and a partial trailing write is simply withheld until its newline lands. That is also
what makes replay free: point it at a finished run and the same reducer produces the
same frames, with a scrubber.
"""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
KIT_ROOT = HERE.parent.parent
RUNS = KIT_ROOT / "runs"
MAX_CHUNK = 256 * 1024


def _latest_run() -> str | None:
    if not RUNS.is_dir():
        return None
    runs = sorted((p for p in RUNS.iterdir() if p.is_dir()),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return runs[0].name if runs else None


def _read_from(path: Path, after: int, limit: int) -> tuple[list[dict], int, bool]:
    """Complete lines only. Returns (events, next_offset, eof)."""
    if not path.is_file():
        return [], after, False
    size = path.stat().st_size
    if after >= size:
        return [], after, True
    with path.open("rb") as fh:
        fh.seek(after)
        chunk = fh.read(min(limit, MAX_CHUNK))
    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:
        return [], after, False          # nothing committed yet; withhold
    complete = chunk[: last_nl + 1]
    events: list[dict] = []
    for raw in complete.split(b"\n"):
        if not raw.strip():
            continue
        try:
            events.append(json.loads(raw.decode("utf8")))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue                     # a corrupt line is skipped, never fatal
    nxt = after + len(complete)
    return events, nxt, nxt >= size


class Handler(SimpleHTTPRequestHandler):
    run_name: str | None = None

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload).encode("utf8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        if parsed.path in ("/", "/index.html", "/spar.html"):
            html = HERE / "spar.html"
            if not html.is_file():
                self._json({"error": "spar.html not built — run kit/arena_ui/build_ui.py"}, 404)
                return
            body = html.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/events":
            run = (q.get("run") or [self.run_name or ""])[0]
            exchange = (q.get("exchange") or ["events"])[0]
            after = int((q.get("after") or ["0"])[0])
            limit = int((q.get("limit") or [str(MAX_CHUNK)])[0])
            path = RUNS / run / f"{exchange}.jsonl"
            events, nxt, eof = _read_from(path, after, limit)
            self._json({"run_id": run, "exchange_id": exchange,
                        "events": events, "next_offset": nxt, "eof": eof})
            return
        if parsed.path == "/state":
            run = (q.get("run") or [self.run_name or ""])[0]
            summary = RUNS / run / "summary.json"
            self._json({"run_id": run,
                        "summary": json.loads(summary.read_text(encoding="utf8"))
                        if summary.is_file() else []})
            return
        self._json({"error": "not found"}, 404)

    def log_message(self, *_args) -> None:  # keep the terminal readable
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default=None, help="a directory under runs/ (default: newest)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args(argv)

    run = a.run or _latest_run()
    if run is None:
        print("no runs yet — try:  python spar.py --bot operator --ui", file=sys.stderr)
        return 2
    Handler.run_name = run
    url = f"http://localhost:{a.port}/?run={run}"
    print(f"  arena: {url}\n  serving runs/{run}   (ctrl-c to stop)")
    if not a.no_open:
        webbrowser.open(url)
    try:
        HTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
