"""kit/broker/base.py — the Broker contract (CONTRACTS.md section 9) and the
shared primitives every implementation in this package is built from.

CONTRACTS.md section 9, verbatim::

    class Broker(Protocol):
        def query(self, messages: list[dict], **kw) -> dict: ...   # returns the raw API message dict

Three implementations exist project-wide: ``LiveBroker`` (arena only, real
DeepSeek calls — never in this package), ``FrozenBroker`` (replay by
canonical prompt hash — :mod:`kit.broker.frozen`), and ``MockBroker``
(deterministic scripted policies, kit only, zero-key —
:mod:`kit.broker.mock`). All three return the identical shape so a caller
can swap implementations without a second code path (FINAL-PLAN.md
section 10: "degraded mode is a swap, not a second code path").

THE INVARIANT THIS PACKAGE EXISTS TO UPHOLD (FINAL-PLAN.md section 2.1):
**the student kit contains no HTTP client that can reach a model endpoint,
and no code path that reads an API key.** Nothing in this file — or
anywhere under ``kit/broker/`` — may import ``urllib.request``,
``http.client``, ``socket``, or ``ssl``, and nothing may read
``os.environ["DEEPSEEK_API_KEY"]`` (or any other environment lookup).
``tests/test_brokers.py`` greps/ASTs this package to enforce it as a
shipped gate, not a comment.

This module provides:

* :class:`Broker` — the ``typing.Protocol`` itself, so callers can type-hint
  against it without caring which concrete class they were handed.
* :func:`validate_broker_message` — checks a returned dict really has the
  "raw API message shape" ``{"role","content","tool_calls"?,
  "reasoning_content"?}`` (CONTRACTS.md section 9's return value, and the
  same shape M0 measured DeepSeek actually returning — FINAL-PLAN.md
  section 10). AMBIGUITY: a genuine tool-calling turn commonly carries
  ``content: null`` even from the real API (the text lives in
  ``tool_calls`` instead) — measured on a live DeepSeek-shaped response,
  not merely assumed. So ``content`` may be ``str`` OR ``None``, and
  ``None`` is only accepted when ``tool_calls`` is present and non-empty;
  a final-answer turn (no ``tool_calls``) must carry a real ``str``.
* :func:`final_message` / :func:`tool_call_message` / :func:`make_tool_call`
  — small constructors both concrete brokers build their turns from, so the
  message shape is defined in exactly one place.
* :func:`canonical_prompt_hash` and its helpers — CONTRACTS.md section 9's
  "hashing must be canonical: sort keys, strip reasoning_content, normalise
  whitespace" rule for :class:`~kit.broker.frozen.FrozenBroker`, kept here
  (rather than private to ``frozen.py``) because it is a pure function of
  "what shape is a prompt", not of replay specifically — a future recorder
  or a test wanting "would these two prompts hit the same frozen entry?"
  needs exactly this function, not a private one buried in the replay class.

Stdlib only. No network, no randomness, no wall-clock.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

__all__ = [
    "Broker",
    "validate_broker_message",
    "final_message",
    "tool_call_message",
    "make_tool_call",
    "canonical_prompt_hash",
    "canonicalize_message",
]


# ---------------------------------------------------------------------------
# The Protocol (CONTRACTS.md section 9)
# ---------------------------------------------------------------------------
@runtime_checkable
class Broker(Protocol):
    """``query(messages, **kw) -> dict`` — CONTRACTS.md section 9, verbatim.

    ``messages`` is the running chat history in the standard
    role/content(/tool_calls) shape; ``**kw`` absorbs whatever a live
    endpoint would accept (``temperature``, ``max_tokens``,
    ``response_format``, ...) so every implementation shares one call
    signature even though only ``LiveBroker`` (arena-only, not in this
    package) actually reads any of it. The return value is one assistant
    turn in that same raw shape — see :func:`validate_broker_message`.
    """

    def query(self, messages: list[dict], **kw: object) -> dict: ...


# ---------------------------------------------------------------------------
# Message shape validation
# ---------------------------------------------------------------------------
def validate_broker_message(message: Mapping[str, object]) -> None:
    """Raise ``ValueError``/``TypeError`` unless ``message`` is a legal
    "raw API message" (CONTRACTS.md section 9): ``role == "assistant"``,
    ``content`` a ``str`` (or ``None`` iff a non-empty ``tool_calls`` is
    present), an optional non-empty ``tool_calls`` list of well-formed
    OpenAI/DeepSeek-shaped function-call entries, and an optional
    ``reasoning_content`` that is a ``str`` or ``None`` when present.

    Every concrete :class:`Broker` in this package calls this on its own
    return value before handing it back — a broker that returns a
    malformed message is a broker bug, and this function is what turns
    that bug into an immediate, loud failure instead of a confusing one
    three modules downstream.
    """
    if not isinstance(message, Mapping):
        raise TypeError(f"broker message must be a dict, got {type(message).__name__}")

    if message.get("role") != "assistant":
        raise ValueError(f"broker message must have role=='assistant', got {message.get('role')!r}")

    has_calls = "tool_calls" in message and message["tool_calls"]
    content = message.get("content")
    if content is None:
        if not has_calls:
            raise ValueError(
                "broker message 'content' is None but carries no 'tool_calls' — "
                "a final-answer turn (no tool_calls) must have real str content"
            )
    elif not isinstance(content, str):
        raise ValueError(f"broker message 'content' must be a str or None, got {type(content).__name__}")

    if "tool_calls" in message:
        calls = message["tool_calls"]
        if not isinstance(calls, list) or not calls:
            raise ValueError("broker message 'tool_calls', when present, must be a non-empty list")
        for i, call in enumerate(calls):
            if not isinstance(call, Mapping):
                raise ValueError(f"tool_calls[{i}] must be a dict, got {call!r}")
            if not isinstance(call.get("id"), str) or not call["id"]:
                raise ValueError(f"tool_calls[{i}] missing non-empty 'id': {call!r}")
            if call.get("type") != "function":
                raise ValueError(f"tool_calls[{i}]['type'] must be 'function', got {call.get('type')!r}")
            fn = call.get("function")
            if not isinstance(fn, Mapping):
                raise ValueError(f"tool_calls[{i}]['function'] must be a dict, got {fn!r}")
            name = fn.get("name")
            if not isinstance(name, str) or not name or "." not in name:
                raise ValueError(
                    f"tool_calls[{i}]['function']['name'] must be a dotted 'server.tool' str, got {name!r}"
                )
            args = fn.get("arguments")
            if not isinstance(args, str):
                raise ValueError(
                    f"tool_calls[{i}]['function']['arguments'] must be a JSON-encoded str, got {type(args).__name__}"
                )
            try:
                json.loads(args)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(
                    f"tool_calls[{i}]['function']['arguments'] is not valid JSON: {args!r}"
                ) from exc

    if "reasoning_content" in message:
        rc = message["reasoning_content"]
        if rc is not None and not isinstance(rc, str):
            raise ValueError(f"broker message 'reasoning_content' must be a str or None, got {type(rc).__name__}")


# ---------------------------------------------------------------------------
# Message constructors — the ONE place either concrete broker builds a turn
# ---------------------------------------------------------------------------
def make_tool_call(name: str, args: Mapping[str, object], *, call_id: str) -> dict:
    """One OpenAI/DeepSeek-shaped ``tool_calls`` entry. ``name`` is dotted
    ``"server.tool"`` (matching the convention ``kit/mcp/specs.py`` already
    uses for a deprecated tool's ``successor`` field) so a downstream
    intercept/canonicalise step can split it on ``"."`` into ``(server,
    tool)`` without a second naming scheme to learn. ``args`` is serialised
    with ``sort_keys=True`` — hard rule 4: never a dict-iteration-order
    dependent output."""
    if "." not in name:
        raise ValueError(f"make_tool_call: name must be dotted 'server.tool', got {name!r}")
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(dict(args), sort_keys=True, ensure_ascii=True),
        },
    }


def final_message(content: str, *, reasoning_content: str | None = None) -> dict:
    """A terminal assistant turn: real text, no ``tool_calls`` key at all
    (CONTRACTS.md section 9's ``"tool_calls"?`` — the key is genuinely
    absent, not ``null``, when there is nothing to call)."""
    if not isinstance(content, str) or not content:
        raise ValueError(f"final_message: content must be a non-empty str, got {content!r}")
    out: dict = {"role": "assistant", "content": content}
    if reasoning_content is not None:
        out["reasoning_content"] = reasoning_content
    return out


def _with_action_fence(content: str | None, calls: Sequence[Mapping[str, object]]) -> str:
    """Render the FIRST call in `kit/loop/agent.py`'s action grammar:

        ```action
        MCP slides.query q="streamable http" fields=title,anchor
        ```

    Only the first call is rendered: the loop requires EXACTLY ONE fence per turn
    (zero or several both raise), and it issues one command per iteration anyway.
    """
    import json as _j

    call = dict(calls[0])
    fn = dict(call.get("function") or {})
    name = str(fn.get("name") or call.get("name") or "mcp_execute")
    try:
        args = _j.loads(fn.get("arguments") or "{}")
    except (ValueError, TypeError):
        args = {}
    if not isinstance(args, dict):
        args = {}

    server = str(args.pop("server", "") or "")
    tool = str(args.pop("tool", "") or "")
    if not server or not tool:
        # A flat tool name like `slides_query` or `slides.query`.
        base = name.replace("mcp_execute", "").strip("_. ") or name
        if "." in base:
            server, tool = base.split(".", 1)
        elif "_" in base:
            server, tool = base.split("_", 1)
        else:
            server, tool = "slides", base or "query"

    verb = "A2A" if "-" in server else "MCP"
    parts = [f"{verb} {server}.{tool}"]

    fields = args.pop("fields", None)
    if isinstance(fields, (list, tuple)) and fields:
        parts.append("fields=" + ",".join(str(f) for f in fields))
    lease = args.pop("lease_id", None) or args.pop("lease", None)
    if lease:
        parts.append(f"lease={lease}")
    for key, value in dict(args.pop("headers", {}) or {}).items():
        parts.append(f'header.{str(key).lower()}="{value}"')
    for key, value in args.items():
        text = str(value)
        parts.append(f'{key}="{text}"' if (" " in text or not text) else f"{key}={text}")

    fence = "```action\n" + " ".join(parts) + "\n```"
    prose = (content or "").strip()
    return f"{prose}\n\n{fence}" if prose else fence


def tool_call_message(
    content: str | None, calls: Sequence[Mapping[str, object]], *, reasoning_content: str | None = None
) -> dict:
    """A tool-calling assistant turn. ``content`` may be ``None`` (matching
    what a real endpoint sends on a pure tool-call turn) or a short
    human-readable rationale string — this package always supplies the
    latter, for a legible transcript."""
    if not calls:
        raise ValueError("tool_call_message: calls must be non-empty")
    # ★ ALSO RENDER THE CALL AS AN ```action FENCE IN `content`.
    #
    # This package speaks the OpenAI `tool_calls` convention. `kit/loop/agent.py`
    # deliberately does NOT — its own docstring says "there is no tool-calling JSON
    # schema in this loop", and it parses exactly one ```action fenced block out of the
    # assistant's TEXT. Two conventions, two modules, each fully tested against itself.
    #
    # The consequence was total and silent: every mock-driven turn arrived with prose in
    # `content` and no fence, the loop raised "expected exactly one ```action fenced
    # block, found 0" twice, hit its format-error cap, and produced an EMPTY ANSWER WITH
    # NO TOOL CALLS. A whole 16-duel bracket ran to completion with all 160 rounds at
    # 100-100 and the health metric flagging "the format has a problem" — when the real
    # problem was that the agent never took a single action.
    #
    # Emitting both keeps `tool_calls` for anything that wants the structured form and
    # gives the loop the fence it actually parses.
    out: dict = {"role": "assistant",
                 "content": _with_action_fence(content, calls),
                 "tool_calls": [dict(c) for c in calls]}
    if reasoning_content is not None:
        out["reasoning_content"] = reasoning_content
    return out


# ---------------------------------------------------------------------------
# Canonical prompt hashing (CONTRACTS.md section 9 / FrozenBroker's backbone)
# ---------------------------------------------------------------------------
import hashlib  # noqa: E402 - grouped with the other stdlib-only hashing code, not a broker/network import


def _looks_like_json_container(s: str) -> bool:
    stripped = s.strip()
    return stripped[:1] in "{["


def _canonical_json_string(s: str) -> str:
    """Whitespace-normalise ``s``; additionally re-serialise it in
    sorted-key, compact form if it parses as a JSON object/array (the shape
    ``tool_calls[*].function.arguments`` and JSON-carrying tool-result
    ``content`` strings take) — so two payloads that differ only in key
    order or incidental spacing still hash identically."""
    normalized = " ".join(s.split())
    if not normalized or not _looks_like_json_container(s):
        return normalized
    try:
        parsed = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return normalized
    return json.dumps(_canon(parsed), sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _canon(value: object) -> object:
    """Recursively: drop every ``reasoning_content`` key at any nesting
    depth, sort every dict's keys, and whitespace/JSON-normalise every
    string leaf. Pure, total (never raises on odd-shaped input — an
    unrecognised leaf type is returned unchanged)."""
    if isinstance(value, Mapping):
        return {k: _canon(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0])) if k != "reasoning_content"}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    if isinstance(value, str):
        return _canonical_json_string(value)
    return value


def canonicalize_message(message: Mapping[str, object]) -> dict:
    """The canonical form of one message dict — ``reasoning_content``
    stripped, keys sorted, string content whitespace/JSON-normalised.
    Exposed standalone (not only inlined into
    :func:`canonical_prompt_hash`) because a caller may want to compare two
    single messages for prompt-equivalence without hashing a whole
    conversation."""
    if not isinstance(message, Mapping):
        raise TypeError(f"canonicalize_message: expected a dict, got {type(message).__name__}")
    canon = _canon(dict(message))
    assert isinstance(canon, dict)  # a top-level Mapping always canonicalises to a dict
    return canon


def canonical_prompt_hash(messages: Sequence[Mapping[str, object]]) -> str:
    """``"sha256:<16 hex>"`` of the canonical form of ``messages``
    (CONTRACTS.md section 9: "sort keys, strip reasoning_content, normalise
    whitespace"). Same 16-hex-char convention as ``kit/world/page.py``'s
    ``compute_etag`` — this codebase's one house style for a short content
    fingerprint. This is THE lookup key :class:`~kit.broker.frozen.FrozenBroker`
    uses against a recorded bundle, and the reason G-REPRO
    (CONTRACTS.md section 11: replay one exchange 10x, mean |Δdamage| < 2 HP)
    is achievable at all — two calls that differ only in incidental
    formatting (a recorder's JSON pretty-printing, `reasoning_content` that
    should never have been echoed back into history) must still resolve to
    the same recorded response.
    """
    if not isinstance(messages, (list, tuple)):
        raise TypeError(f"canonical_prompt_hash: messages must be a list, got {type(messages).__name__}")
    canon_list = [canonicalize_message(m) for m in messages]
    blob = json.dumps(canon_list, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


if __name__ == "__main__":
    print("=== kit.broker.base: Broker protocol + message shape ===\n")

    print("=== validate_broker_message() — accepted shapes ===")
    ok_final = final_message("Streamable HTTP replaced HTTP+SSE on 2026-07-28.")
    validate_broker_message(ok_final)
    print(f"  final_message(...) -> {ok_final}")
    print("  validate_broker_message(ok_final): OK")

    call = make_tool_call("slides.query", {"q": "streamable http", "fields": ["title"]}, call_id="call_0")
    ok_call = tool_call_message("Looking up streamable http.", [call], reasoning_content="checking the deck first")
    validate_broker_message(ok_call)
    print(f"\n  tool_call_message(...) -> {ok_call}")
    print("  validate_broker_message(ok_call): OK")

    ok_null_content = {"role": "assistant", "content": None, "tool_calls": [call]}
    validate_broker_message(ok_null_content)
    print(f"\n  content=None permitted alongside tool_calls -> {ok_null_content}")
    print("  validate_broker_message(ok_null_content): OK")

    print("\n=== validate_broker_message() — rejected shapes (each must raise) ===")

    def _expect_error(label: str, fn) -> None:
        try:
            fn()
        except (ValueError, TypeError) as exc:
            print(f"  [{label:46}] -> {type(exc).__name__}: {exc}")
        else:
            raise AssertionError(f"expected an error for case {label!r}")

    _expect_error("role != assistant", lambda: validate_broker_message({"role": "user", "content": "hi"}))
    _expect_error(
        "content=None with no tool_calls",
        lambda: validate_broker_message({"role": "assistant", "content": None}),
    )
    _expect_error(
        "tool_calls present but empty",
        lambda: validate_broker_message({"role": "assistant", "content": "x", "tool_calls": []}),
    )
    _expect_error(
        "tool name not dotted server.tool",
        lambda: validate_broker_message(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "search", "arguments": "{}"}}],
            }
        ),
    )
    _expect_error(
        "arguments not valid JSON",
        lambda: validate_broker_message(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "slides.query", "arguments": "{not json"}}
                ],
            }
        ),
    )

    print("\n=== canonical_prompt_hash() — CONTRACTS.md section 9's three normalisations ===")
    base_messages = [
        {"role": "system", "content": "You are the COLOSSEUM agent."},
        {"role": "user", "content": "Which day covers streamable http?"},
        ok_call,
    ]
    h1 = canonical_prompt_hash(base_messages)
    print(f"  hash(base) = {h1}")

    # 1. whitespace-only differences must not change the hash.
    whitespace_variant = [
        {"role": "system", "content": "  You are   the COLOSSEUM\nagent.  "},
        {"role": "user", "content": "Which  day covers streamable http?"},
        ok_call,
    ]
    h2 = canonical_prompt_hash(whitespace_variant)
    print(f"  hash(whitespace-variant) = {h2}  (equal: {h1 == h2})")
    assert h1 == h2

    # 2. reasoning_content must be stripped before hashing.
    with_reasoning = list(base_messages[:-1]) + [
        {**ok_call, "reasoning_content": "a completely different chain of thought, 400 tokens of it"}
    ]
    h3 = canonical_prompt_hash(with_reasoning)
    print(f"  hash(+reasoning_content) = {h3}  (equal: {h1 == h3})")
    assert h1 == h3

    # 3. key order inside a dict must not change the hash.
    reordered = [
        {"content": base_messages[0]["content"], "role": base_messages[0]["role"]},
        {"content": base_messages[1]["content"], "role": base_messages[1]["role"]},
        {"tool_calls": ok_call["tool_calls"], "content": ok_call["content"], "role": ok_call["role"]},
    ]
    h4 = canonical_prompt_hash(reordered)
    print(f"  hash(key-order-swapped) = {h4}  (equal: {h1 == h4})")
    assert h1 == h4

    # 4. an actual content change MUST change the hash (the hash isn't
    #    accidentally constant).
    different = [
        {"role": "system", "content": "You are the COLOSSEUM agent."},
        {"role": "user", "content": "Which day covers field masks?"},  # different question
        ok_call,
    ]
    h5 = canonical_prompt_hash(different)
    print(f"  hash(different question) = {h5}  (equal to base: {h1 == h5})")
    assert h1 != h5

    print("\nAll kit/broker/base.py demos passed.")
