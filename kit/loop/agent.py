"""kit/loop/agent.py — the vendored control loop (mini-swe-agent's shape,
~220 readable lines, MIT-spirited) and, per this task's brief, "the loop
side" of CONTRACTS.md section 4's trusted envelope: the canonicaliser.

WHAT THIS FILE IS NOT (DELETE-BY-DESIGN, read this before touching it)
------------------------------------------------------------------------
mini-swe-agent's own default environment (`LocalEnvironment`) runs the
model's action as a real bash command via `subprocess.run(["bash", "-c",
action], ...)`. THIS KIT HAS NO SUCH CLASS AND MUST NEVER GROW ONE. Every
action here is a canonicalised MCP / A2A / discover Command, or a final
ANSWER — never a shell command against the host. There is no `bash`, no
`subprocess`, no `os.system` anywhere below. If a future edit adds a
"local" or "shell" environment to this file, it has broken the game: the
kernel sandbox (CONTRACTS.md section 12) is built entirely around there
being no legitimate reason for student code to reach a process boundary,
and RULES.md section 2 makes `subprocess` a submission-time disqualifier.

THE SEAM THIS FILE OWNS (CONTRACTS.md section 4)
------------------------------------------------------------------------
    [ trusted ]   loop emits a raw action                 <- this file
         v
    [ trusted ]   INTERCEPT + CANONICALISE -> Command      <- this file
         v
    [ UNTRUSTED ] Gateway.decide(cmd) -> Decision           <- agent/gateway.py (student's)
         v
    [ trusted ]   ENFORCE, meter, execute or refuse         <- the Environment this file is handed
         v
    [ trusted ]   RECORD the L1 event, RENDER the Observation
         v
    [ trusted ]   the model sees the Observation            <- this file

Everything from `Gateway.decide` through "RECORD/RENDER" happens *inside*
whatever concrete `Environment` this module is handed — `Agent` never
imports `agent.gateway`, never sees a `Decision`, and never executes a
tool call itself. Its job stops at producing a canonicalised action dict
and handing it to `env.execute(action)`; everything downstream of that
call is arena-owned trust boundary, not loop code. That is what makes the
loop auditable in ten minutes: it has nothing to hide because it is not
where enforcement happens.

THE ACTION GRAMMAR (this file's own local decision — not in CONTRACTS.md)
------------------------------------------------------------------------
"there is no tool-calling JSON schema in this loop — that is deliberate"
(this task's brief). Every model turn must contain **exactly one** fenced
block:

    ```action
    VERB rest...
    ```

`VERB` is one of `MCP` `A2A` `DISCOVER` `ANSWER` (case-insensitive) — the
same three names CONTRACTS.md section 4.1's `Command.kind` uses, plus the
loop's own fourth verb for submitting a final answer (`Command.kind` has
no `"answer"` member: an answer is not a tool call routed to a server, so
it never becomes a `Command` at all — see `_canonicalise_answer` below).

For `MCP` / `A2A` / `DISCOVER`, `rest` is one `server.tool` token followed
by whitespace-separated `key=value` pairs (quote a value with spaces:
`q="streamable http"` — parsed with `shlex.split`, so quoting rules are
ordinary shell quoting). Two keys are reserved:

    fields=title,body        -> canonicalised (sorted/deduped/lowercased,
                                 CONTRACTS.md 4.1) into the Command's
                                 `fields` tuple. Omitted -> `()`, meaning
                                 "the tool's default mask" (kit/mcp/specs.py).
    lease=lse_7f21            -> the Command's `lease_id`.
    header.if-match=sha256:.. -> folded into `headers`, key lowercased
                                 (CONTRACTS.md 4.1: "already lowercased
                                 keys"); `header.` prefix stripped.

Every other `key=value` becomes a `(key, value)` pair in `args`, **key and
value both taken verbatim** — CONTRACTS.md 4.1 only names `fields` and
header *keys* as canonicalised; args are opaque, tool-specific payload the
gateway and (eventually) a prosecutor need exactly as the model wrote it,
not lowercased or reordered by this file.

For `ANSWER`, `rest` is a JSON object (may span multiple lines — the fence
is what delimits it, not end-of-line) with at minimum a non-empty string
`"text"` field; an optional `"cited_anchors"` list of anchor strings; and
whatever else the exchange's ask `require`s (CONTRACTS.md section 7) —
this loop does not know or enforce ask-specific shape, only the two
universal fields the L1 `answer` event and the span-citation convention
(CONTRACTS.md 6.1) depend on.

`kind` IS NEVER TAKEN FROM THE MODEL'S VERB. It is derived from
`(server, tool)` identity: `server` in the known A2A peer set -> `"a2a"`;
`tool` in the small set of discovery-shaped tools -> `"discover"`;
otherwise `"mcp"`. This makes the *label* the model chose purely
pedagogical framing — a model cannot talk its way into a different `kind`
by writing the "wrong" verb, and the canonicaliser cannot be fooled the
other direction either. See the module-level `A2A_PEERS` /
`DISCOVERY_TOOLS` constants.

CRITICAL INVARIANT — `server`/`tool` ARE NEVER VALIDATED AGAINST A KNOWN
TABLE HERE. An unrecognised `server.tool` canonicalises cleanly (falling
into `kind="mcp"` by default) and is passed straight through to
`env.execute()`. FINAL-PLAN.md section 4.4's `shadow_server` mutation
class — an attack that redirects or forges a server identity — depends on
exactly this: the trusted downstream layer must be the one that notices
(or is fooled by) an unknown/spoofed server, never the loop rejecting it
on sight. `test_unknown_server_passes_through_uncontested` in
tests/test_loop.py pins this down; do not "fix" it by adding a lookup.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from kit.loop.limits import FormatError, LimitsExceeded, Limits, TimeExceeded

# kit.mcp.specs is a collaborator's file (hard rule 2: import it and degrade
# gracefully if it is not there yet). It IS present as of this writing, but
# this module must keep working even if that ever changes mid-edit.
try:
    from kit.mcp.specs import A2A_PEERS as _A2A_PEERS
except ImportError:
    _A2A_PEERS = frozenset({"curriculum-analyst", "citation-checker", "roster"})

try:
    from kit.mcp.types import canonicalise_fields
except ImportError:  # pragma: no cover - degrade gracefully, hard rule 2

    def canonicalise_fields(fields: Any) -> tuple[str, ...]:
        """Local fallback, kept byte-identical to kit/mcp/types.py's rule:
        sort, dedupe, lowercase. `()` and `("*",)` round-trip unchanged."""
        if isinstance(fields, (str, bytes)):
            raise TypeError(
                f"fields must be an iterable of field-name strings, not a bare "
                f"{type(fields).__name__} ({fields!r})"
            )
        lowered: set[str] = set()
        for f in fields:
            if not isinstance(f, str):
                raise TypeError(f"field mask entries must be str, got {f!r}")
            lowered.add(f.lower())
        return tuple(sorted(lowered))


__all__ = [
    "Model",
    "Environment",
    "ActionParseError",
    "RunResult",
    "Agent",
    "A2A_PEERS",
    "DISCOVERY_TOOLS",
    "VERBS",
    "ACTION_GRAMMAR",
    "extract_action_fence",
    "canonicalise_action",
    "render_observation",
]

# A copy under this module's own name: prompt.py and tests import it from
# here so the tool-catalog-dependent classification stays a single source
# of truth regardless of whether kit.mcp.specs was importable above.
A2A_PEERS: frozenset[str] = frozenset(_A2A_PEERS)

# CONTRACTS.md 4.2 mechanic 2: "a lease_id from a search/locate"; mechanic
# 8 names `slides.search`'s successor as `slides.query`, and `list_servers`
# / `list_terms` are the two "punishment button" full-catalog reads
# (FINAL-PLAN.md section 4.1) — all five are "browse what exists" calls,
# never "read one specific anchor" calls, which is the DISCOVER/MCP split
# this loop's grammar teaches.
DISCOVERY_TOOLS: frozenset[str] = frozenset({"search", "locate", "list_servers", "list_terms"})

VERBS: frozenset[str] = frozenset({"MCP", "A2A", "DISCOVER", "ANSWER"})


@runtime_checkable
class Model(Protocol):
    """Duck-typed. CONTRACTS.md section 9's `Broker` shape, reused as the
    loop's model interface: `query(messages, **kw)` returns the raw API
    message dict (at minimum a `"content"` string)."""

    def query(self, messages: list[dict], **kw: Any) -> dict: ...


@runtime_checkable
class Environment(Protocol):
    """Duck-typed. The single execution boundary: everything from
    `Gateway.decide` through recording the authoritative L1 event and
    rendering the next `Observation` lives inside whatever concrete class
    satisfies this protocol — never inside `Agent` (see module docstring).
    """

    def execute(self, action: dict) -> dict: ...


class ActionParseError(ValueError):
    """Raised by `extract_action_fence` / `canonicalise_action` when a
    model turn cannot be turned into a well-formed action. Caught inside
    `Agent._next_action` and turned into a one-shot repair prompt
    (`Limits.max_consecutive_format_errors`); never escapes `Agent.run()`
    on its own — a second consecutive one becomes `FormatError` instead."""


_FENCE_RE = re.compile(r"```action[ \t]*\r?\n(.*?)\n```", re.DOTALL)
_VERB_RE = re.compile(r"^\s*(MCP|A2A|DISCOVER|ANSWER)\b(.*)$", re.IGNORECASE | re.DOTALL)
_SERVER_TOOL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\.([A-Za-z][A-Za-z0-9_]*)$")

ACTION_GRAMMAR = """Every turn, reply with EXACTLY ONE fenced block:

