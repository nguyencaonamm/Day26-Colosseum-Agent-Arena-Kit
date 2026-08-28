"""kit/world/loader.py — the read-only World interface (CONTRACTS.md section 2).

`World` is how every other kit module (mcp servers, the gateway, the
referee, bots, `spar.py`) reads the frozen VLearnPedia index. It never
writes. Build one with `World.load(path)` against a `world/` directory
produced by `worldbuild/index.py` (arena side, real corpus) or by this
package's sibling `kit.world.fixture.build_fixture_world` (a small
synthetic world, for building and testing every other module before the
real indexer lands).

Reading discipline — why every method below looks the way it does
--------------------------------------------------------------------
`pages.jsonl` is opened ONCE at `load()` time to build an `anchor -> byte
offset` index (a single linear pass, `f.tell()` before each `readline()`).
`.page()` then does a single `seek()` + `readline()` + parse for exactly the
anchor asked for — never the whole file — and caches the constructed `Page`.
That is the "lazy/indexed, not load 3000 pages into a dict every call"
requirement: the expensive part (constructing a validated `Page` dataclass,
which re-parses its `Anchor` and recomputes its `etag` in `__post_init__`)
happens at most once per anchor actually requested, not once per call and
not for pages nobody asked about. `.terms()`, `.links()`, `.drifts()` and
`.truth()` are the same shape: an eagerly-built small index (these files are
tiny relative to `pages.jsonl` — anchors and short answers, not page
bodies), then an O(1)/O(k) dict lookup per call.

`.search()` is the one deliberate exception: free-text search over
`title`/`body` has no inverted index in the CONTRACTS.md section 2 artifact
(`terms.json` is a *named*-term index, not a free-text one), so matching
genuinely requires looking at content. It streams the indexed anchors once
per call — bounded by page count, and touching only the raw JSON row, not a
full validated `Page` — and only builds `Page` objects (via the same cached
`.page()` path) for the pages that actually match, after sorting matches by
anchor so the result never depends on file iteration order.

Stdlib only. No network, no randomness, no wall-clock.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

# ---------------------------------------------------------------------------
# kit/world/anchor.py and kit/world/page.py are owned by a collaborator.
# Import them; if they are not there yet, fall back to a minimal local
# definition so this module still runs standalone. As of this file being
# written, both already exist in the tree and are used directly — the
# fallback below is dead code in that case, kept only for resilience.
# ---------------------------------------------------------------------------
try:
    from kit.world.anchor import Anchor, AnchorSyntaxError, NAMESPACES, path_id
    from kit.world.page import Page, compute_etag

    _USING_FALLBACK_MODELS = False
except ImportError:  # pragma: no cover - exercised only if anchor.py/page.py are missing
    _USING_FALLBACK_MODELS = True
    import hashlib
    import re
    from dataclasses import dataclass, field

    class AnchorSyntaxError(ValueError):
        """Fallback stand-in for kit/world/anchor.py's exception."""

    NAMESPACES = frozenset(
        {
            "Concept", "Frame", "Deck", "Section", "Claim", "Talk", "Source",
            "KC", "Lab", "Code", "Note", "Learner", "Glossary",
        }
    )
    _SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
    _REV_VALUES = frozenset({"w", "c"})
    _IDX_RE = re.compile(r"^\d{3}$")
    _SPAN_RE = re.compile(r"^(?:L\d+-\d+|s\d+)$")

    @dataclass(frozen=True, slots=True)
    class Anchor:
        """Minimal fallback matching CONTRACTS.md section 1's grammar."""

        ns: str
        slug: str
        rev: str | None = None
        idx: str | None = None
        span: str | None = None

        def __post_init__(self) -> None:
            if self.ns not in NAMESPACES:
                raise AnchorSyntaxError(f"unknown namespace {self.ns!r}")
            if not _SLUG_RE.match(self.slug):
                raise AnchorSyntaxError(f"malformed slug {self.slug!r}")
            if self.rev is not None and self.rev not in _REV_VALUES:
                raise AnchorSyntaxError(f"malformed rev {self.rev!r}")
            if self.idx is not None and not _IDX_RE.match(self.idx):
                raise AnchorSyntaxError(f"malformed idx {self.idx!r}")
            if self.span is not None and not _SPAN_RE.match(self.span):
                raise AnchorSyntaxError(f"malformed span {self.span!r}")

        @classmethod
        def parse(cls, s: str) -> "Anchor":
            if not isinstance(s, str) or s == "":
                raise AnchorSyntaxError(f"invalid anchor string {s!r}")
            head, sep, span = s.partition("#")
            span = span if sep else None
            if sep and span == "":
                raise AnchorSyntaxError(f"empty span in {s!r}")
            if ":" not in head:
                raise AnchorSyntaxError(f"missing ':' in {s!r}")
            ns, rest = head.split(":", 1)
            parts = rest.split("/")
            slug = parts[0]
            extra = parts[1:]
            rev = idx = None
            if len(extra) >= 1 and extra[0]:
                if extra[0] in _REV_VALUES:
                    rev = extra[0]
                elif _IDX_RE.match(extra[0]):
                    idx = extra[0]
                else:
                    raise AnchorSyntaxError(f"bad segment {extra[0]!r} in {s!r}")
            if len(extra) == 2 and extra[1]:
                idx = extra[1]
            return cls(ns=ns, slug=slug, rev=rev, idx=idx, span=span)

        def __str__(self) -> str:
            out = f"{self.ns}:{self.slug}"
            if self.rev is not None:
                out += f"/{self.rev}"
            if self.idx is not None:
                out += f"/{self.idx}"
            if self.span is not None:
                out += f"#{self.span}"
            return out

        def key(self) -> tuple:
            return (self.ns, self.slug, self.rev, self.idx)

    def path_id(repo_relative_path: str) -> str:
        normalized = repo_relative_path.strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]

    def compute_etag(body: str) -> str:
        return f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()[:16]}"

    @dataclass(frozen=True, slots=True)
    class Page:
        """Minimal fallback matching CONTRACTS.md section 2's Page shape."""

        anchor: str
        ns: str
        path_id: str
        rev: str | None
        idx: str | None
        title: str
        body: str
        lang: str
        etag: str
        status: str
        meta: Mapping[str, object]
        links: tuple = field(default_factory=tuple)
        extraction_tier: str = "A"
        confidence: float = 1.0

        def to_json(self) -> dict:
            return {
                "anchor": self.anchor, "ns": self.ns, "path_id": self.path_id,
                "rev": self.rev, "idx": self.idx, "title": self.title,
                "body": self.body, "lang": self.lang, "etag": self.etag,
                "status": self.status, "meta": dict(self.meta),
                "links": list(self.links), "extraction_tier": self.extraction_tier,
                "confidence": self.confidence,
            }

        @classmethod
        def from_json(cls, d: Mapping[str, object]) -> "Page":
            return cls(
                anchor=d["anchor"], ns=d["ns"], path_id=d["path_id"],
                rev=d.get("rev"), idx=d.get("idx"), title=d["title"],
                body=d["body"], lang=d["lang"], etag=d["etag"], status=d["status"],
                meta=dict(d.get("meta", {})), links=tuple(d.get("links", ())),
                extraction_tier=d.get("extraction_tier", "A"),
                confidence=d.get("confidence", 1.0),
            )

        def dumps(self) -> str:
            return json.dumps(self.to_json(), ensure_ascii=False, sort_keys=True)


