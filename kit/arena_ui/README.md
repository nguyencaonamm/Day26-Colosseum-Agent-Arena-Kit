# `kit/arena_ui/` — the pixel arena UI

COLOSSEUM's battle HUD: a shared `core/`, and two views built on top of it.
Byte-identical between this repo and `Day26-Colosseum-Agent-Arena` (CONTRACTS.md
§10). This file documents `spar.html` (SparView) and `build_ui.py` (the
inliner) — the two pieces built here. `projector.html` (bracket + table
standings + featured duel) is a collaborator's file; everything below that
concerns *building* applies to it too, for free.

```
arena_ui/
├── core/                  shared, byte-identical between repos (not this task)
│   ├── decode.js          envelope + version handling
│   ├── reduce.js          events -> MatchState, pure
│   ├── sprites.js         string-encoded sprite sheets -> canvas
│   ├── font.js             5x7 bitmap font
│   ├── widgets.js         hp bar · credit bar · combat log · claim cut-in · scrubber
│   └── theme.js           colours, sizes, timings
├── spar.src.html          SparView, DEV SOURCE — real ES module imports
├── spar.html              SparView, SHIPPED — built by build_ui.py, self-contained
├── projector.src.html     ProjectorView, DEV SOURCE (a collaborator's file)
├── projector.html         ProjectorView, SHIPPED — also built by build_ui.py
├── build_ui.py            the inliner — turns *.src.html into self-contained *.html
└── tests/
    ├── sample_exchange.jsonl   a full fixture exchange — see below
    └── reduce.test.js          core/'s own vanilla-JS test suite (not this task)
```

## Building

```bash
python3 -m kit.arena_ui.build_ui        # from the repo root
# or: python3 kit/arena_ui/build_ui.py
```

Reads `spar.src.html` (required) and `projector.src.html` (optional —
skipped with a log line, never a failed build, if it isn't there yet).
Every `core/*.js` module either file imports — including a module's own
*transitive* imports, e.g. `widgets.js`'s static import of `theme.js` and its
dynamic `import('./font.js')` — is read, has its own local imports resolved
first (depth-first, memoized so a module shared by both files is only
encoded once), and is embedded as a `data:text/javascript;base64,...` URI.
The page's own `import ... from './core/x.js'` statements are rewritten to
point at those URIs in place; nothing else about the file changes. Real ES
module semantics the whole way (live bindings, one module instance per
page) — not a bundler, not a build step beyond this one script.

The build then asserts the output has **no `src=`/`href=` pointing off-host,
no un-inlined relative import left over, and no `fetch()` call with a literal
`http(s)://` or `//` prefix** — `data:` URIs and the page's own *relative*
runtime `fetch('/events?...')` (same-origin, required for live polling) are
both fine and don't trip it. It fails loudly (non-zero exit, every offending
reference listed) rather than ship a half-built page.

`spar.html` works from a `file://` URL with no network at all — open it
directly, or run it under any static file server for the query-param
transport (below) to actually reach anything.

**Gotcha discovered building this**: `core/theme.js`'s own header comment
contains a literal, quoted example import (`// import {...} from
"./core/theme.js"`) to show a *consumer* how to import it. An import-scanning
regex that doesn't know about comments matches that line as if it were real
code and resolves it relative to `theme.js`'s own directory — doubling the
path into a nonexistent `core/core/theme.js`. Fixed by anchoring the import
regexes to `^[ \t]*import` (a real import in this codebase is always the
first non-whitespace token on its own line; the fake one is preceded by
`// `) rather than trying to strip comments generically.

## `spar.html` — SparView

One `<canvas>` at a fixed 960×540 (16:9) logical resolution, scaled to fit
the viewport and DPR-aware, so pixel art stays crisp at any zoom. Layout
mirrors FINAL-PLAN.md §8.2's mock top to bottom: an integrity strip (reserved
space always, so it never has to shove the HUD down the instant it first
fires) · both HP bars + credit bars + latent-flag counters · the round
banner between two agent sprites · the combat log · the prosecution cut-in ·
(replay mode only) a scrubber.

### Query params

| Param | Meaning |
|---|---|
| `run`, `exchange` | poll the live event server: `GET /events?run=..&exchange=..&after=..&limit=..` (CONTRACTS §10.1), 125 ms while events arrive, exponential backoff to 1 s when idle, immediate re-poll on a non-empty response |
| `replay=<path>` | fetch a **finished** JSONL once (same-origin relative path) and serve it back through the exact same `{events, next_offset, eof}` shape as the live endpoint, sliced by byte offset — the reducer never knows the difference; only the *source* differs |
| `you=A\|B` | which trace-side letter is "you" (default `A`). The **left** screen slot is always drawn "A-style" (teal, `agentA` sprite, left-filling HP bar) and the right slot always "B-style", regardless of which literal side you are — `core/theme.js`'s own header calls this out: side identity is fixed, the *composing view* decides which half of the screen it lands on. `you` only picks which `MatchState` slice feeds which slot. |
| `debug=1` | exposes `window.__NH_DEBUG__` — read-only getters into this module's own closed-over state (`matchState`, `fx`, `cursor`, `scrubberRects`, `layout`, the playback-queue internals) plus a `forceRender(t)` escape hatch. Canvas has no DOM to inspect from outside, so this is how an external driver (a browser-automation check, a future e2e test) can assert on state without screen-scraping pixels. Inert — no code path above reads it — unless the param is present. |