```action
VERB rest...
```

VERB is one of MCP, A2A, DISCOVER, ANSWER (case-insensitive).

MCP / A2A / DISCOVER — one server.tool token, then space-separated
key=value pairs (quote values with spaces: q="streamable http"):

    MCP slides.get_frame anchor=Frame:3f2a9c11/w/041 fields=title,body lease=lse_7f21
    A2A curriculum-analyst.which_days_cover concept=Concept:streamable-http fields=anchor,course_day,track
    DISCOVER registry.list_servers fields=name

  fields=a,b   -> the field mask (omit for the tool's default mask)
  lease=...    -> the lease_id from a prior search/locate
  header.KEY=v -> a request header, e.g. header.if-match=sha256:...

Every other key=value becomes an argument, exactly as written.

ANSWER — a JSON object with a non-empty "text" field and, where useful,
"cited_anchors" (a list of anchors your answer is grounded in), plus any
fields the question asked for:

```action
ANSWER {"text": "...", "cited_anchors": ["Frame:3f2a9c11/w/041"]}
```

ANSWER ends the exchange. Nothing you write after it is read."""


def extract_action_fence(content: str | None) -> str:
    """Pull the single ```action fenced block's body out of one model
    turn's raw text. Exactly one fence is required — zero or several both
    raise `ActionParseError` (a model padding its action with prose
    outside the fence is fine; a SECOND fence, or none at all, is not)."""
    text = content or ""
    matches = _FENCE_RE.findall(text)
    if len(matches) != 1:
        raise ActionParseError(
            f"expected exactly one ```action fenced block, found {len(matches)}. "
            "See the grammar in the system prompt."
        )
    body = matches[0].strip("\n")
    if not body.strip():
        raise ActionParseError("the ```action fenced block is empty")
    return body


