"""kit/isolation/rpc.py — length-prefixed JSON-frame RPC + the ALLOWED_METHODS
allowlist (CONTRACTS.md section 12.2 mechanic 2, FINAL-PLAN.md section 11).

**The wire format.** One frame is a 4-byte big-endian unsigned length prefix
followed by that many bytes of UTF-8 JSON:

    <uint32 BE length><JSON payload>

This is the channel `child_driver.py` speaks over stdin/stdout once it is
running as the sandboxed child (CONTRACTS.md 12: ``sandbox-exec -f duel.sb
python3.12 -m kit.isolation.child_driver``). The **trusted parent** — the
process outside the sandbox that owns the real tool servers — sends
:class:`RpcRequest` frames in; the sandboxed child answers with
:class:`RpcResponse` frames out. A length prefix (rather than newline
framing) means a payload containing a raw newline byte is never ambiguous,
and a reader never has to guess where a JSON value ends.

**The allowlist.** CONTRACTS.md 12.2 mechanic 2, quoted exactly: "The RPC
allowlist stays. ``ALLOWED_METHODS = set(TOOL_SPECS)``; anything else is
rejected, not executed." :data:`ALLOWED_METHODS` here is built as exactly
that expression — the literal key set of
:data:`kit.mcp.specs.TOOL_SPECS`, i.e. a frozenset of ``(server, tool)``
string pairs — imported with the workspace's mandatory graceful-degrade
(hard rule 2): if ``kit.mcp.specs`` is not importable yet, ``TOOL_SPECS``
falls back to ``{}`` and :data:`ALLOWED_METHODS` becomes the *empty* set.
That is a fail-closed default for a security allowlist — everything is
rejected until the real table loads — never fail-open.

**The integrity taxonomy.** CONTRACTS.md 5.2's L1 ``integrity`` event has
exactly one closed ``kind`` enum: ``fs_escape | net_denied | proc_denied |
timeout | malformed_decision``. :class:`IntegrityKind` and
:func:`make_integrity` are this module's version of the pattern
``kit/mcp/errors.py`` already established for the (unrelated) nine-code MCP
error taxonomy: one closed enum, one constructor that refuses an unknown
member. Both :mod:`kit.isolation.sandbox` (fs_escape / net_denied /
proc_denied / timeout, from the OS boundary) and this module's own
allowlist rejection (malformed_decision — "not a metered toolkit call" is,
at the RPC layer, exactly what a malformed decision looks like) build their
records through this one function, so every denial in the isolation
package shares one shape: ``{"kind": ..., "detail": ...}`` — the ``p``
payload CONTRACTS.md 5.2 documents for an ``integrity`` L1 event.
(This module intentionally returns only that inner payload, never a full
envelope — ``v``/``layer``/``seq``/``t``/``run_id``/... belong to the
ledger producer, not to this package.)

RESOLVED AMBIGUITY — :class:`RpcRequest` does **not** import
``kit.mcp.types.ToolCall`` even though its fields overlap. Reusing
``ToolCall`` would work today (it already exists), but it is a
collaborator's file edited concurrently with this one (workspace hard rule
2), and the two types serve different layers: ``ToolCall`` is the
*canonicalised* request the arena hands a tool server; ``RpcRequest`` is
the *wire* request a sandboxed child sends its trusted parent asking for
one to be executed. Keeping this module's own dataclass self-contained
means ``kit/isolation`` never breaks because someone reshapes
``ToolCall`` for an unrelated reason, and the only real dependency this
package takes on ``kit.mcp`` — ``TOOL_SPECS`` for the allowlist — stays
exactly as narrow as CONTRACTS.md requires.

Stdlib only. No network, no randomness, no wall-clock.
"""

from __future__ import annotations

import json
import logging
import struct
from dataclasses import dataclass, field
from enum import StrEnum
from typing import BinaryIO, Mapping

