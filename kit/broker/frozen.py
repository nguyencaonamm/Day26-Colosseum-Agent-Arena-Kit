"""kit/broker/frozen.py — FrozenBroker: replay by canonical prompt hash.

CONTRACTS.md section 9: "``FrozenBroker`` (replay by prompt hash)" — listed
alongside ``LiveBroker``/``MockBroker`` but, unlike either, not marked
"arena only" or "kit only": it ships in the kit (FINAL-PLAN.md section 2.2's
tree: "``kit/broker/`` — MockBroker + FrozenBroker ONLY. No live client
here") because it is what makes CONTRACTS.md section 11's reproducibility
gate possible without spending a live model call on every check:

    G-REPRO: replay one exchange 10x through FrozenBroker; mean |Δdamage|
    < 2 HP.

and it is FINAL-PLAN.md section 10's named fallback when the live endpoint
degrades mid-bracket ("the frozen-bracket fallback stays built"). Either
way, ``FrozenBroker`` never calls a model — it looks a recorded response up
by the **canonical hash of the prompt that would have produced it**
(:func:`kit.broker.base.canonical_prompt_hash`) and returns exactly that
recording, or refuses.

THE DESIGN DECISION THIS FILE COMMITS TO — resolved, not left ambiguous:
**an unknown prompt raises :class:`FrozenMissError`; it never returns any
message, empty-content or otherwise.** The task brief says "a clearly-
labelled miss, never a silent empty string" — a labelled *return value*
would still be a value a careless caller could half-parse as a real answer
(``msg["content"]`` on a miss-flagged dict is still a string). Raising is
the only shape that cannot be silently treated as content: a caller must
either catch :class:`FrozenMissError` and handle "this replay diverged from
what was recorded" explicitly, or let it propagate — and *that* is what
G-REPRO is actually checking for, since a divergent replay must be visible
as a divergence, not scored as if it were a slightly different answer.

Bundle format: this module supports three ways to build the replay table,
all converging on the same internal ``{canonical_hash: response}`` map —
none of them assumes a specific recorder's on-disk shape exists yet (no
recorder is part of this task; the arena's own recording harness is a
sibling module not written here):

* :meth:`FrozenBroker.from_pairs` — the ergonomic path for tests, fixtures,
  and any recorder: an iterable of ``(messages, response)`` pairs. This
  module hashes each ``messages`` itself, so a caller never needs to touch
  :func:`~kit.broker.base.canonical_prompt_hash` directly.
* :meth:`FrozenBroker.from_mapping` — an already-hash-keyed dict, for a
  caller that has (or wants to precompute) the hashes itself.
* :meth:`FrozenBroker.load` — a JSON file on disk, one bundle: a JSON list
  of ``{"prompt_hash": "sha256:...", "response": {...}}`` records (the
  natural "append one record per recorded live call" shape) OR a flat
  ``{hash: response}`` object — both accepted, detected by top-level JSON
  type. This is a local decision (no on-disk bundle format is specified
  anywhere in CONTRACTS.md/FINAL-PLAN.md); documented here so a future
  recorder can target it directly.

Stdlib only. No network, no randomness, no wall-clock.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from kit.broker.base import Broker, canonical_prompt_hash, validate_broker_message

__all__ = ["FrozenMissError", "BundleFormatError", "FrozenBroker"]


class FrozenMissError(LookupError):
    """Raised by :meth:`FrozenBroker.query` when the canonical hash of
    ``messages`` is not in the loaded bundle. Never swallowed into a return
    value — see the module docstring. Carries enough to debug a divergent
    replay without re-deriving it: the hash that missed, how many records
    the bundle holds, and (best-effort, truncated) a preview of the prompt
    that produced it."""

    def __init__(self, prompt_hash: str, *, bundle_size: int, messages: Sequence[Mapping[str, object]]) -> None:
        self.prompt_hash = prompt_hash
        self.bundle_size = bundle_size
        preview = _preview_messages(messages)
        super().__init__(
            f"FrozenBroker: no recorded response for prompt_hash={prompt_hash!r} "
            f"(bundle holds {bundle_size} record{'s' if bundle_size != 1 else ''}). "
            f"Prompt preview: {preview}"
        )


class BundleFormatError(ValueError):
    """Raised when a bundle passed to :meth:`FrozenBroker.from_mapping` /
    :meth:`FrozenBroker.from_pairs` / :meth:`FrozenBroker.load` is malformed
    — a non-string hash, a response that fails :func:`validate_broker_message`,
    a JSON file that is neither a list-of-records nor a flat hash->response
    object, or (in :meth:`from_pairs` / :meth:`load`'s list form) two
    records disagreeing about the response recorded for the same hash,
    which would make replay depend on load order — exactly the kind of
    silent nondeterminism CONTRACTS.md section 11 rules out."""


def _preview_messages(messages: Sequence[Mapping[str, object]], *, limit: int = 200) -> str:
    try:
        blob = json.dumps(list(messages), ensure_ascii=True)
    except (TypeError, ValueError):
        return "<unserialisable messages>"
    return blob if len(blob) <= limit else blob[: limit - 3] + "..."


def _validate_bundle_entry(prompt_hash: object, response: object) -> None:
    if not isinstance(prompt_hash, str) or not prompt_hash.startswith("sha256:"):
        raise BundleFormatError(f"bundle key must be a 'sha256:...' str, got {prompt_hash!r}")
    if not isinstance(response, Mapping):
        raise BundleFormatError(f"bundle response for {prompt_hash!r} must be a dict, got {type(response).__name__}")
    validate_broker_message(response)


class FrozenBroker:
    """CONTRACTS.md section 9's ``Broker`` (see :class:`kit.broker.base.Broker`),
    implemented as a lookup against a fixed, pre-recorded bundle. Construct
    via :meth:`from_pairs`, :meth:`from_mapping`, or :meth:`load` — never
    directly (the constructor takes an already-validated internal map and
    exists mainly so the three classmethods share one code path)."""

    def __init__(self, records: Mapping[str, dict]) -> None:
        self._records: dict[str, dict] = dict(records)

    # -- construction ------------------------------------------------

    @classmethod
    def from_mapping(cls, records: Mapping[str, Mapping[str, object]]) -> "FrozenBroker":
        """Build directly from an already hash-keyed ``{prompt_hash:
        response}`` mapping. Every entry is validated (a legal ``sha256:``
        key, a response that passes :func:`validate_broker_message`) before
        acceptance, and every response is deep-copied so the bundle cannot
        be mutated out from under this broker by a caller still holding a
        reference to the input mapping."""
        built: dict[str, dict] = {}
        for prompt_hash, response in records.items():
            _validate_bundle_entry(prompt_hash, response)
            built[prompt_hash] = copy.deepcopy(dict(response))
        return cls(built)

    @classmethod
    def from_pairs(cls, pairs: Iterable[tuple[Sequence[Mapping[str, object]], Mapping[str, object]]]) -> "FrozenBroker":
        """Build from an iterable of ``(messages, response)`` pairs — the
        ergonomic path: hash each ``messages`` with
        :func:`~kit.broker.base.canonical_prompt_hash` internally, so a
        caller (a test, a fixture, a future recorder) never computes a hash
        by hand. Two pairs that hash to the same canonical prompt must
        record the *same* response, byte for byte after canonicalisation of
        the response's own JSON shape — a genuine disagreement means the
        recording run was itself nondeterministic, which
        :class:`BundleFormatError` surfaces immediately rather than letting
        replay silently pick whichever pair loaded last."""
        built: dict[str, dict] = {}
        for messages, response in pairs:
            validate_broker_message(response)
            prompt_hash = canonical_prompt_hash(list(messages))
            candidate = copy.deepcopy(dict(response))
            if prompt_hash in built and _stable_dump(built[prompt_hash]) != _stable_dump(candidate):
                raise BundleFormatError(
                    f"from_pairs: two different responses recorded for the same canonical prompt "
                    f"(prompt_hash={prompt_hash!r}) — the recording run was not deterministic"
                )
            built[prompt_hash] = candidate
        return cls(built)

    @classmethod
    def load(cls, path: str | Path) -> "FrozenBroker":
        """Load a bundle from a JSON file. Accepts either top-level shape:

        * a list of ``{"prompt_hash": "sha256:...", "response": {...}}``
          records (the natural "append one per recorded call" shape), or
        * a flat ``{"sha256:...": {...}, ...}`` object.

        Either way, every record is validated exactly as in
        :meth:`from_mapping`. Raises :class:`BundleFormatError` for
        anything else at the top level, or for a malformed record."""
        p = Path(path)
        with p.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        if isinstance(raw, list):
            mapping: dict[str, dict] = {}
            for i, record in enumerate(raw):
                if not isinstance(record, Mapping) or "prompt_hash" not in record or "response" not in record:
                    raise BundleFormatError(
                        f"{p}: record {i} must be a dict with 'prompt_hash' and 'response' keys, got {record!r}"
                    )
                prompt_hash = record["prompt_hash"]
                response = record["response"]
                _validate_bundle_entry(prompt_hash, response)
                if prompt_hash in mapping and _stable_dump(mapping[prompt_hash]) != _stable_dump(dict(response)):
                    raise BundleFormatError(f"{p}: duplicate, disagreeing records for prompt_hash={prompt_hash!r}")
                mapping[prompt_hash] = copy.deepcopy(dict(response))
            return cls(mapping)

        if isinstance(raw, dict):
            return cls.from_mapping(raw)

        raise BundleFormatError(f"{p}: top-level JSON must be a list of records or a {{hash: response}} object, got {type(raw).__name__}")

    # -- Broker protocol -----------------------------------------------

    def __len__(self) -> int:
        return len(self._records)

    def query(self, messages: list[dict], **kw: object) -> dict:
        """Look ``messages`` up by its canonical hash. Returns a deep copy
        of the recorded response (never the stored object itself, so a
        caller mutating its result cannot corrupt this broker for the next
        lookup) or raises :class:`FrozenMissError` — never a silent empty
        string (see the module docstring). ``**kw`` is accepted and ignored,
        matching :class:`~kit.broker.base.Broker`'s shared signature."""
        if not isinstance(messages, list):
            raise TypeError(f"FrozenBroker.query: messages must be a list, got {type(messages).__name__}")
        prompt_hash = canonical_prompt_hash(messages)
        try:
            recorded = self._records[prompt_hash]
        except KeyError:
            raise FrozenMissError(prompt_hash, bundle_size=len(self._records), messages=messages) from None
        response = copy.deepcopy(recorded)
        validate_broker_message(response)
        return response


def _stable_dump(d: Mapping[str, object]) -> str:
    return json.dumps(d, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


if __name__ == "__main__":
    import tempfile

    from kit.broker.base import final_message, make_tool_call, tool_call_message

    print("=== kit.broker.frozen: build a tiny bundle with from_pairs() ===\n")

    sys_msg = {"role": "system", "content": "You are the COLOSSEUM agent."}
    user_msg = {"role": "user", "content": "Which day covers streamable http?"}
    call = make_tool_call("slides.query", {"q": "streamable http", "fields": ["title"]}, call_id="call_0")
    turn0 = tool_call_message("Looking it up.", [call], reasoning_content="checking the deck first")
    tool_result_msg = {"role": "tool", "tool_call_id": "call_0", "content": json.dumps({"ok": True, "rows": []})}
    turn1 = final_message("Ngày 26 [Frame:8c5bf8f6/w/001].")

    pairs = [
        ([sys_msg, user_msg], turn0),
        ([sys_msg, user_msg, turn0, tool_result_msg], turn1),
    ]
    bundle = FrozenBroker.from_pairs(pairs)
    print(f"  bundle holds {len(bundle)} records")
    assert len(bundle) == 2

    print("\n=== exact replay: same messages -> byte-identical recorded response ===")
    got0 = bundle.query([sys_msg, user_msg])
    print(f"  query([sys, user]) -> {got0}")
    assert got0 == turn0

    got1 = bundle.query([sys_msg, user_msg, turn0, tool_result_msg])
    print(f"  query([sys, user, turn0, tool_result]) -> {got1}")
    assert got1 == turn1

    print("\n=== canonical-hash insensitivity: whitespace/reasoning_content-only variants still hit ===")
    whitespace_variant_user = {"role": "user", "content": "  Which  day   covers streamable http?  "}
    turn0_different_reasoning = {**turn0, "reasoning_content": "a totally different chain of thought"}
    got_variant = bundle.query([sys_msg, whitespace_variant_user, turn0_different_reasoning, tool_result_msg])
    print(f"  query() with whitespace/reasoning_content differences -> still returns turn1: {got_variant == turn1}")
    assert got_variant == turn1

    print("\n=== a genuinely different prompt is a MISS, and MISSES RAISE, never return silently ===")
    try:
        bundle.query([sys_msg, {"role": "user", "content": "What is field mask?"}])
    except FrozenMissError as exc:
        print(f"  FrozenMissError raised, as required: {exc}")
        assert exc.bundle_size == 2
    else:
        raise AssertionError("expected FrozenMissError for an unrecorded prompt")

    print("\n=== G-REPRO smoke test: replay the SAME exchange 10x, byte-identical every time ===")
    replays = [bundle.query([sys_msg, user_msg, turn0, tool_result_msg]) for _ in range(10)]
    all_identical = all(r == turn1 for r in replays)
    print(f"  10/10 replays identical: {all_identical}")
    assert all_identical

    print("\n=== load()/save round trip through an on-disk JSON bundle (list-of-records shape) ===")
    with tempfile.TemporaryDirectory(prefix="colosseum-frozen-") as tmp:
        bundle_path = Path(tmp) / "bundle.json"
        records = [
            {"prompt_hash": h, "response": r}
            for h, r in {
                **{k: v for k, v in zip([canonical_prompt_hash([sys_msg, user_msg])], [turn0])},
                canonical_prompt_hash([sys_msg, user_msg, turn0, tool_result_msg]): turn1,
            }.items()
        ]
        with bundle_path.open("w", encoding="utf-8") as f:
            json.dump(records, f, sort_keys=True, ensure_ascii=False, indent=2)
        loaded = FrozenBroker.load(bundle_path)
        print(f"  loaded {len(loaded)} records from {bundle_path.name}")
        assert len(loaded) == 2
        assert loaded.query([sys_msg, user_msg]) == turn0
        assert loaded.query([sys_msg, user_msg, turn0, tool_result_msg]) == turn1
        print("  both records replay correctly after a JSON round trip")

    print("\n=== load() also accepts the flat {hash: response} shape ===")
    with tempfile.TemporaryDirectory(prefix="colosseum-frozen-flat-") as tmp2:
        flat_path = Path(tmp2) / "bundle_flat.json"
        flat = {canonical_prompt_hash([sys_msg, user_msg]): turn0}
        with flat_path.open("w", encoding="utf-8") as f:
            json.dump(flat, f, sort_keys=True, ensure_ascii=False, indent=2)
        loaded_flat = FrozenBroker.load(flat_path)
        assert loaded_flat.query([sys_msg, user_msg]) == turn0
        print("  flat-shape bundle loads and replays correctly")

    print("\n=== from_pairs() rejects two disagreeing responses for the same canonical prompt ===")
    try:
        FrozenBroker.from_pairs(
            [
                ([sys_msg, user_msg], turn0),
                ([sys_msg, user_msg], final_message("a completely different answer")),
            ]
        )
    except BundleFormatError as exc:
        print(f"  BundleFormatError raised, as required: {exc}")
    else:
        raise AssertionError("expected BundleFormatError for two disagreeing recordings of the same prompt")

    print("\nAll kit/broker/frozen.py demos passed.")