No params at all: renders a full idle HUD (100/100 both sides) and,
best-effort, tries `tests/sample_exchange.jsonl` as a relative replay so
opening the page with nothing set still shows something real. Silent,
non-fatal if that fixture isn't sitting next to it in a real deployment.

### Live vs. replay, one reducer

Both `LiveSource` and `ReplaySource` expose the same
`poll(afterOffset) -> Promise<{events, next_offset, eof}>`, and the poll
loop, the pacing, and the fold through `core/decode.js` + `core/reduce.js`
are identical either way — CONTRACTS' own words: "there is no replay flag in
the reducer, only a different source." The two implementations differ only
in *how they get bytes*: one fetches `/events?...` and decodes an
already-parsed JSON array (`normalizeEvent`); the other slices a locally
loaded byte buffer and parses raw JSONL lines (`parseLine`) — exactly the
two entry points `decode.js`'s own header documents for exactly this reason.

### Full temporal choreography, even from one big batch

A poll response — especially a `ReplaySource`'s very first one — can hand
back an entire finished exchange in one array. Folding all of it into
`MatchState` synchronously would satisfy the reducer's purity contract but
would throw away the choreography FINAL-PLAN.md §8.2 asks for: a claim
cut-in that should hold 3 s would be replaced by the next one before a
single frame ever painted it. So arrival and application are separate:
polled/fetched events are queued, then released one at a time in the render
loop, paced by their own `t` field against a wall-clock anchor — the same
clock every `fx` timestamp already uses. A scrubber **seek** is the one
exception: it re-folds everything up to the target byte instantly (the
viewer asked to jump there, not to fast-forward through it), then playback
resumes paced from that point.

One consequence worth knowing if you extend this file: a seek's instant
catch-up deliberately does **not** re-arm the full-screen 5 s reveal
overlay, even though a stale `exchange_start` is normally what triggers it —
otherwise scrubbing to any point in a finished match would blank the screen
with a card-flip for the next 5 real seconds. Every other transient beat
(screen shake, the mutation particle burst, a sprite's attack/hurt flash,
the cut-in) is left triggerable on a seek too, since none of them cover the
whole canvas — briefly flashing the cut-in for a claim you just scrubbed
next to reads more like a caption than a bug.

### View-local choreography vs. `MatchState`

`core/reduce.js`'s own header is explicit that it does *not* carry
wall-clock animation bookkeeping (a tween's `from`/`to`/`changedAt`) — that
adapter is out of its scope, for "whoever writes it next." This file is that
adapter: after each fold, it diffs the previous `MatchState` against the new
one (exactly the diff-two-snapshots approach `reduce.js` itself suggests)
and derives a small `fx` object — HP/credit tween state, the latest
prosecution cut-in, screen shake, a sprite's momentary attack/hurt frame,
the sticky integrity banner, the KO overlay — which the render loop reads
alongside `MatchState` every frame. `MatchState` itself is never mutated,
and `hp`/`claim_outcome` values are always rendered exactly as the referee
published them, never recomputed here.

## `tests/sample_exchange.jsonl`

~54 CONTRACTS §5 events covering one full exchange end to end: the 4-call
agent loop (a clean `slides.query`, a `Gateway.decide` that raises —
`integrity`/`penalty`/denied `enforced` — a retried `slides.get_frame` whose
result is `partial` and never followed up, an `a2a
curriculum-analyst.which_days_cover` call landing under an `identity`
mutation that the gateway fails to catch, and a disciplined
`research.cite_source`), an answer, **one verified claim**
(`authority_exceeded`, weight 10 × round-scale 1.25 = 12 damage) and **one
false claim** (`wasteful`, −3 recoil to the prosecutor), a `latent_violation`
neither claim touched, and a **KO** (`duel_end`, `reason: "ko"`). Generated,
not hand-typed — every `evt:NNNN` evidence reference is a real index into
the file, and it round-trips losslessly through `core/decode.js` +
`core/reduce.js` (`node` check, zero rejected lines).

Exercise it with:

```bash
python3 -m http.server 8080          # from kit/arena_ui/, after building
# then open: http://127.0.0.1:8080/spar.html?replay=tests/sample_exchange.jsonl
```

Other agents' fixtures/tests are welcome to read this file directly too — it
is schema-valid JSONL, not a UI-specific format.