__all__ = [
    "MAX_FRAME_BYTES",
    "RpcFramingError",
    "write_frame",
    "read_frame",
    "IntegrityKind",
    "make_integrity",
    "RpcRequest",
    "RpcResponse",
    "ALLOWED_METHODS",
    "MethodNotAllowed",
    "check_method",
    "reject",
]

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Frame I/O
# ---------------------------------------------------------------------------

# 1 MiB is generous for a single tool_result row batch but not unbounded —
# an attacker-controlled length prefix must not be trusted to allocate an
# arbitrary amount of memory before we've even looked at the payload.
MAX_FRAME_BYTES = 1_048_576

_LEN_STRUCT = struct.Struct(">I")  # 4-byte big-endian unsigned length prefix


class RpcFramingError(Exception):
    """The byte stream did not contain a well-formed length-prefixed JSON
    frame: a truncated length header, a truncated body, a declared length
    over :data:`MAX_FRAME_BYTES`, or a body that is not valid UTF-8 JSON."""


def _read_exact(stream: BinaryIO, n: int) -> bytes:
    """Read exactly ``n`` bytes or fewer at a genuine EOF. A single
    ``stream.read(n)`` call is not guaranteed to return all ``n`` bytes for
    a pipe, so this loops — the standard reason naive frame readers break
    on real stdin/stdout pipes under load."""
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def write_frame(stream: BinaryIO, obj: Mapping[str, object]) -> None:
    """Write one length-prefixed JSON frame to a binary stream and flush.

    ``sort_keys=True`` keeps the wire bytes a pure function of the dict's
    *content*, never its construction order (workspace hard rule 4: no
    dict-iteration-order dependence in any output)."""
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise RpcFramingError(
            f"frame too large to send: {len(payload)} bytes > MAX_FRAME_BYTES={MAX_FRAME_BYTES}"
        )
    stream.write(_LEN_STRUCT.pack(len(payload)))
    stream.write(payload)
    stream.flush()


def read_frame(stream: BinaryIO) -> dict | None:
    """Read one length-prefixed JSON frame from a binary stream.

    Returns ``None`` on a *clean* EOF — nothing at all read where a new
    frame's length header was expected. Any other truncation (a partial
    length header, or a body shorter than its declared length) raises
    :class:`RpcFramingError` rather than being confused with a clean
    shutdown."""
    header = _read_exact(stream, 4)
    if header == b"":
        return None
    if len(header) < 4:
        raise RpcFramingError(f"truncated frame length header: got {len(header)} of 4 bytes")
    (length,) = _LEN_STRUCT.unpack(header)
    if length > MAX_FRAME_BYTES:
        raise RpcFramingError(
            f"declared frame length {length} exceeds MAX_FRAME_BYTES={MAX_FRAME_BYTES}"
        )
    body = _read_exact(stream, length)
    if len(body) < length:
        raise RpcFramingError(f"truncated frame body: got {len(body)} of {length} declared bytes")
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RpcFramingError(f"malformed JSON frame body: {exc}") from exc
    if not isinstance(decoded, dict):
        raise RpcFramingError(f"frame body must decode to a JSON object, got {type(decoded).__name__}")
    return decoded


# ---------------------------------------------------------------------------
# The integrity taxonomy (CONTRACTS.md 5.2)
# ---------------------------------------------------------------------------


class IntegrityKind(StrEnum):
    """The five-member closed ``kind`` enum for an L1 ``integrity`` event
    (CONTRACTS.md 5.2). Nothing in this package ever emits a sixth."""

    FS_ESCAPE = "fs_escape"
    NET_DENIED = "net_denied"
    PROC_DENIED = "proc_denied"
    TIMEOUT = "timeout"
    MALFORMED_DECISION = "malformed_decision"


