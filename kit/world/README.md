# `kit/world/` — the VLearnPedia index you play against

This directory holds two different things and it matters which one you're looking at:

```
kit/world/
├── anchor.py, page.py, loader.py, fixture.py, __init__.py    <- CODE. Yours to read, not to write.
└── <world_id>/                                                <- DATA. Read-only. Not in git.
    ├── manifest.json
    ├── pages.jsonl
    ├── terms.json
    ├── links.json
    ├── drift.json
    ├── denylist_report.json
    └── kit_export_manifest.json
    (truth.json is deliberately not here — see "Where's truth.json?" below)
```

The `.py` files are the **reader**: `Anchor` (the citation grammar), `Page` (one indexed
document), `World` (the read-only interface everything else in this kit — MCP servers, the
gateway, bots, the referee-side fixtures — loads and calls `.page()`/`.search()`/`.terms()`/
`.links()`/`.drifts()` against). You never write to any of this from `agent/gateway.py` or
anywhere else scored — `World` has no write methods, on purpose (CONTRACTS.md §2: "produced by
`worldbuild/index.py` [arena side], consumed read-only by everything").

The `<world_id>/` directory is the **data**: one built snapshot of the AI20K corpus — 24 working
decks, 66 canonical decks, 35 RESEARCH companions, 16 GLOSSARY companions, reduced to ~12,000
citable pages. It is not checked into this repo (`.gitignore` excludes `kit/world/*/`) because it's
an 11+ MB generated artifact, not source — you build or fetch it locally, the same way you'd build
any other compiled output.

## Getting a world

You have two, and you will use both:

**1. The fixture, for every fast local test.** `kit.world.fixture.build_fixture_world(dest)`
synthesises a small (~40-page), fully self-consistent world in milliseconds, no corpus required.
Every test under `tests/` that doesn't explicitly ask for the real thing runs against this. It's
what makes `make test` fast and key-free.

**2. The real, ~12,000-page world**, exported from the instructor's build. From a checkout that
also has `Day26-Colosseum-Agent-Arena` as a sibling directory (the instructor's environment; not
something a student repo ships), run, from the *Arena* side:

```bash
cd Day26-Colosseum-Agent-Arena
python3.12 -m worldbuild.index --slice main          # builds corpus_snapshot/<world_id>/
python3.12 -m tools.export_kit_world                 # copies it here, into kit/world/<world_id>/
```

`tools.export_kit_world` is the only thing that should ever populate this directory. It:

- copies `manifest.json`, `pages.jsonl`, `terms.json`, `links.json`, `drift.json` byte-for-byte,
- **never copies `truth.json`** — checked twice: once by skipping the filename, once again by
  recursively re-scanning the destination directory for it afterward, and it hard-fails
  (`KitExportSafetyError`) rather than warn if that second check ever finds one,
- rewrites `denylist_report.json` down to pattern counts (see below),
- and writes `kit_export_manifest.json` — a small provenance record: which `world_id`, which
  source build (`corpus_sha`, `built_at`), when this export ran, and a `sha256` per exported file
  so tampering or corruption after the fact is detectable.

To re-check an export without touching anything (e.g. after pulling a fresh copy, or if you're
not sure a previous export finished cleanly):

```bash
python3.12 -m tools.export_kit_world --verify
```

This re-derives every file's hash and compares it against `kit_export_manifest.json`, re-confirms
`truth.json` is genuinely absent, and re-confirms `denylist_report.json` is still redacted. It
prints `"ok": false` and a `problems` list (not a stack trace) on anything it finds — including
if you deliberately go edit a file in here, which you shouldn't, since this directory is meant to
be read-only from every other module's point of view.

If you don't have an Arena checkout: you don't need one for anything in `tests/`. Only a handful
of things — actually running a full 10-round duel end to end against the real corpus, or checking
your gateway against real citation density rather than the fixture's ~40 pages — need the real
world at all.

## What each file holds

| File | What's in it |
|---|---|
| `manifest.json` | `world_id`, `built_at`, `corpus_sha`, per-namespace page counts, which build stages ran. Read via `World.manifest`. |
| `pages.jsonl` | One JSON object per line, one per indexed page — `Frame`, `Section`, `Deck`, `Concept`, `Claim`, `Source`, `Talk`, `Note`, `Learner`, `Glossary`, keyed by `Anchor` (CONTRACTS.md §1). This is the thing your tools actually read content from. |
| `terms.json` | `{term_lower: [anchor, ...]}` — the glossary/alias map `define_term` resolves against. |
| `links.json` | `{anchor: [anchor, ...]}` — precomputed "what links here," backing the `whatlinkshere` ask type. |
| `drift.json` | Per-deck working-vs-canonical page-count deltas — which decks have a working copy that has moved ahead of (or fallen behind) the canonical one. Backs `current_version_of` and the `stale_read` detector. |
| `denylist_report.json` | **Redacted.** Pattern-match counts only (`by_pattern`, `files_scanned`, `files_denied`, `files_indexable`) — proof the safety scan ran and roughly how much it caught, with the per-file evidence (which would include real student submission filenames) stripped out before this ever left the instructor's machine. |
| `kit_export_manifest.json` | Not part of CONTRACTS.md §2's own artifact shape — added by `tools.export_kit_world` as this export's own receipt: source `world_id`/`corpus_sha`/`built_at`, when the export ran, and a sha256 per file for `--verify` to check against later. |

`World.load()` only *requires* `manifest.json` and `pages.jsonl` to exist; everything else is
optional and degrades to an empty index for that facet if missing (`kit/world/loader.py`'s own
docstring). `kit_export_manifest.json` is never read by `World` — it exists for the export
tool-chain, not the game.

## Where's `truth.json`?

Not here. Deliberately, permanently, and by construction — not just "usually."

`truth.json` is `{ask_key: resolved_answer}` for every question the arena's ask types can pose
(CONTRACTS.md §7). It is how a `record_mastery` write, or a `which_day_covers` answer, gets graded.
Shipping it to the kit would hand every student the answer key to the game they're playing — so it
is excluded on the instructor's side, before anything crosses into a student-visible repo, and
re-checked independently after the copy. `World.has_truth` is `False` against a directory built
this way, and `World.truth()` returns `None` rather than raising — code that calls it (there is
some, in developer-side fixtures that *do* ship `truth.json` for grading tests) degrades cleanly
instead of crashing when it's asked to run against the real, student-shipped world.

If you ever find a `truth.json` sitting in `kit/world/<world_id>/`, that is a bug — in the export
tool if it came from `tools.export_kit_world`, or in whatever process put it there if it didn't.
Report it; don't build against it.

## Read-only, in practice

Nothing under `kit/world/<world_id>/` should ever be opened for writing by anything in `agent/`,
`kit/mcp/`, `kit/loop/`, or `bots/`. `World` has no write methods and no server here executes a
write against these files directly — a `record_mastery` write goes through the gateway and the
arena's own recording path, never a direct file write into this directory. If your code needs
`open(..., "w")` anywhere near `kit/world/`, that's a sign you've reached for the wrong tool.
