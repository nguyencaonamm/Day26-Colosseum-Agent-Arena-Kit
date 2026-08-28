"""kit/mcp/types.py — the shared MCP request/result types (CONTRACTS.md 3.1, 3.2).

:class:`ToolCall` is section 3.1's request shape, field for field.
:class:`ToolResult` is section 3.2's "one shape, always" success envelope —
CONTRACTS.md leaves the Python type unnamed there, only the JSON — extended
to also hold section 3.3's much smaller ``{"ok": false, "error": {...},
"cost": n}`` error envelope for the ``ok=False`` case. ``to_dict()`` never
lets the two shapes bleed into each other: a failed call's dict carries
only ``ok``/``error``/``cost``, never a success envelope padded with
``null`` provenance fields. Padding an ``unavailable`` failure with
``"anchors": null, "etag": null, ...`` would not violate the letter of
kit/mcp/errors.py's ``{"code"}``-only rule (that rule is about the
*``error``* sub-dict), but it would violate its spirit by handing a
curious student a wall of nulls to speculate from — so this module keeps
the two envelopes structurally disjoint.

:func:`canonicalise_fields` is CONTRACTS.md 4.1's "already canonicalised:
sorted, deduped, lowercased" field-mask transform, exposed here so every
caller — the arena building a ``Command``, a server validating a
``ToolCall`` — applies the identical stdlib-only rule instead of several
slightly different reimplementations. :class:`ToolCall` applies it to its
own ``fields`` on construction (see the module-ambiguity note below).

Stdlib only. No network, no randomness, no wall-clock.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from kit.mcp.errors import ErrorCode

__all__ = ["ToolCall", "ToolResult", "canonicalise_fields"]


def canonicalise_fields(fields: Iterable[str]) -> tuple[str, ...]:
    """Sort, dedupe and lowercase a field mask (CONTRACTS.md 4.1).

    ``()`` (the "default set" sentinel) round-trips to ``()``. ``("*",)``
    (the "all fields" sentinel) round-trips to ``("*",)``. Mixed-case,
    duplicate, or out-of-order input collapses to one canonical tuple —
    which is what keeps ``cost_of()``'s ``sum(spec.field_weight[f] for f
    in fields)`` (CONTRACTS.md 3.4) deterministic regardless of how a
    caller phrased the mask (a repeated field could otherwise double-charge
    its weight; two callers naming the same fields in different order or
    case could otherwise be charged differently for an identical request).
    """
    if isinstance(fields, (str, bytes)):
        raise TypeError(
            f"fields must be an iterable of field-name strings, not a bare "
            f"{type(fields).__name__} ({fields!r}) — did you mean ({fields!r},)?"
        )
    lowered: set[str] = set()
    for f in fields:
        if not isinstance(f, str):
            raise TypeError(f"field mask entries must be str, got {f!r}")
        lowered.add(f.lower())
    return tuple(sorted(lowered))


@dataclass(frozen=True, slots=True)
class ToolCall:
    """The request shape (CONTRACTS.md 3.1), field for field.

    AMBIGUITY RESOLVED: CONTRACTS.md 4.1 says a ``Command``'s ``fields``
    arrive at the student's gateway "already canonicalised: sorted,
    deduped, lowercased" — stated there as a property of the arena-built
    ``Command``, not explicitly of every ``ToolCall``. Rather than trust
    every future caller (server code, referee replay, a student's
    ``Decision.call``) to remember to call :func:`canonicalise_fields`
    themselves, ``__post_init__`` applies it unconditionally to
    ``self.fields``. This can only ever narrow a mask (dedupe) or reorder
    it — it never adds or removes a *distinct* field — so it cannot change
    what a call is asking for, only make ``cost_of()`` and any downstream
    dedup logic robust to how the caller phrased the mask.
    """

    server: str
    tool: str
    args: dict
    fields: tuple[str, ...] = ()
    headers: dict = field(default_factory=dict)
    lease_id: str | None = None
    call_index: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.server, str) or not self.server:
            raise ValueError(f"ToolCall.server must be a non-empty str, got {self.server!r}")
        if not isinstance(self.tool, str) or not self.tool:
            raise ValueError(f"ToolCall.tool must be a non-empty str, got {self.tool!r}")
        if not isinstance(self.args, dict):
            raise ValueError(f"ToolCall.args must be a dict, got {type(self.args).__name__}")
        if not isinstance(self.headers, dict):
            raise ValueError(f"ToolCall.headers must be a dict, got {type(self.headers).__name__}")
        if self.lease_id is not None and not isinstance(self.lease_id, str):
            raise ValueError(f"ToolCall.lease_id must be a str or None, got {self.lease_id!r}")
        if (
            not isinstance(self.call_index, int)
            or isinstance(self.call_index, bool)
            or self.call_index < 0
        ):
            raise ValueError(
                f"ToolCall.call_index must be a non-negative int, got {self.call_index!r}"
            )

        # Always reassign (not just when the value changes): a caller may
        # have passed a list rather than a tuple, and this also normalises
        # the *type*, not just the sort/dedupe/case of an already-tuple value.
        object.__setattr__(self, "fields", canonicalise_fields(self.fields))

    def to_dict(self) -> dict:
        """The exact JSON-serialisable dict for this request."""
        return {
            "server": self.server,
            "tool": self.tool,
            "args": dict(self.args),
            "fields": list(self.fields),
            "headers": dict(self.headers),
            "lease_id": self.lease_id,
            "call_index": self.call_index,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> "ToolCall":
        """Inverse of :meth:`to_dict`. Missing optional keys fall back to
        the same defaults the constructor would use."""
        return cls(
            server=d["server"],
            tool=d["tool"],
            args=dict(d.get("args", {})),
            fields=tuple(d.get("fields", ())),
            headers=dict(d.get("headers", {})),
            lease_id=d.get("lease_id"),
            call_index=d.get("call_index", 0),
        )


_VALID_REPLICAS: frozenset[str] = frozenset({"w", "c"})


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The result shape: CONTRACTS.md 3.2's success envelope (``ok=True``)
    or 3.3's error envelope (``ok=False``), depending on ``ok``.

    When ``ok`` is ``False``: ``error`` must be a
    :func:`kit.mcp.errors.make_error` dict (a ``code`` from the closed
    nine-member taxonomy, plus whatever extra keys that code allows — none,
    for ``unavailable``), and every success-only field (``rows``,
    ``anchors``, ``partial``, ``continuation``, ``lease_id``, ``etag``,
    ``replica``, ``ttl``, ``deprecated``, ``successor``) must be left at
    its default. ``cost`` is meaningful in both cases (CONTRACTS.md 3.3:
    "Cost is still charged except where noted").

    When ``ok`` is ``True``: ``error`` must be ``None``.
    """

    ok: bool
    rows: tuple[Mapping[str, object], ...] = ()
    anchors: tuple[str, ...] = ()
    cost: int = 0
    partial: bool = False
    continuation: str | None = None
    lease_id: str | None = None
    etag: str | None = None
    replica: str | None = None
    ttl: int | None = None
    deprecated: bool = False
    successor: str | None = None
    error: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.ok, bool):
            raise ValueError(f"ToolResult.ok must be a bool, got {self.ok!r}")
        if not isinstance(self.cost, int) or isinstance(self.cost, bool) or self.cost < 0:
            raise ValueError(f"ToolResult.cost must be a non-negative int, got {self.cost!r}")

        if isinstance(self.rows, (str, bytes)):
            raise ValueError("ToolResult.rows must be a sequence of row mappings, not a bare str")
        rows_t = tuple(self.rows)
        if not all(isinstance(r, Mapping) for r in rows_t):
            raise ValueError("ToolResult.rows must contain only mappings")
        object.__setattr__(self, "rows", rows_t)

        if isinstance(self.anchors, (str, bytes)):
            raise ValueError("ToolResult.anchors must be a sequence of anchor strings, not a bare str")
        anchors_t = tuple(self.anchors)
        if not all(isinstance(a, str) for a in anchors_t):
            raise ValueError("ToolResult.anchors must contain only str")
        object.__setattr__(self, "anchors", anchors_t)

        if self.replica is not None and self.replica not in _VALID_REPLICAS:
            raise ValueError(f"ToolResult.replica must be 'w', 'c', or None, got {self.replica!r}")
        if self.ttl is not None and (
            not isinstance(self.ttl, int) or isinstance(self.ttl, bool) or self.ttl < 0
        ):
            raise ValueError(f"ToolResult.ttl must be a non-negative int or None, got {self.ttl!r}")
        if not isinstance(self.partial, bool):
            raise ValueError(f"ToolResult.partial must be a bool, got {self.partial!r}")
        if not isinstance(self.deprecated, bool):
            raise ValueError(f"ToolResult.deprecated must be a bool, got {self.deprecated!r}")

        if self.ok:
            if self.error is not None:
                raise ValueError("ToolResult.error must be None when ok is True")
            return

        # ok is False: CONTRACTS.md 3.3's much smaller error envelope.
        if self.error is None:
            raise ValueError("ToolResult.error must be set when ok is False")
        if "code" not in self.error:
            raise ValueError(f"ToolResult.error is missing 'code': {dict(self.error)!r}")
        code_raw = self.error["code"]
        try:
            resolved = ErrorCode(code_raw)
        except ValueError as exc:
            raise ValueError(
                f"ToolResult.error['code']={code_raw!r} is not one of the nine closed "
                f"MCP error codes: {sorted(c.value for c in ErrorCode)}"
            ) from exc
        if resolved is ErrorCode.UNAVAILABLE and set(self.error.keys()) != {"code"}:
            raise ValueError(
                "ToolResult.error for 'unavailable' must be exactly "
                '{"code": "unavailable"} (CONTRACTS.md 3.3), got keys '
                f"{sorted(self.error.keys())}"
            )

        success_only_set = (
            bool(self.rows)
            or bool(self.anchors)
            or self.partial
            or self.continuation is not None
            or self.lease_id is not None
            or self.etag is not None
            or self.replica is not None
            or self.ttl is not None
            or self.deprecated
            or self.successor is not None
        )
        if success_only_set:
            raise ValueError(
                "ToolResult with ok=False must leave every success-only field "
                "(rows/anchors/partial/continuation/lease_id/etag/replica/ttl/"
                "deprecated/successor) at its default — got a mix of the two envelopes"
            )

    def to_dict(self) -> dict:
        """CONTRACTS.md 3.2's full envelope when ``ok`` is True; 3.3's
        exact ``{"ok", "error", "cost"}`` envelope — nothing padded in
        around it — when ``ok`` is False."""
        if not self.ok:
            return {"ok": False, "error": dict(self.error), "cost": self.cost}
        return {
            "ok": True,
            "rows": [dict(r) for r in self.rows],
            "anchors": list(self.anchors),
            "cost": self.cost,
            "partial": self.partial,
            "continuation": self.continuation,
            "lease_id": self.lease_id,
            "etag": self.etag,
            "replica": self.replica,
            "ttl": self.ttl,
            "deprecated": self.deprecated,
            "successor": self.successor,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> "ToolResult":
        """Inverse of :meth:`to_dict`. Handles both envelope shapes,
        selected by ``d["ok"]``."""
        if not d["ok"]:
            return cls(ok=False, error=dict(d["error"]), cost=d.get("cost", 0))
        return cls(
            ok=True,
            rows=tuple(d.get("rows", ())),
            anchors=tuple(d.get("anchors", ())),
            cost=d.get("cost", 0),
            partial=d.get("partial", False),
            continuation=d.get("continuation"),
            lease_id=d.get("lease_id"),
            etag=d.get("etag"),
            replica=d.get("replica"),
            ttl=d.get("ttl"),
            deprecated=d.get("deprecated", False),
            successor=d.get("successor"),
        )


if __name__ == "__main__":
    import json

    print("=== canonicalise_fields() demo ===")
    cases = [
        (("Title", "body", "BODY"), ("body", "title")),
        ((), ()),
        (["*"], ("*",)),
        (["Body", "Title", "body"], ("body", "title")),
    ]
    for given, expected in cases:
        got = canonicalise_fields(given)
        print(f"  canonicalise_fields({given!r}) -> {got!r}")
        assert got == expected, f"expected {expected!r}, got {got!r}"

    print("\n=== ToolCall auto-canonicalises its field mask on construction ===")
    call = ToolCall(
        server="slides",
        tool="query",
        args={"q": "streamable http"},
        fields=("Title", "body", "body"),
    )
    print(f"  ToolCall(..., fields=('Title','body','body')).fields -> {call.fields!r}")
    assert call.fields == ("body", "title")

    print("\n=== ToolCall.to_dict() / from_dict() round-trip ===")
    call2 = ToolCall(
        server="slides",
        tool="get_frame",
        args={"anchor": "Frame:3f2a9c11/w/041"},
        fields=("*",),
        headers={"mcp-replica": "w", "if-match": "sha256:deadbeef"},
        lease_id="lse_7f21",
        call_index=3,
    )
    dumped = json.dumps(call2.to_dict(), sort_keys=True)
    restored = ToolCall.from_dict(json.loads(dumped))
    print(f"  dumped: {dumped}")
    print(f"  ToolCall.from_dict(json.loads(dumped)) == call2 -> {restored == call2}")
    assert restored == call2

    print("\n=== ToolResult success envelope (CONTRACTS.md 3.2) ===")
    ok_result = ToolResult(
        ok=True,
        rows=({"anchor": "Frame:3f2a9c11/w/041", "title": "Streamable HTTP"},),
        anchors=("Frame:3f2a9c11/w/041",),
        cost=6,
        replica="w",
        ttl=30,
    )
    ok_dict = ok_result.to_dict()
    print(f"  ok_result.to_dict() -> {ok_dict}")
    assert set(ok_dict.keys()) == {
        "ok", "rows", "anchors", "cost", "partial", "continuation",
        "lease_id", "etag", "replica", "ttl", "deprecated", "successor",
    }
    ok_roundtrip = ToolResult.from_dict(json.loads(json.dumps(ok_dict, sort_keys=True)))
    print(f"  round-trips through json.dumps/loads -> {ok_roundtrip == ok_result}")
    assert ok_roundtrip == ok_result

    print("\n=== ToolResult error envelope (CONTRACTS.md 3.3): unavailable ===")
    from kit.mcp.errors import make_error

    unavailable_result = ToolResult(ok=False, error=make_error("unavailable"), cost=6)
    unavailable_dict = unavailable_result.to_dict()
    print(f"  unavailable_result.to_dict() -> {unavailable_dict}")
    assert unavailable_dict == {"ok": False, "error": {"code": "unavailable"}, "cost": 6}
    print("  keys are exactly {'ok', 'error', 'cost'} — no success envelope leaked through")
    assert set(unavailable_dict.keys()) == {"ok", "error", "cost"}
    unavailable_roundtrip = ToolResult.from_dict(
        json.loads(json.dumps(unavailable_dict, sort_keys=True))
    )
    assert unavailable_roundtrip == unavailable_result

    print("\n=== ToolResult error envelope: bad_request (extra context allowed) ===")
    bad_req_result = ToolResult(
        ok=False,
        error=make_error("bad_request", reason="unknown field 'nope'"),
        cost=2,
    )
    print(f"  bad_req_result.to_dict() -> {bad_req_result.to_dict()}")
    assert bad_req_result.to_dict() == {
        "ok": False,
        "error": {"code": "bad_request", "reason": "unknown field 'nope'"},
        "cost": 2,
    }

    print("\n=== Rejection demo (each must raise ValueError) ===")

    def _expect_value_error(label: str, fn) -> None:
        try:
            fn()
        except ValueError as exc:
            print(f"  [{label:42}] -> ValueError: {exc}")
        else:
            raise AssertionError(f"expected ValueError for case {label!r}")

    _expect_value_error(
        "ok=True with a non-None error",
        lambda: ToolResult(ok=True, error=make_error("not_found")),
    )
    _expect_value_error("ok=False with no error", lambda: ToolResult(ok=False))
    _expect_value_error(
        "ok=False with an unknown error code",
        lambda: ToolResult(ok=False, error={"code": "teapot"}),
    )
    _expect_value_error(
        "ok=False, unavailable softened with a reason",
        lambda: ToolResult(ok=False, error={"code": "unavailable", "reason": "db down"}),
    )
    _expect_value_error(
        "ok=False but rows carried over from a success envelope",
        lambda: ToolResult(
            ok=False, error=make_error("rate_limited"), rows=({"leaked": True},)
        ),
    )
    _expect_value_error(
        "negative cost",
        lambda: ToolResult(ok=True, cost=-1),
    )
    _expect_value_error(
        "bogus replica",
        lambda: ToolResult(ok=True, replica="x"),
    )

    print("\nAll types.py demos passed.")