def make_integrity(kind: "IntegrityKind | str", detail: str = "") -> dict:
    """Build the ``p`` payload of an L1 ``integrity`` event: ``{"kind":
    ..., "detail": ...}``. ``kind`` must resolve to one of the five closed
    members above — anything else raises :class:`ValueError` naming the
    legal set, the same refuse-to-soften pattern
    ``kit.mcp.errors.make_error`` uses for its own closed taxonomy."""
    try:
        resolved = IntegrityKind(kind)
    except ValueError as exc:
        raise ValueError(
            f"{kind!r} is not one of the five closed integrity kinds: "
            f"{sorted(k.value for k in IntegrityKind)}"
        ) from exc
    return {"kind": resolved.value, "detail": str(detail)}


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RpcRequest:
    """One ask, sent child->parent over the RPC pipe: "please execute this
    metered toolkit call and hand back the result." Mirrors the tool-call
    shape CONTRACTS.md 3.1 describes for ``ToolCall`` (server/tool/args/
    fields/headers/lease_id/call_index) without importing that class — see
    the module docstring's resolved-ambiguity note."""

    req_id: str
    server: str
    tool: str
    args: dict = field(default_factory=dict)
    fields: tuple[str, ...] = ()
    headers: dict = field(default_factory=dict)
    lease_id: str | None = None
    call_index: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.req_id, str) or not self.req_id:
            raise ValueError(f"RpcRequest.req_id must be a non-empty str, got {self.req_id!r}")
        if not isinstance(self.server, str) or not self.server:
            raise ValueError(f"RpcRequest.server must be a non-empty str, got {self.server!r}")
        if not isinstance(self.tool, str) or not self.tool:
            raise ValueError(f"RpcRequest.tool must be a non-empty str, got {self.tool!r}")
        if not isinstance(self.args, dict):
            raise ValueError(f"RpcRequest.args must be a dict, got {type(self.args).__name__}")
        if not isinstance(self.headers, dict):
            raise ValueError(f"RpcRequest.headers must be a dict, got {type(self.headers).__name__}")
        if self.lease_id is not None and not isinstance(self.lease_id, str):
            raise ValueError(f"RpcRequest.lease_id must be a str or None, got {self.lease_id!r}")
        if (
            not isinstance(self.call_index, int)
            or isinstance(self.call_index, bool)
            or self.call_index < 0
        ):
            raise ValueError(f"RpcRequest.call_index must be a non-negative int, got {self.call_index!r}")
        object.__setattr__(self, "fields", tuple(self.fields))
        if not all(isinstance(f, str) for f in self.fields):
            raise ValueError(f"RpcRequest.fields must contain only str, got {self.fields!r}")

    def method(self) -> tuple[str, str]:
        """The ``(server, tool)`` pair checked against :data:`ALLOWED_METHODS`."""
        return (self.server, self.tool)

    def to_dict(self) -> dict:
        return {
            "req_id": self.req_id,
            "server": self.server,
            "tool": self.tool,
            "args": dict(self.args),
            "fields": list(self.fields),
            "headers": dict(self.headers),
            "lease_id": self.lease_id,
            "call_index": self.call_index,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> "RpcRequest":
        return cls(
            req_id=d["req_id"],
            server=d["server"],
            tool=d["tool"],
            args=dict(d.get("args", {})),
            fields=tuple(d.get("fields", ())),
            headers=dict(d.get("headers", {})),
            lease_id=d.get("lease_id"),
            call_index=d.get("call_index", 0),
        )


@dataclass(frozen=True, slots=True)
class RpcResponse:
    """One answer, sent parent->child. Exactly one of ``result`` /
    ``error`` is set, matching the ``ok`` flag — the same disjoint-envelope
    discipline ``kit.mcp.types.ToolResult`` uses for the same reason: no
    ambiguous half-filled dict for a caller to speculate from."""

    req_id: str
    ok: bool
    result: Mapping[str, object] | None = None
    error: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.req_id, str) or not self.req_id:
            raise ValueError(f"RpcResponse.req_id must be a non-empty str, got {self.req_id!r}")
        if not isinstance(self.ok, bool):
            raise ValueError(f"RpcResponse.ok must be a bool, got {self.ok!r}")
        if self.ok:
            if self.error is not None:
                raise ValueError("RpcResponse.error must be None when ok is True")
        else:
            if self.error is None:
                raise ValueError("RpcResponse.error must be set when ok is False")
            if self.result is not None:
                raise ValueError("RpcResponse.result must be None when ok is False")
            if "kind" not in self.error:
                raise ValueError(f"RpcResponse.error must carry a 'kind' (integrity shape), got {dict(self.error)!r}")

    def to_dict(self) -> dict:
        if not self.ok:
            return {"req_id": self.req_id, "ok": False, "error": dict(self.error)}
        return {
            "req_id": self.req_id,
            "ok": True,
            "result": dict(self.result) if self.result is not None else {},
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> "RpcResponse":
        if not d["ok"]:
            return cls(req_id=d["req_id"], ok=False, error=dict(d["error"]))
        return cls(req_id=d["req_id"], ok=True, result=dict(d.get("result", {})))


# ---------------------------------------------------------------------------
# The allowlist (CONTRACTS.md 12.2 mechanic 2)
# ---------------------------------------------------------------------------

try:
    from kit.mcp.specs import TOOL_SPECS  # type: ignore[import-not-found]
except (ImportError, AttributeError) as exc:  # pragma: no cover - collaborator file
    _LOG.warning(
        "kit.mcp.specs not available yet (%s) — ALLOWED_METHODS is EMPTY "
        "(fail-closed: every RPC method is rejected until it loads), not the "
        "reverse.",
        exc,
    )
    TOOL_SPECS: Mapping[tuple[str, str], object] = {}

#: CONTRACTS.md 12.2, verbatim: "ALLOWED_METHODS = set(TOOL_SPECS)". A
#: frozenset of (server, tool) string pairs — exactly TOOL_SPECS's key set,
#: nothing recomputed or reshaped.
ALLOWED_METHODS: frozenset[tuple[str, str]] = frozenset(TOOL_SPECS)


class MethodNotAllowed(Exception):
    """Raised by :func:`check_method` for any ``(server, tool)`` pair that
    is not a metered toolkit call. Never executed — CONTRACTS.md 12.2:
    "anything that is not a metered toolkit call is REJECTED, not
    executed."."""

    def __init__(self, server: str, tool: str) -> None:
        self.server = server
        self.tool = tool
        super().__init__(
            f"{server}.{tool} is not in ALLOWED_METHODS "
            f"({len(ALLOWED_METHODS)} metered toolkit calls) — rejected, not executed"
        )


def check_method(server: str, tool: str, allowed: frozenset[tuple[str, str]] = ALLOWED_METHODS) -> None:
    """Raise :class:`MethodNotAllowed` unless ``(server, tool)`` is a real
    metered toolkit call. Never raises for a legal pair."""
    if (server, tool) not in allowed:
        raise MethodNotAllowed(server, tool)


def reject(req_id: str, server: str, tool: str) -> RpcResponse:
    """Build the ``ok=False`` response for a disallowed RPC method — an
    ``integrity`` record of kind ``malformed_decision``, since "asked for
    something that is not a metered toolkit call" is, at this layer,
    exactly what a malformed decision looks like."""
    detail = (
        f"{server}.{tool} is not in ALLOWED_METHODS "
        f"({len(ALLOWED_METHODS)} metered toolkit calls); the call was rejected, not executed"
    )
    return RpcResponse(req_id=req_id, ok=False, error=make_integrity(IntegrityKind.MALFORMED_DECISION, detail))


if __name__ == "__main__":
    import io

    print("=== kit.isolation.rpc: frame I/O round-trip ===")
    buf = io.BytesIO()
    sent = {"req_id": "req:0001", "server": "slides", "tool": "query", "args": {"q": "streamable http"}}
    write_frame(buf, sent)
    buf.seek(0)
    got = read_frame(buf)
    print(f"  wrote {sent!r}")
    print(f"  read  {got!r}")
    assert got == sent

    print("\n=== read_frame on a clean, empty stream returns None ===")
    empty = io.BytesIO(b"")
    assert read_frame(empty) is None
    print("  read_frame(empty BytesIO()) -> None  OK")

    print("\n=== read_frame on a truncated frame raises RpcFramingError ===")
    truncated = io.BytesIO()
    write_frame(truncated, {"a": 1})
    truncated_bytes = truncated.getvalue()[:-2]  # chop the tail off the body
    try:
        read_frame(io.BytesIO(truncated_bytes))
    except RpcFramingError as exc:
        print(f"  RpcFramingError: {exc}")
    else:
        raise AssertionError("expected RpcFramingError on a truncated frame")

    print("\n=== two frames back to back, read in order ===")
    multi = io.BytesIO()
    write_frame(multi, {"n": 1})
    write_frame(multi, {"n": 2})
    multi.seek(0)
    first = read_frame(multi)
    second = read_frame(multi)
    third = read_frame(multi)
    print(f"  {first!r}, {second!r}, then EOF -> {third!r}")
    assert first == {"n": 1} and second == {"n": 2} and third is None

    print(f"\n=== ALLOWED_METHODS: {len(ALLOWED_METHODS)} metered toolkit calls ===")
    for pair in sorted(ALLOWED_METHODS):
        print(f"  {pair[0]}.{pair[1]}")
    if not ALLOWED_METHODS:
        print("  (empty — kit.mcp.specs was not importable at demo time; every method rejects)")

    print("\n=== check_method(): a real pair passes, a bogus one raises ===")
    if ALLOWED_METHODS:
        sample_server, sample_tool = sorted(ALLOWED_METHODS)[0]
        check_method(sample_server, sample_tool)
        print(f"  check_method({sample_server!r}, {sample_tool!r}) -> no raise  OK")
    try:
        check_method("evil", "exec_shell")
    except MethodNotAllowed as exc:
        print(f"  check_method('evil', 'exec_shell') -> MethodNotAllowed: {exc}")
    else:
        raise AssertionError("expected MethodNotAllowed for a bogus method pair")

    print("\n=== reject() builds a malformed_decision integrity response ===")
    resp = reject("req:0099", "evil", "exec_shell")
    print(f"  {resp.to_dict()}")
    assert resp.ok is False
    assert resp.error["kind"] == "malformed_decision"

    print("\n=== make_integrity(): all five kinds, then a rejected sixth ===")
    for kind in IntegrityKind:
        rec = make_integrity(kind, detail=f"demo for {kind.value}")
        print(f"  {rec}")
        assert rec["kind"] == kind.value
    try:
        make_integrity("teapot")
    except ValueError as exc:
        print(f"  make_integrity('teapot') -> ValueError: {exc}")
    else:
        raise AssertionError("expected ValueError for an unknown integrity kind")

    print("\n=== RpcRequest / RpcResponse to_dict/from_dict round-trip ===")
    req = RpcRequest(
        req_id="req:0007",
        server="slides",
        tool="get_frame",
        args={"anchor": "Frame:3f2a9c11/w/041"},
        fields=("title", "Body", "body"),
        headers={"mcp-replica": "w"},
        lease_id="lse_7f21",
        call_index=2,
    )
    req_roundtrip = RpcRequest.from_dict(json.loads(json.dumps(req.to_dict())))
    print(f"  {req.to_dict()}")
    assert req_roundtrip == req
    assert req.fields == ("title", "Body", "body"), "RpcRequest stores fields as-given; canonicalisation is the arena's job upstream"

    resp_ok = RpcResponse(req_id="req:0007", ok=True, result={"rows": []})
    resp_roundtrip = RpcResponse.from_dict(json.loads(json.dumps(resp_ok.to_dict())))
    print(f"  {resp_ok.to_dict()}")
    assert resp_roundtrip == resp_ok

    print("\nAll kit/isolation/rpc.py demos passed.")
