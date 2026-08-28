"""kit/world/anchor.py — the Anchor primitive.

Grammar (CONTRACTS.md section 1):

    ns:slug[/rev][/idx][#span]

This is THE parser for the citation grammar used across the whole world
index. Every other module that needs to read or build an anchor string
imports :class:`Anchor` (and :func:`path_id`) from here rather than
re-implementing the grammar.

Stdlib only. No network, no randomness, no wall-clock.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

__all__ = [
    "AnchorSyntaxError",
    "Anchor",
    "NAMESPACES",
    "path_id",
]


class AnchorSyntaxError(ValueError):
    """Raised by :meth:`Anchor.parse` (and by direct construction of a
    malformed :class:`Anchor`) with a message naming exactly what was wrong
    and, where useful, the original string."""


# The 13 legal namespaces (CONTRACTS.md section 1, the `ns` row).
NAMESPACES: frozenset[str] = frozenset(
    {
        "Concept",
        "Frame",
        "Deck",
        "Section",
        "Claim",
        "Talk",
        "Source",
        "KC",
        "Lab",
        "Code",
        "Note",
        "Learner",
        "Glossary",
    }
)

# slug: [a-z0-9][a-z0-9-]*
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# rev: 'w' (working) or 'c' (canonical)
_REV_VALUES: frozenset[str] = frozenset({"w", "c"})

# idx: zero-padded 3-digit ordinal within the file
_IDX_RE = re.compile(r"^\d{3}$")

# span: L<start>-<end> line range, or s<n> sentence ordinal
_SPAN_RE = re.compile(r"^(?:L\d+-\d+|s\d+)$")


@dataclass(frozen=True, slots=True)
class Anchor:
    """A parsed `ns:slug[/rev][/idx][#span]` citation.

    Every field, however the instance was constructed (via :meth:`parse` or
    directly), is validated in ``__post_init__`` — so an ``Anchor`` object
    is always grammar-valid, and ``str(Anchor.parse(s))`` always reparses to
    an equal object.
    """

    ns: str
    slug: str
    rev: str | None = None
    idx: str | None = None
    span: str | None = None

    def __post_init__(self) -> None:
        if self.ns not in NAMESPACES:
            raise AnchorSyntaxError(
                f"unknown namespace {self.ns!r}; must be one of {sorted(NAMESPACES)}"
            )
        if not isinstance(self.slug, str) or not _SLUG_RE.match(self.slug):
            raise AnchorSyntaxError(
                f"malformed slug {self.slug!r}; expected pattern [a-z0-9][a-z0-9-]*"
            )
        if self.rev is not None and self.rev not in _REV_VALUES:
            raise AnchorSyntaxError(f"malformed rev {self.rev!r}; expected 'w' or 'c'")
        if self.idx is not None and not _IDX_RE.match(self.idx):
            raise AnchorSyntaxError(
                f"malformed idx {self.idx!r}; expected a zero-padded 3-digit ordinal, e.g. '041'"
            )
        if self.span is not None and not _SPAN_RE.match(self.span):
            raise AnchorSyntaxError(
                f"malformed span {self.span!r}; expected 'L<start>-<end>' or 's<n>'"
            )

    @classmethod
    def parse(cls, s: str) -> "Anchor":
        """Strict parse of `ns:slug[/rev][/idx][#span]`.

        Raises :class:`AnchorSyntaxError` with a useful message on any
        malformed input. Never returns a partially-valid Anchor.
        """
        if not isinstance(s, str):
            raise AnchorSyntaxError(f"anchor must be a str, got {type(s).__name__}")
        raw = s
        if s == "":
            raise AnchorSyntaxError("empty anchor string")

        if "#" in s:
            head, span = s.split("#", 1)
            if span == "":
                raise AnchorSyntaxError(f"empty span after '#' in {raw!r}")
        else:
            head, span = s, None

        if ":" not in head:
            raise AnchorSyntaxError(f"missing ':' separator in {raw!r}")
        ns, rest = head.split(":", 1)
        if ns == "":
            raise AnchorSyntaxError(f"empty namespace in {raw!r}")
        if rest == "":
            raise AnchorSyntaxError(f"empty slug in {raw!r}")

        parts = rest.split("/")
        slug = parts[0]
        extra = parts[1:]
        if len(extra) > 2:
            raise AnchorSyntaxError(
                f"too many '/' segments in {raw!r} (at most 2: rev then idx)"
            )

        rev: str | None = None
        idx: str | None = None

        if len(extra) == 1:
            seg = extra[0]
            if seg == "":
                raise AnchorSyntaxError(f"empty segment after '/' in {raw!r}")
            if seg in _REV_VALUES:
                rev = seg
            elif _IDX_RE.match(seg):
                idx = seg
            else:
                raise AnchorSyntaxError(
                    f"segment {seg!r} in {raw!r} is neither a valid rev ('w'/'c') "
                    "nor a 3-digit idx"
                )
        elif len(extra) == 2:
            rev_seg, idx_seg = extra
            if rev_seg == "" or idx_seg == "":
                raise AnchorSyntaxError(f"empty '/' segment in {raw!r}")
            if rev_seg not in _REV_VALUES:
                raise AnchorSyntaxError(
                    f"expected rev ('w'/'c') before idx, got {rev_seg!r} in {raw!r}"
                )
            if not _IDX_RE.match(idx_seg):
                raise AnchorSyntaxError(
                    f"expected 3-digit idx after rev, got {idx_seg!r} in {raw!r}"
                )
            rev, idx = rev_seg, idx_seg

        try:
            return cls(ns=ns, slug=slug, rev=rev, idx=idx, span=span)
        except AnchorSyntaxError as exc:
            raise AnchorSyntaxError(f"{exc} (parsing {raw!r})") from exc

    def __str__(self) -> str:
        out = f"{self.ns}:{self.slug}"
        if self.rev is not None:
            out += f"/{self.rev}"
        if self.idx is not None:
            out += f"/{self.idx}"
        if self.span is not None:
            out += f"#{self.span}"
        return out

    def key(self) -> tuple[str, str, str | None, str | None]:
        """`(ns, slug, rev, idx)` — the span-insensitive dedup key."""
        return (self.ns, self.slug, self.rev, self.idx)


def path_id(repo_relative_path: str) -> str:
    """The stable 8-lowercase-hex-char `path_id` for a file identity.

    THE slug for `Frame`/`Deck`/`Section` anchors. Deliberately never a day
    number: CORPUS-FACTS.md section 3 shows day11 alone resolves to three
    distinct canonical `.tex` files, and day10's real content lives at a
    different path than its `.tex` name suggests (`_flat-day10.tex` behind
    a 4,625 B `\\input` decoy) — a day-number key collides or silently picks
    the wrong file. Hashing the path instead makes every file identity
    disjoint by construction.

    Pure function of ``repo_relative_path`` (sha256, first 8 hex digits).
    Path convention: relative to the ai20k workspace root, e.g.
    ``"day26/day26-mcp-a2a-infrastructure-agentic-routing.tex"`` or
    ``"CourseMaterial/GD1/Latex/Latex Files/01_phase1_nen-tang/day11-guardrails-ai-safety.tex"``
    (CONTRACTS.md section 2's ``meta.source_path`` example uses the same
    convention). Backslashes are normalised to forward slashes and a
    leading ``./`` is stripped so the same file hashes identically
    regardless of how its path was assembled — beyond that this function
    does no filesystem access and does not care whether the path exists.
    """
    normalized = repo_relative_path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return digest[:8]


if __name__ == "__main__":
    from pathlib import Path

    # kit/world/anchor.py -> kit/world -> kit -> Day26-Colosseum-Agent-Arena-Kit -> lab -> day26 -> ai20k
    WORKSPACE_ROOT = Path(__file__).resolve().parents[5]

    print("=== Anchor round-trip demo ===")
    examples = [
        "Concept:streamable-http",
        "Frame:3f2a9c11/w/041",
        "Frame:3f2a9c11/c/041",
        "Claim:breach-cost/c/001",
        "Source:arxiv-2410-01234",
        "KC:context-window-budgeting",
        "Note:learner-sv-0417/w/003",
        "Learner:sv-0417",
        "Glossary:mcp-registry",
        "Frame:3f2a9c11/w/041#L812-848",
        "Section:9a1b2c3d/w/002#s4",
    ]
    for s in examples:
        a = Anchor.parse(s)
        rebuilt = str(a)
        ok = rebuilt == s and Anchor.parse(rebuilt) == a
        print(f"  {s!r:45} -> {a!r}  round-trip={'OK' if ok else 'FAIL'}")
        assert ok, f"round-trip failed for {s!r}"

    print("\n=== Rejection demo (each must raise AnchorSyntaxError) ===")
    bad_examples = [
        ("", "empty string"),
        (":slug", "empty ns"),
        ("Bogus:slug", "unknown ns"),
        ("Frame:UPPER", "uppercase slug"),
        ("Frame:3f2a9c11/x", "bad rev"),
        ("Frame:3f2a9c11/w/abc", "non-numeric idx"),
        ("Frame:3f2a9c11/w/41", "idx wrong width"),
        ("Frame:3f2a9c11#", "empty span"),
        ("Frame:3f2a9c11/w/041/extra", "too many segments"),
    ]
    for s, label in bad_examples:
        try:
            Anchor.parse(s)
        except AnchorSyntaxError as exc:
            print(f"  [{label:20}] {s!r:35} -> AnchorSyntaxError: {exc}")
        else:
            raise AssertionError(f"expected AnchorSyntaxError for {s!r} ({label})")

    print("\n=== path_id() on the real corpus ===")
    # The day11 trio: three genuinely different canonical files that a
    # day-number-keyed scheme would collide. All must hash to distinct ids.
    real_paths = [
        "day26/day26-mcp-a2a-infrastructure-agentic-routing.tex",
        "day11/day11-guardrails-ai-safety.tex",
        "CourseMaterial/GD1/Latex/Latex Files/01_phase1_nen-tang/day11-guardrails-ai-safety.tex",
        "CourseMaterial/GD1/Latex/Latex Files/01_phase1_nen-tang/"
        "day11-guardrails-ai-safety_E403_v2_linh.tex",
        "CourseMaterial/GĐ2/Base Slides/Track 3/"
        "day11-model-context-protocol-mcp-chuan-hoa-tool-integration.tex",
        "day10/day10-data-pipeline-observability.tex",  # 4,625 B \input decoy
        "day10/_flat-day10.tex",  # the real 202,840 B content
    ]
    ids = {}
    for rel in real_paths:
        full = WORKSPACE_ROOT / rel
        exists = full.is_file()
        pid = path_id(rel)
        ids.setdefault(pid, []).append(rel)
        print(f"  {pid}  exists={exists!s:5}  {rel}")
        assert exists, f"expected real corpus file at {full}"

    collisions = {pid: paths for pid, paths in ids.items() if len(paths) > 1}
    assert not collisions, f"path_id collided across distinct files: {collisions}"
    print(f"\n  {len(ids)} distinct path_ids for {len(real_paths)} distinct real files — no collisions.")

    # Stability: same path, called twice, same id.
    stable = path_id(real_paths[0]) == path_id(real_paths[0])
    assert stable
    print(f"  stability check: path_id() called twice on the same path -> equal: {stable}")

    # Every id must look like an 8-char lowercase hex string.
    hex_re = re.compile(r"^[0-9a-f]{8}$")
    assert all(hex_re.match(pid) for pid in ids), "a path_id was not 8 lowercase hex chars"
    print("  every path_id matches ^[0-9a-f]{8}$")

    print("\n=== Anchor.key() dedup demo ===")
    a1 = Anchor.parse("Frame:3f2a9c11/w/041#L812-848")
    a2 = Anchor.parse("Frame:3f2a9c11/w/041#L1-1")
    print(f"  {a1} .key() == {a2} .key() -> {a1.key() == a2.key()}  (span-insensitive)")
    assert a1.key() == a2.key()
    assert a1 != a2  # full equality still distinguishes span

    print(f"\n{len(NAMESPACES)} legal namespaces: {sorted(NAMESPACES)}")
    print("\nAll anchor.py demos passed.")