__all__ = [
    "World",
    "ask_key",
    "ASK_IDENTITY_FIELDS",
    "AskKeyError",
]


class AskKeyError(ValueError):
    """Raised by :func:`ask_key` for an ask dict of unknown or malformed shape."""


# ---------------------------------------------------------------------------
# CONTRACTS.md section 7 defines the eight ask *answer* shapes but not an ask
# *identity* convention for truth.json's keys. That convention is a local
# decision, made here, and `kit.world.fixture` builds its fixture truth.json
# with this exact function so the two always agree. `worldbuild/index.py`
# (arena side, built concurrently by another team) must key its truth.json
# the same way for `.truth()` lookups to resolve against the real corpus —
# see this task's report for the explicit flag.
#
# Only the fields listed here participate in a truth.json key; `"require"`
# is always dropped (it says what the *answer* must contain, not which
# lookup this is), and any extra ask fields are ignored too.
# ---------------------------------------------------------------------------
ASK_IDENTITY_FIELDS: Mapping[str, tuple] = {
    "which_day_covers": ("concept",),
    "source_of": ("anchor",),
    "citation_for": ("concept",),
    "current_version_of": ("path_id",),
    "contradiction_between": ("talk",),
    "define_term": ("term", "anchor"),
    "whatlinkshere": ("anchor",),
    "record_mastery": ("learner", "concept"),
}