def canonicalise_action(body: str, *, call_index: int | None) -> dict[str, Any]:
    """THE canonicaliser: a fenced block's body -> a Command-shaped dict
    (CONTRACTS.md 4.1's `Command`, minus the arena-minted `cmd_id`), or an
    `"answer"`-kind dict for a final ANSWER (see module docstring — an
    answer is never a `Command`).

    `call_index` is supplied by the caller (`Agent`), not computed here: it
    must count *emitted commands*, never model iterations or fence
    attempts, so a format-error turn never opens a gap a card's
    `{"on": "call_index", "gte": N}` trigger (CONTRACTS.md section 8) could
    misfire on. Pass whatever the next command's 0-based index should be;
    an `"answer"` action's `call_index` in the returned dict is always
    `None` regardless of what was passed, since it is not a command at all.
    """
    m = _VERB_RE.match(body)
    if not m:
        raise ActionParseError(
            f"action must start with one of {sorted(VERBS)}, got: {body[:60]!r}"
        )
    verb = m.group(1).upper()
    rest = m.group(2).strip()
    if not rest:
        raise ActionParseError(f"{verb} action has no content after the verb")

    if verb == "ANSWER":
        return _canonicalise_answer(rest, raw=body)
    return _canonicalise_call(verb, rest, raw=body, call_index=call_index)


