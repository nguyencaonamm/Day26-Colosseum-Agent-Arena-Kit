"""kit/world/page.py — the `Page` record (CONTRACTS.md section 2).

One `Page` is one line of `world/pages.jsonl`, the frozen index artifact.
Stdlib only. No network, no randomness, no wall-clock.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from kit.world.anchor import Anchor, AnchorSyntaxError

__all__ = ["Page", "compute_etag"]

# CONTRACTS.md section 2, Page: "lang" is one of these three.
_LANG_VALUES: frozenset[str] = frozenset({"vi", "en", "mixed"})

# CONTRACTS.md section 2, Page: "status" is one of these three.
_STATUS_VALUES: frozenset[str] = frozenset({"ok", "stub", "skeleton"})


def compute_etag(body: str) -> str:
    """`"sha256:<16 hex>"` — a PURE function of ``body``.

    Same body text always yields the same etag; nothing else (not a
    timestamp, not the anchor, not the rest of the Page) participates.
    This is what lets a caller recompute it from ``body`` alone and get
    back exactly what is stored (CONTRACTS.md section 2, invariant 3).
    """
    if not isinstance(body, str):
        raise TypeError(f"compute_etag body must be a str, got {type(body).__name__}")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


@dataclass(frozen=True, slots=True)
class Page:
    """One entry of `world/pages.jsonl` (CONTRACTS.md section 2).

    `anchor`, `ns`, `path_id`, `rev`, `idx` are kept as separate fields
    (rather than derived on the fly from a single Anchor field) because
    that is the JSON shape the contract specifies — callers filter/sort
    `pages.jsonl` on the flattened fields without re-parsing every anchor.
    `__post_init__` enforces that they are mutually consistent with the
    anchor string, and that `etag` really is `compute_etag(body)`, so a
    `Page` cannot exist in a self-contradictory state.
    """

    anchor: str  # str(Anchor), e.g. "Frame:3f2a9c11/w/041"
    ns: str
    path_id: str  # mirrors the anchor's slug component (a hash for
    #                Frame/Deck/Section; a human slug otherwise)
    rev: str | None
    idx: str | None
    title: str
    body: str  # plain text, LaTeX macros stripped
    lang: str  # "vi" | "en" | "mixed"
    etag: str  # "sha256:16hex" — must equal compute_etag(body)
    status: str  # "ok" | "stub" | "skeleton"
    meta: Mapping[str, object]
    links: tuple[str, ...] = field(default_factory=tuple)
    extraction_tier: str = "A"
    confidence: float = 1.0

    def __post_init__(self) -> None:
        try:
            parsed = Anchor.parse(self.anchor)
        except AnchorSyntaxError as exc:
            raise ValueError(f"Page.anchor {self.anchor!r} is not a valid Anchor: {exc}") from exc

        if parsed.ns != self.ns:
            raise ValueError(
                f"Page.ns {self.ns!r} does not match anchor {self.anchor!r} (ns={parsed.ns!r})"
            )
        if parsed.slug != self.path_id:
            raise ValueError(
                f"Page.path_id {self.path_id!r} does not match anchor {self.anchor!r} "
                f"(slug={parsed.slug!r})"
            )
        if parsed.rev != self.rev:
            raise ValueError(
                f"Page.rev {self.rev!r} does not match anchor {self.anchor!r} (rev={parsed.rev!r})"
            )
        if parsed.idx != self.idx:
            raise ValueError(
                f"Page.idx {self.idx!r} does not match anchor {self.anchor!r} (idx={parsed.idx!r})"
            )

        if self.lang not in _LANG_VALUES:
            raise ValueError(f"Page.lang {self.lang!r} must be one of {sorted(_LANG_VALUES)}")
        if self.status not in _STATUS_VALUES:
            raise ValueError(f"Page.status {self.status!r} must be one of {sorted(_STATUS_VALUES)}")

        expected_etag = compute_etag(self.body)
        if self.etag != expected_etag:
            raise ValueError(
                f"Page.etag {self.etag!r} is not compute_etag(body) ({expected_etag!r}) "
                f"for anchor {self.anchor!r}"
            )

        if not isinstance(self.confidence, (int, float)) or isinstance(self.confidence, bool):
            raise ValueError(f"Page.confidence must be a number, got {self.confidence!r}")
        if not (0.0 <= float(self.confidence) <= 1.0):
            raise ValueError(f"Page.confidence {self.confidence!r} must be within [0.0, 1.0]")

    def to_json(self) -> dict:
        """The exact JSON-serialisable dict for one `pages.jsonl` line."""
        return {
            "anchor": self.anchor,
            "ns": self.ns,
            "path_id": self.path_id,
            "rev": self.rev,
            "idx": self.idx,
            "title": self.title,
            "body": self.body,
            "lang": self.lang,
            "etag": self.etag,
            "status": self.status,
            "meta": dict(self.meta),
            "links": list(self.links),
            "extraction_tier": self.extraction_tier,
            "confidence": self.confidence,
        }

    @classmethod
    def from_json(cls, d: Mapping[str, object]) -> "Page":
        """Inverse of :meth:`to_json`. Missing optional keys fall back to
        the same defaults `Page`'s constructor would use."""
        links_raw = d.get("links", ())
        if not isinstance(links_raw, Sequence) or isinstance(links_raw, (str, bytes)):
            raise ValueError(f"Page.links must be a list of anchor strings, got {links_raw!r}")
        meta_raw = d.get("meta", {})
        if not isinstance(meta_raw, Mapping):
            raise ValueError(f"Page.meta must be an object, got {meta_raw!r}")
        return cls(
            anchor=d["anchor"],
            ns=d["ns"],
            path_id=d["path_id"],
            rev=d.get("rev"),
            idx=d.get("idx"),
            title=d["title"],
            body=d["body"],
            lang=d["lang"],
            etag=d["etag"],
            status=d["status"],
            meta=dict(meta_raw),
            links=tuple(links_raw),
            extraction_tier=d.get("extraction_tier", "A"),
            confidence=d.get("confidence", 1.0),
        )

    def dumps(self) -> str:
        """One deterministic JSON line (sorted keys) for `pages.jsonl`."""
        return json.dumps(self.to_json(), ensure_ascii=False, sort_keys=True)