def ask_key(ask: Mapping[str, object]) -> str:
    """Canonicalise a CONTRACTS.md section 7 `ask` dict into the string key
    `truth.json` is keyed by.

    Pure function of `ask`'s identity fields (`ASK_IDENTITY_FIELDS[type]`)
    plus `"type"` itself — nothing else participates, so two asks that
    differ only in `require` (or in any field this ask type does not use
    for identity) collapse onto the same truth.json entry. Any `Anchor`
    value is stringified before serialising so callers may pass either
    `Anchor` instances or plain anchor strings interchangeably.
    """
    if not isinstance(ask, Mapping):
        raise AskKeyError(f"ask must be a mapping, got {type(ask).__name__}")
    ask_type = ask.get("type")
    if ask_type not in ASK_IDENTITY_FIELDS:
        raise AskKeyError(
            f"unknown ask type {ask_type!r}; expected one of {sorted(ASK_IDENTITY_FIELDS)}"
        )
    identity: dict[str, object] = {"type": ask_type}
    for field_name in ASK_IDENTITY_FIELDS[ask_type]:
        if field_name in ask and ask[field_name] is not None:
            value = ask[field_name]
            identity[field_name] = str(value) if isinstance(value, Anchor) else value
    return json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _normalize_anchor(anchor: object) -> str:
    return str(anchor)


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


