"""kit.world — the frozen VLearnPedia index artifact (CONTRACTS.md section 2).

Public surface, re-exported for convenience:

    from kit.world import Anchor, AnchorSyntaxError, NAMESPACES, path_id
    from kit.world import Page, compute_etag

`Anchor` (kit/world/anchor.py) is THE parser for the `ns:slug[/rev][/idx][#span]`
citation grammar — every other module that needs one imports it from here
rather than re-implementing the grammar. `Page` (kit/world/page.py) is one
line of `world/pages.jsonl`. Both are stdlib-only, frozen dataclasses with
no network, randomness, or wall-clock dependence.
"""

from __future__ import annotations

from kit.world.anchor import Anchor, AnchorSyntaxError, NAMESPACES, path_id
from kit.world.page import Page, compute_etag

__all__ = [
    "Anchor",
    "AnchorSyntaxError",
    "NAMESPACES",
    "path_id",
    "Page",
    "compute_etag",
]


if __name__ == "__main__":
    from pathlib import Path

    # kit/world/__init__.py -> kit/world -> kit -> Day26-Colosseum-Agent-Arena-Kit -> lab -> day26 -> ai20k
    WORKSPACE_ROOT = Path(__file__).resolve().parents[5]

    print(f"kit.world: {len(NAMESPACES)} namespaces = {sorted(NAMESPACES)}\n")

    # A small real-corpus smoke test: build one Frame Page per working deck
    # found under day*/day*.tex (skipping the day10 \input decoy, which
    # CORPUS-FACTS.md section 6 flags explicitly), anchored on path_id, and
    # confirm every anchor round-trips and every etag is reproducible.
    deck_paths = sorted(
        p for p in WORKSPACE_ROOT.glob("day*/day*.tex") if p.name != "day10-data-pipeline-observability.tex"
    )
    print(f"found {len(deck_paths)} working decks under day*/day*.tex (day10 decoy excluded)")

    pages: list[Page] = []
    seen_path_ids: dict[str, Path] = {}
    for deck_path in deck_paths:
        rel = str(deck_path.relative_to(WORKSPACE_ROOT))
        pid = path_id(rel)
        if pid in seen_path_ids and seen_path_ids[pid] != deck_path:
            raise AssertionError(f"path_id collision: {pid} for both {seen_path_ids[pid]} and {deck_path}")
        seen_path_ids[pid] = deck_path

        text = deck_path.read_text(encoding="utf-8", errors="replace")
        # First 1,500 raw chars: every deck opens with the same 3-line
        # \documentclass/\usetheme/\usepackage(vinuni-macros) boilerplate
        # (CORPUS-FACTS.md section 4), so a too-short prefix would collide
        # across files by construction. 1,500 chars reaches past it into
        # deck-specific content (title, packages) for all but one real pair
        # — see the printed collision report below.
        anchor_str = f"Frame:{pid}/w/001"
        body = text[:1500].strip()
        page = Page(
            anchor=anchor_str,
            ns="Frame",
            path_id=pid,
            rev="w",
            idx="001",
            title=deck_path.name,
            body=body,
            lang="mixed",
            etag=compute_etag(body),
            status="ok",
            meta={"source_path": rel},
            links=(),
        )
        assert str(Anchor.parse(page.anchor)) == page.anchor
        assert Page.from_json(page.to_json()) == page
        pages.append(page)

    anchor_keys = {p.anchor for p in pages}
    assert len(anchor_keys) == len(pages), "anchor collision across the built pages"

    print(f"built and round-tripped {len(pages)} Frame Pages, one per deck, zero anchor collisions.")
    print("sample:")
    for page in pages[:3]:
        print(f"  {page.anchor}  {page.title!r:55} etag={page.etag}")

    by_etag: dict[str, list[str]] = {}
    for page in pages:
        by_etag.setdefault(page.etag, []).append(page.title)
    body_collisions = {etag: titles for etag, titles in by_etag.items() if len(titles) > 1}
    print(
        f"\n{len(by_etag)} distinct etags across {len(pages)} pages "
        f"(distinct etag == distinct 1,500-char body prefix)."
    )
    if body_collisions:
        print("  real body-prefix collision(s) — same etag, different files:")
        for etag, titles in sorted(body_collisions.items()):
            print(f"    {etag}: {titles}")

    print("\nkit.world __init__ smoke test passed.")