if __name__ == "__main__":
    from pathlib import Path

    print("=== compute_etag() purity demo ===")
    body_a = "Streamable HTTP thay the HTTP+SSE lam giao van MCP mac dinh tu 2026-07-28."
    body_b = body_a + " "  # one trailing space -> must change the etag
    etag_a1 = compute_etag(body_a)
    etag_a2 = compute_etag(body_a)
    etag_b = compute_etag(body_b)
    print(f"  compute_etag(body_a) called twice: {etag_a1} == {etag_a2} -> {etag_a1 == etag_a2}")
    print(f"  compute_etag(body_a) != compute_etag(body_a + ' '): {etag_a1} != {etag_b} -> {etag_a1 != etag_b}")
    assert etag_a1 == etag_a2
    assert etag_a1 != etag_b
    assert etag_a1.startswith("sha256:") and len(etag_a1) == len("sha256:") + 16

    print("\n=== Page round-trip demo, built from the real day26 deck ===")
    workspace_root = Path(__file__).resolve().parents[5]
    source_path = "day26/day26-mcp-a2a-infrastructure-agentic-routing.tex"
    deck_path = workspace_root / source_path
    text = deck_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    # Grab a real, non-trivial line range straight out of the actual deck,
    # so the demo body/anchor/meta all describe the same real content.
    start = 900
    end = 903
    snippet = "\n".join(lines[start - 1 : end]).strip()
    print(f"  read {len(lines)} lines from {source_path}")
    print(f"  L{start}-{end} snippet: {snippet[:80]!r}...")

    from kit.world.anchor import path_id as pid_fn

    pid = pid_fn(source_path)
    anchor_str = f"Frame:{pid}/w/017"
    page = Page(
        anchor=anchor_str,
        ns="Frame",
        path_id=pid,
        rev="w",
        idx="017",
        title="day26 working deck, sample frame",
        body=snippet,
        lang="mixed",
        etag=compute_etag(snippet),
        status="ok",
        meta={
            "track": "P2T2",
            "file_day": 26,
            "course_day": 26,
            "source_path": source_path,
            "line_start": start,
            "line_end": end,
        },
        links=(),
        extraction_tier="A",
        confidence=1.0,
    )
    print(f"  built Page(anchor={page.anchor!r}, etag={page.etag!r})")

    dumped = page.dumps()
    restored = Page.from_json(json.loads(dumped))
    print(f"  Page.from_json(json.loads(page.dumps())) == page -> {restored == page}")
    assert restored == page

    print("\n=== Rejection demo (each must raise ValueError) ===")

    def _try(label: str, **overrides: object) -> None:
        kwargs = page.to_json()
        kwargs.pop("anchor")
        kwargs["anchor"] = overrides.pop("anchor", page.anchor)
        kwargs.update(overrides)
        kwargs["links"] = tuple(kwargs["links"])
        try:
            Page(**kwargs)
        except ValueError as exc:
            print(f"  [{label:22}] -> ValueError: {exc}")
        else:
            raise AssertionError(f"expected ValueError for case {label!r}")

    _try("stale etag", etag="sha256:0000000000000000")
    _try("bad lang", lang="fr")
    _try("bad status", status="done")
    _try("ns/anchor mismatch", ns="Deck")
    _try("path_id/anchor mismatch", path_id="deadbeef")
    _try("confidence out of range", confidence=1.7)

    print("\nAll page.py demos passed.")