class World:
    """Read-only handle onto one `world/` artifact directory.

    Construct with :meth:`load` — never directly.
    """

    __slots__ = (
        "_root", "_manifest", "_pages_path", "_offsets", "_page_cache",
        "_terms", "_links", "_drift", "_has_truth", "_truth",
    )

    def __init__(
        self,
        *,
        root: Path,
        manifest: dict,
        pages_path: Path,
        offsets: Mapping[str, int],
        terms: Mapping[str, tuple],
        links: Mapping[str, tuple],
        drift: Mapping[str, dict],
        has_truth: bool,
        truth: Mapping[str, dict],
    ) -> None:
        self._root = root
        self._manifest = manifest
        self._pages_path = pages_path
        self._offsets = dict(offsets)
        self._page_cache: dict[str, Page] = {}
        self._terms = dict(terms)
        self._links = dict(links)
        self._drift = dict(drift)
        self._has_truth = has_truth
        self._truth = dict(truth)

    # -- construction --------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "World":
        """Build a `World` over the `world/` directory at `path`.

        Only `manifest.json` and `pages.jsonl` are required; `terms.json`,
        `links.json`, `drift.json` and `truth.json` are each optional
        (missing -> empty index for that facet). `truth.json` in particular
        is *expected* to be absent in the student kit (CONTRACTS.md section
        2 invariant 4) — its absence sets `.has_truth` to `False` rather
        than raising.
        """
        root = Path(path)
        manifest_path = root / "manifest.json"
        pages_path = root / "pages.jsonl"
        terms_path = root / "terms.json"
        links_path = root / "links.json"
        drift_path = root / "drift.json"
        truth_path = root / "truth.json"

        if not manifest_path.is_file():
            raise FileNotFoundError(f"world manifest missing: {manifest_path}")
        if not pages_path.is_file():
            raise FileNotFoundError(f"world pages.jsonl missing: {pages_path}")

        manifest = _read_json(manifest_path)
        offsets = cls._index_pages(pages_path)

        terms_raw = _read_json(terms_path) if terms_path.is_file() else {}
        terms = {
            term: tuple(Anchor.parse(a) for a in anchors)
            for term, anchors in terms_raw.items()
        }

        links_raw = _read_json(links_path) if links_path.is_file() else {}
        links = {
            anchor: tuple(Anchor.parse(a) for a in targets)
            for anchor, targets in links_raw.items()
        }

        drift = _read_json(drift_path) if drift_path.is_file() else {}

        has_truth = truth_path.is_file()
        truth = _read_json(truth_path) if has_truth else {}

        return cls(
            root=root, manifest=manifest, pages_path=pages_path, offsets=offsets,
            terms=terms, links=links, drift=drift, has_truth=has_truth, truth=truth,
        )

    @staticmethod
    def _index_pages(pages_path: Path) -> dict[str, int]:
        """One linear pass over `pages.jsonl`, building `anchor -> byte
        offset`. This is the only full read of the file that ever happens
        at load time (`.search()` is the documented, deliberate exception
        that reads it again per call)."""
        offsets: dict[str, int] = {}
        with pages_path.open("rb") as f:
            while True:
                offset = f.tell()
                raw = f.readline()
                if not raw:
                    break
                stripped = raw.strip()
                if not stripped:
                    continue
                row = json.loads(stripped)
                anchor = row["anchor"]
                if anchor in offsets:
                    raise ValueError(
                        "duplicate anchor in pages.jsonl (CONTRACTS.md section 2 "
                        f"invariant 1 violated): {anchor!r}"
                    )
                offsets[anchor] = offset
        return offsets

    # -- read surface ----------------------------------------------------

    @property
    def manifest(self) -> dict:
        return dict(self._manifest)

    @property
    def has_truth(self) -> bool:
        """`False` in the student kit — `truth.json` is never shipped there
        (CONTRACTS.md section 2, invariant 4)."""
        return self._has_truth

    def page(self, anchor: "Anchor | str") -> "Page | None":
        """O(1) indexed lookup + lazy parse, cached. Returns `None` for an
        anchor not present in this world — never raises for that case."""
        anchor_str = _normalize_anchor(anchor)
        cached = self._page_cache.get(anchor_str)
        if cached is not None:
            return cached
        offset = self._offsets.get(anchor_str)
        if offset is None:
            return None
        with self._pages_path.open("rb") as f:
            f.seek(offset)
            raw = f.readline()
        page = Page.from_json(json.loads(raw))
        self._page_cache[anchor_str] = page
        return page

    def search(self, q: str, ns: str | None = None, limit: int = 20) -> list:
        """Case-insensitive substring match over `title` + `body`, restarted
        for every call (see the module docstring for why this — and only
        this — method is a scan rather than an indexed lookup). `ns`
        restricts to one namespace when given. Results are collected in
        full, then sorted by anchor and sliced to `limit`, so the answer
        never depends on file iteration order (CONTRACTS.md section 11)."""
        if not isinstance(q, str) or not q.strip():
            return []
        needle = q.strip().lower()
        matched: list[str] = []
        with self._pages_path.open("rb") as f:
            for anchor_str, offset in self._offsets.items():
                f.seek(offset)
                raw = f.readline()
                if not raw:
                    continue
                row = json.loads(raw)
                if ns is not None and row.get("ns") != ns:
                    continue
                haystack = f"{row.get('title', '')}\n{row.get('body', '')}".lower()
                if needle in haystack:
                    matched.append(anchor_str)
        matched.sort()
        return [self.page(a) for a in matched[: max(0, limit)]]

    def terms(self, term: str, lang: str | None = None) -> list:
        """Glossary/alias/redirect lookup (`terms.json`).

        `lang=None` returns every anchor registered under `term` — every
        sense, every language — in the fixed order `terms.json` stores them
        in. This is the *raw* view: FINAL-PLAN.md section 4.2 hard-mode
        mechanic #7 says a wrong or missing `lang` should silently return
        the other language's entry. The loader makes that possible by
        keeping every sense/language for an ambiguous term reachable (never
        collapsing them at load time) — but it never performs the silent
        substitution itself; a tool server decides whether and when to call
        `.terms(term)` unfiltered versus `.terms(term, lang=...)` honest.

        `lang="vi"`/`"en"` filters to anchors whose resolved `Page.lang`
        equals `lang` or is `"mixed"`. No match -> `[]`, never a guess.
        """
        key = term.strip().lower()
        anchors = self._terms.get(key, ())
        if lang is None:
            return list(anchors)
        matched = []
        for anchor in anchors:
            page = self.page(anchor)
            if page is not None and page.lang in (lang, "mixed"):
                matched.append(anchor)
        return matched

    def links(self, anchor: "Anchor | str") -> list:
        """`whatlinkshere` — the precomputed reverse-link lookup
        (`links.json`). Unlinked anchor -> `[]`."""
        anchor_str = _normalize_anchor(anchor)
        return list(self._links.get(anchor_str, ()))

    def drifts(self, path_id: str) -> bool:
        """Whether the working/canonical replicas sharing this `path_id`
        disagree (`drift.json`). An unmeasured `path_id` -> `False`
        (nothing measured, so nothing to report as drifting)."""
        record = self._drift.get(path_id)
        if record is None:
            return False
        return bool(record.get("drifts", False))

    def drift_info(self, path_id: str) -> dict | None:
        """The full `drift.json` record for `path_id` (`w_frames`,
        `c_frames`, `drifts`, `delta`), or `None` if unmeasured. Every
        field here is already public in the artifact; `.drifts()` is a
        one-field convenience over this same record."""
        record = self._drift.get(path_id)
        return dict(record) if record is not None else None

    def truth(self, ask: Mapping[str, object]) -> dict | None:
        """Pure lookup against `truth.json` (CONTRACTS.md section 7).

        Always `None` when `.has_truth` is `False` — the student kit never
        ships `truth.json`, so this degrades to "nothing to look up" rather
        than raising."""
        if not self._has_truth:
            return None
        key = ask_key(ask)
        answer = self._truth.get(key)
        if answer is None:
            return None
        # A deep copy, not `dict(answer)`: some answers (e.g. `whatlinkshere`'s
        # `anchors` list) nest a mutable value one level down, and `dict()`
        # only copies the top level — a caller mutating that nested list
        # would otherwise corrupt `self._truth` for every later call. Every
        # answer is JSON by construction, so a json round-trip is a cheap,
        # correct deep copy.
        return json.loads(json.dumps(answer))