def _canonicalise_answer(rest: str, *, raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(rest)
    except json.JSONDecodeError as exc:
        raise ActionParseError(f"ANSWER payload is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ActionParseError(
            f"ANSWER payload must be a JSON object, got {type(payload).__name__}"
        )
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ActionParseError("ANSWER payload must include a non-empty string 'text' field")
    cited = payload.get("cited_anchors", [])
    if not isinstance(cited, list) or not all(isinstance(a, str) for a in cited):
        raise ActionParseError(
            "ANSWER payload's 'cited_anchors', if present, must be a list of anchor strings"
        )
    return {
        "kind": "answer",
        "raw": raw,
        "server": None,
        "tool": None,
        "args": payload,
        "fields": (),
        "headers": {},
        "lease_id": None,
        "call_index": None,
    }


def _canonicalise_call(verb: str, rest: str, *, raw: str, call_index: int | None) -> dict[str, Any]:
    try:
        tokens = shlex.split(rest)
    except ValueError as exc:
        raise ActionParseError(f"could not tokenise {verb} action: {exc}") from exc
    if not tokens:
        raise ActionParseError(f"{verb} action has no server.tool token")

    server_tool_tok, *kv_tokens = tokens
    st_match = _SERVER_TOOL_RE.match(server_tool_tok)
    if not st_match:
        raise ActionParseError(
            f"expected 'server.tool', got {server_tool_tok!r} in {verb} action"
        )
    # Lowercased (the identity token itself), NEVER validated against a
    # known-tool table — see the module docstring's CRITICAL INVARIANT.
    server = st_match.group(1).lower()
    tool = st_match.group(2).lower()

    args: dict[str, str] = {}
    fields: tuple[str, ...] = ()
    headers: dict[str, str] = {}
    lease_id: str | None = None

    for tok in kv_tokens:
        if "=" not in tok:
            raise ActionParseError(f"expected key=value, got bare token {tok!r} in {verb} action")
        raw_key, _, value = tok.partition("=")
        key = raw_key.strip()
        if not key:
            raise ActionParseError(f"empty key in {tok!r}")
        key_lower = key.lower()

        if key_lower == "fields":
            parts = [v for v in value.split(",") if v.strip()]
            fields = canonicalise_fields(parts) if parts else ()
        elif key_lower == "lease":
            lease_id = value
        elif key_lower.startswith("header."):
            header_name = key_lower[len("header.") :]
            if not header_name:
                raise ActionParseError(f"empty header name in {tok!r}")
            headers[header_name] = value
        else:
            # Verbatim, both key and value: CONTRACTS.md 4.1 canonicalises
            # only `fields` and header keys, never args (see module docstring).
            if key in args:
                raise ActionParseError(f"duplicate key {key!r} in {verb} action")
            args[key] = value

    if server in A2A_PEERS:
        resolved_kind = "a2a"
    elif tool in DISCOVERY_TOOLS:
        resolved_kind = "discover"
    else:
        resolved_kind = "mcp"

    return {
        "kind": resolved_kind,
        "raw": raw,
        "server": server,
        "tool": tool,
        "args": args,
        "fields": fields,
        "headers": headers,
        "lease_id": lease_id,
        "call_index": call_index,
    }


def render_observation(observation: Mapping[str, Any]) -> str:
    """Turn an `Environment.execute()` result into the text the model sees
    next. `sort_keys=True` — hard rule 4 bans dict-iteration-order
    dependence in any output, and this string becomes part of the message
    history that the whole exchange (and G-REPRO's replay check) must
    reproduce identically."""
    return json.dumps(dict(observation), sort_keys=True, default=str)


def _repair_prompt(exc: ActionParseError) -> str:
    return (
        f"Your last turn could not be parsed: {exc}\n\n"
        "Reply with EXACTLY ONE ```action fenced block, starting with one of "
        "MCP, A2A, DISCOVER, ANSWER. This is your one repair attempt — a second "
        "malformed turn in a row ends the exchange."
    )


@dataclass(slots=True)
class RunResult:
    """What `Agent.run()` returns on a clean finish (kind=="answer"). A
    limits/time/format termination does NOT produce one of these — per
    this task's brief, those raise instead (`kit.loop.limits`'s three
    exceptions); a caller that wants the partial transcript after catching
    one reads it straight off the `Agent` instance (`agent.messages`,
    `agent.observations`), which the exception itself does not carry."""

    exit_status: str
    steps: int
    calls: int
    elapsed: float
    answer: dict[str, Any] | None
    messages: tuple[dict[str, Any], ...]
    observations: tuple[dict[str, Any], ...]


class Agent:
    """The control loop. `run()` alternates `model.query()` and
    `env.execute()` until the model submits an ANSWER or a limit fires.

    `clock` is injectable (defaults to `time.monotonic`) so
    `wall_time_limit` is testable with a fake, ticking clock instead of a
    real sleep — no real time ever needs to pass for
    `test_wall_time_limit_raises_time_exceeded` to be exact and fast.
    """

    def __init__(
        self,
        model: Model,
        env: Environment,
        *,
        system_prompt: str,
        task: str,
        limits: Limits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model = model
        self.env = env
        self.limits = limits or Limits()
        self._clock = clock
        self._start = self._clock()
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]
        self._observations: list[dict[str, Any]] = []
        self._steps = 0
        self._calls = 0
        self._consecutive_format_errors = 0
        self._cost_spent = 0
        self._credits_left: int | None = None

    def elapsed(self) -> float:
        """Seconds since construction, via the injected clock. Never
        `time.time()`/`datetime.now()` — hard rule 4."""
        return self._clock() - self._start

    @property
    def observations(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._observations)

    def run(self) -> RunResult:
        while True:
            self._check_time()
            self._check_step_budget()
            action = self._next_action()
            if action is None:
                # A format error was tolerated: a repair prompt was already
                # appended to self.messages. Loop again without executing
                # anything or consuming a call_index.
                continue
            observation = self._execute(action)
            self._observations.append(observation)
            if action["kind"] == "answer":
                return self._finish("answered", answer=dict(action["args"]))
            self._append_observation(observation)

    def _check_time(self) -> None:
        elapsed = self.elapsed()
        if elapsed > self.limits.wall_time_limit:
            raise TimeExceeded(
                f"exchange wall-clock deadline exceeded: {elapsed:.3f}s > {self.limits.wall_time_limit}s",
                elapsed=elapsed,
                cap=self.limits.wall_time_limit,
                steps=self._steps,
                calls=self._calls,
            )

    def _check_step_budget(self) -> None:
        if self._steps >= self.limits.step_limit:
            raise LimitsExceeded(
                f"step budget exhausted: {self._steps} model iterations >= cap "
                f"{self.limits.step_limit} with no ANSWER submitted",
                limit="step",
                value=self._steps,
                cap=self.limits.step_limit,
            )

    def _next_action(self) -> dict[str, Any] | None:
        response = self.model.query(self.messages)
        content = response.get("content") or ""
        # M0 rule (FINAL-PLAN.md section 10): "Strip reasoning_content
        # before echoing an assistant message into history." broker/live.py
        # owns that rule at the source; this loop enforces it again here,
        # defensively, by construction — only role+content are ever stored,
        # so a broker that forgot cannot leak reasoning_content into the
        # transcript this loop builds.
        self.messages.append({"role": "assistant", "content": content})
        self._steps += 1
        try:
            body = extract_action_fence(content)
            action = canonicalise_action(body, call_index=self._calls)
        except ActionParseError as exc:
            self._consecutive_format_errors += 1
            if self._consecutive_format_errors > self.limits.max_consecutive_format_errors:
                raise FormatError(
                    f"{self._consecutive_format_errors} consecutive malformed turns "
                    f"(cap {self.limits.max_consecutive_format_errors}): {exc}",
                    consecutive=self._consecutive_format_errors,
                    cap=self.limits.max_consecutive_format_errors,
                    steps=self._steps,
                ) from exc
            self.messages.append({"role": "user", "content": _repair_prompt(exc)})
            return None
        self._consecutive_format_errors = 0
        if action["kind"] != "answer":
            self._calls += 1
        return action

    def _execute(self, action: dict[str, Any]) -> dict[str, Any]:
        observation = self.env.execute(action)
        self._track_cost(observation)
        return observation

    def _track_cost(self, observation: Mapping[str, Any]) -> None:
        """`cost_limit` is a safety net, not the authority (see
        kit/loop/limits.py's docstring): the arena's trusted envelope
        meters credits for real. When an observation reports
        `credits_left` (CONTRACTS.md 5.2's `tool_call` event field), that
        figure is trusted outright; only when it is absent does this loop
        fall back to summing observed `cost`."""
        credits_left = observation.get("credits_left")
        if isinstance(credits_left, int) and not isinstance(credits_left, bool):
            self._credits_left = credits_left
            # Deliberately `< 0`, not `<= 0`: an ANSWER submitted at exactly
            # 0 credits remaining must still go through — this only cuts
            # the agent off once the pool has actually gone negative, never
            # for spending its last credit on the call that ends the
            # exchange.
            if credits_left < 0:
                raise LimitsExceeded(
                    f"duel credit pool exhausted: {credits_left} credits remaining",
                    limit="cost",
                    value=self.limits.cost_limit - credits_left,
                    cap=self.limits.cost_limit,
                )
            return
        cost = observation.get("cost")
        if isinstance(cost, int) and not isinstance(cost, bool) and cost > 0:
            self._cost_spent += cost
            if self._cost_spent > self.limits.cost_limit:
                raise LimitsExceeded(
                    f"duel credit pool exhausted: spent {self._cost_spent} > cap {self.limits.cost_limit}",
                    limit="cost",
                    value=self._cost_spent,
                    cap=self.limits.cost_limit,
                )

    def _append_observation(self, observation: dict[str, Any]) -> None:
        self.messages.append({"role": "user", "content": render_observation(observation)})

    def _finish(self, exit_status: str, *, answer: dict[str, Any] | None = None) -> RunResult:
        return RunResult(
            exit_status=exit_status,
            steps=self._steps,
            calls=self._calls,
            elapsed=self.elapsed(),
            answer=answer,
            messages=tuple(self.messages),
            observations=tuple(self._observations),
        )


if __name__ == "__main__":
    print("=== kit.loop.agent: canonicaliser demo, one call per VERB ===")
    examples = [
        'MCP slides.get_frame anchor=Frame:3f2a9c11/w/041 fields=Title,body,body lease=lse_7f21',
        'A2A curriculum-analyst.which_days_cover concept=Concept:streamable-http fields=anchor,course_day,track',
        'DISCOVER registry.list_servers fields=name',
        'ANSWER {"text": "day 26, track P2T2", "cited_anchors": ["Frame:3f2a9c11/w/041"]}',
    ]
    for line in examples:
        action = canonicalise_action(line, call_index=0)
        print(f"  {line!r}\n    -> {action}")
    assert canonicalise_action(examples[0], call_index=0)["kind"] == "mcp"
    assert canonicalise_action(examples[1], call_index=0)["kind"] == "a2a"
    assert canonicalise_action(examples[2], call_index=0)["kind"] == "discover"
    assert canonicalise_action(examples[3], call_index=5)["call_index"] is None

    print("\n=== fields=Title,body,body canonicalises to ('body', 'title') ===")
    got = canonicalise_action(examples[0], call_index=0)["fields"]
    print(f"  {got!r}")
    assert got == ("body", "title")

    print("\n=== an unrecognised server passes through UNCONTESTED (shadow_server) ===")
    shadow = canonicalise_action("MCP totally-unknown-server.mystery_tool x=1", call_index=2)
    print(f"  {shadow}")
    assert shadow["server"] == "totally-unknown-server"
    assert shadow["kind"] == "mcp"  # default bucket, not rejected

    print("\n=== extract_action_fence: exactly one fence required ===")
    wrapped = f"some reasoning here\n```action\n{examples[0]}\n```\nmore reasoning"
    body = extract_action_fence(wrapped)
    print(f"  extracted: {body!r}")
    assert body == examples[0]
    try:
        extract_action_fence("no fence at all")
    except ActionParseError as exc:
        print(f"  no fence -> ActionParseError: {exc}")
    else:
        raise AssertionError("expected ActionParseError")

    print("\n=== a tiny end-to-end run with scripted Model/Environment ===")

    class _ScriptedModel:
        def __init__(self, turns: list[str]) -> None:
            self._turns = list(turns)
            self._i = 0

        def query(self, messages: list[dict], **kw: Any) -> dict:
            content = self._turns[self._i]
            self._i += 1
            return {"role": "assistant", "content": content, "reasoning_content": "scratch (must be stripped)"}

    class _EchoEnvironment:
        """A minimal stand-in Environment: not the real trusted envelope,
        just enough to exercise the loop's own contract (this demo owns no
        gateway, no enforcement — that is deliberately out of scope here)."""

        def __init__(self) -> None:
            self.seen: list[dict] = []

        def execute(self, action: dict) -> dict:
            self.seen.append(action)
            if action["kind"] == "answer":
                return {"ok": True, "recorded": "answer"}
            return {"ok": True, "cost": 4, "credits_left": 96, "anchors": [], "rows": []}

    model = _ScriptedModel(
        [
            "```action\nDISCOVER registry.list_servers fields=name\n```",
            'not a fenced action this time, just prose',
            '```action\nANSWER {"text": "day 26, track P2T2", "cited_anchors": ["Frame:3f2a9c11/w/041"]}\n```',
        ]
    )
    env = _EchoEnvironment()
    agent = Agent(
        model,
        env,
        system_prompt="(demo system prompt)",
        task="Which day covers Concept:streamable-http?",
    )
    result = agent.run()
    print(f"  exit_status={result.exit_status!r} steps={result.steps} calls={result.calls}")
    print(f"  answer={result.answer}")
    print(f"  env.seen call_index sequence: {[a['call_index'] for a in env.seen]}")
    assert result.exit_status == "answered"
    assert result.steps == 3  # discover, one malformed repair turn, answer
    assert result.calls == 1  # only the discover call was ever a real Command
    assert env.seen[0]["call_index"] == 0
    assert env.seen[-1]["kind"] == "answer"
    assert all(m["content"] != "" or m["role"] != "assistant" for m in agent.messages)
    assert all(set(m) == {"role", "content"} for m in agent.messages if m["role"] == "assistant")
    print("  reasoning_content confirmed stripped from every assistant history entry")

    print("\nAll agent.py demos passed.")