if __name__ == "__main__":
    import tempfile

    from kit.world.fixture import build_fixture_world

    print(f"kit.world.loader — using fallback Anchor/Page models: {_USING_FALLBACK_MODELS}\n")

    with tempfile.TemporaryDirectory(prefix="colosseum-world-") as tmp:
        world_dir = build_fixture_world(tmp)
        print(f"=== built fixture world at {world_dir} ===\n")

        print("=== World.load() ===")
        world = World.load(world_dir)
        print(f"  manifest = {world.manifest}")
        print(f"  has_truth = {world.has_truth}  (fixture ships truth.json by default)")
        assert world.has_truth is True

        print("\n=== .page() — indexed lookup + cache ===")
        pages = list(world._offsets)  # noqa: SLF001 - test-only introspection of the index size
        print(f"  {len(pages)} anchors indexed")
        sample_anchor = sorted(a for a in pages if a.startswith("Frame:"))[0]
        p1 = world.page(sample_anchor)
        p2 = world.page(sample_anchor)
        print(f"  page({sample_anchor!r}) -> {p1.title!r} (lang={p1.lang})")
        print(f"  same object returned on 2nd call (cache hit): {p1 is p2}")
        assert p1 is not None and p1 is p2
        assert world.page("Frame:00000000/w/999") is None
        print("  unknown anchor -> None: OK")

        print("\n=== .search() ===")
        hits = world.search("streamable http", ns="Frame", limit=5)
        print(f"  search('streamable http', ns='Frame') -> {len(hits)} hits")
        for h in hits:
            print(f"    {h.anchor}  {h.title!r}")
        assert hits, "expected at least one Frame hit for 'streamable http'"
        assert all(h.ns == "Frame" for h in hits)
        no_hits = world.search("khong-the-nao-tim-thay-chuoi-nay")
        assert no_hits == []
        print("  ns filter respected; empty query correctly returns []")

        print("\n=== .terms() — ambiguous term + lang filtering ===")
        all_senses = world.terms("endpoint")
        vi_sense = world.terms("endpoint", lang="vi")
        en_sense = world.terms("endpoint", lang="en")
        print(f"  terms('endpoint')            -> {[str(a) for a in all_senses]}")
        print(f"  terms('endpoint', lang='vi') -> {[str(a) for a in vi_sense]}")
        print(f"  terms('endpoint', lang='en') -> {[str(a) for a in en_sense]}")
        assert len(all_senses) == 2
        assert len(vi_sense) == 1 and len(en_sense) == 1
        assert set(vi_sense) | set(en_sense) == set(all_senses)
        unambiguous = world.terms("field mask")
        print(f"  terms('field mask')          -> {[str(a) for a in unambiguous]}")
        assert len(unambiguous) == 1

        print("\n=== .links() — whatlinkshere ===")
        backlinks = world.links("Concept:streamable-http")
        print(f"  links('Concept:streamable-http') -> {len(backlinks)} anchors")
        assert backlinks, "expected at least one Frame to cite Concept:streamable-http"

        print("\n=== .drifts() / .drift_info() ===")
        drift_path_ids = sorted(world._drift)  # noqa: SLF001 - test-only introspection
        for pid in drift_path_ids:
            print(f"  drift_info({pid!r}) = {world.drift_info(pid)}  drifts()={world.drifts(pid)}")
        assert any(world.drifts(pid) for pid in drift_path_ids)
        assert any(not world.drifts(pid) for pid in drift_path_ids)
        assert world.drifts("not-a-real-path-id") is False
        print("  at least one drifting deck, at least one byte-identical deck, unknown -> False")

        print("\n=== .truth() — all 8 CONTRACTS.md section 7 ask types ===")
        for ask_type in sorted(ASK_IDENTITY_FIELDS):
            matches = [
                json.loads(k) for k in world._truth if json.loads(k)["type"] == ask_type  # noqa: SLF001
            ]
            assert matches, f"fixture truth.json has no entry for ask type {ask_type!r}"
            ask = matches[0]
            answer = world.truth(ask)
            print(f"  {ask_type:22} ask={ask} -> {answer}")
            assert answer is not None

        print("\n=== has_truth=False world (the student-kit shape) ===")
        with tempfile.TemporaryDirectory(prefix="colosseum-world-notruth-") as tmp2:
            student_dir = build_fixture_world(tmp2, include_truth=False)
            student_world = World.load(student_dir)
            print(f"  has_truth = {student_world.has_truth}")
            assert student_world.has_truth is False
            assert student_world.truth({"type": "source_of", "anchor": sample_anchor}) is None
            assert not (Path(student_dir) / "truth.json").exists()
            print("  truth.json absent on disk, .truth() returns None without raising: OK")

        print("\nAll kit/world/loader.py demos passed.")
