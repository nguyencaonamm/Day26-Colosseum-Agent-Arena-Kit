"""tests/test_loop.py — tests for kit/loop/{agent,limits,prompt}.py.

Covers, in order:
  1. The canonicaliser: verb -> kind derivation, field-mask canonicalisation,
     header/lease extraction, verbatim args, the shadow_server invariant
     (an unknown server passes through uncontested, never rejected).
  2. extract_action_fence: exactly one ```action fence required.
  3. ANSWER-specific validation (JSON object, non-empty text, cited_anchors
     shape) and its `call_index` always being None.
  4. Agent.run() end to end: the happy path, call_index numbering with a
     format error in between (no gap), assistant-history hygiene
     (reasoning_content stripped, only role+content kept).
  5. The three termination exceptions, each exactly as this task's brief
     names them: LimitsExceeded (step and cost), TimeExceeded (via an
     injected fake clock — no real sleep), FormatError (two consecutive
     malformed turns).
  6. prompt.py: every EXAMPLE_ACTIONS line actually parses, and its leading
     verb matches the kind the canonicaliser derives (prompt/parser drift
     guard).

pytest only (permitted in tests/ per the workspace's hard rules). No
network, no unseeded randomness, no real wall-clock sleep.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# Make the repo root importable when pytest is invoked from anywhere.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from kit.loop.agent import (
    Agent,
    ActionParseError,
    canonicalise_action,
    extract_action_fence,
    render_observation,
)
from kit.loop.limits import FormatError, Limits, LimitsExceeded, LoopTerminated, TimeExceeded
from kit.loop.prompt import EXAMPLE_ACTIONS


# ---------------------------------------------------------------------------
# Test doubles: a scripted Model and a scripted Environment, both satisfying
# the loop's duck-typed protocols without importing anything gateway-shaped
# (this file's job is the loop's own contract, not the trusted envelope's).
# ---------------------------------------------------------------------------


class _ScriptedModel:
    """Returns each entry of `turns` in order, one per `.query()` call.
    Every returned message carries a `reasoning_content` key so tests can
    assert the loop strips it before storing history."""

    def __init__(self, turns: list[str]) -> None:
        self._turns = list(turns)
        self.calls = 0

    def query(self, messages: list[dict], **kw: Any) -> dict:
        content = self._turns[self.calls]
        self.calls += 1
        return {"role": "assistant", "content": content, "reasoning_content": "scratch " * 5}


class _ScriptedEnvironment:
    """Returns each entry of `observations` in order, one per `.execute()`
    call, and records every action it was handed for later inspection."""

    def __init__(self, observations: list[dict]) -> None:
        self._observations = list(observations)
        self.seen: list[dict] = []

    def execute(self, action: dict) -> dict:
        self.seen.append(action)
        return self._observations[len(self.seen) - 1]


def _fence(body: str) -> str:
    return f"```action\n{body}\n```"


def _default_observation(**overrides: Any) -> dict:
    base = {"ok": True, "cost": 4, "credits_left": 96, "anchors": [], "rows": []}
    base.update(overrides)
    return base


def _answer_observation() -> dict:
    return {"ok": True, "recorded": "answer"}


class _FakeClock:
    """A ticking fake clock: each `.__call__()` advances by `step` and
    returns the new value. Deterministic, no real time ever passes."""

    def __init__(self, start: float = 0.0, step: float = 0.1) -> None:
        self._t = start
        self._step = step

    def __call__(self) -> float:
        self._t += self._step
        return self._t


# ---------------------------------------------------------------------------
# 1. The canonicaliser: kind derivation, field masks, headers, lease, args.
# ---------------------------------------------------------------------------


def test_mcp_verb_ordinary_server_derives_mcp_kind() -> None:
    action = canonicalise_action("MCP slides.get_frame anchor=Frame:3f2a9c11/w/041", call_index=0)
    assert action["kind"] == "mcp"
    assert action["server"] == "slides"
    assert action["tool"] == "get_frame"
    assert action["call_index"] == 0


def test_a2a_verb_known_peer_derives_a2a_kind() -> None:
    action = canonicalise_action("A2A roster.lookup_learner act=learner:sv-0417", call_index=3)
    assert action["kind"] == "a2a"
    assert action["server"] == "roster"
    assert action["call_index"] == 3


def test_discovery_tool_derives_discover_kind_regardless_of_stated_verb() -> None:
    # kind is derived from (server, tool) identity, never trusted from the
    # model's own verb — even a model that mislabels a discovery call MCP
    # gets the correct kind back.
    action = canonicalise_action("MCP registry.list_servers fields=name", call_index=0)
    assert action["kind"] == "discover"


def test_unknown_server_passes_through_uncontested() -> None:
    """CRITICAL INVARIANT (agent.py's module docstring): shadow_server
    depends on an unrecognised server surviving the loop untouched. The
    canonicaliser must NEVER reject or special-case an unknown identity —
    it falls into the default "mcp" bucket and is handed to env.execute()
    exactly like any other call."""
    action = canonicalise_action("MCP totally-unknown-server.mystery_tool x=1", call_index=2)
    assert action["server"] == "totally-unknown-server"
    assert action["tool"] == "mystery_tool"
    assert action["kind"] == "mcp"
    assert action["args"] == {"x": "1"}
    assert action["call_index"] == 2


def test_fields_are_sorted_deduped_lowercased() -> None:
    action = canonicalise_action("MCP slides.get_frame fields=Title,body,BODY,body", call_index=0)
    assert action["fields"] == ("body", "title")


def test_omitted_fields_default_to_empty_tuple() -> None:
    action = canonicalise_action("MCP registry.provenance anchor=Frame:x/w/001", call_index=0)
    assert action["fields"] == ()


def test_lease_key_becomes_lease_id() -> None:
    action = canonicalise_action("MCP slides.get_frame lease=lse_7f21", call_index=0)
    assert action["lease_id"] == "lse_7f21"
    assert "lease" not in action["args"]


def test_header_prefixed_keys_become_headers_lowercased() -> None:
    action = canonicalise_action(
        'MCP progress.record_mastery header.If-Match=sha256:deadbeef header.Idempotency-Key=abc123',
        call_index=0,
    )
    assert action["headers"] == {"if-match": "sha256:deadbeef", "idempotency-key": "abc123"}
    assert action["args"] == {}


def test_ordinary_args_are_kept_verbatim_not_lowercased() -> None:
    action = canonicalise_action(
        'A2A curriculum-analyst.which_days_cover Concept=Concept:streamable-http', call_index=0
    )
    # The arg KEY is preserved exactly as written (only fields/header keys
    # are canonicalised per CONTRACTS.md 4.1) — "Concept" stays "Concept".
    assert action["args"] == {"Concept": "Concept:streamable-http"}


def test_quoted_values_with_spaces_are_tokenised_correctly() -> None:
    action = canonicalise_action('MCP slides.query q="streamable http replaces http+sse"', call_index=0)
    assert action["args"]["q"] == "streamable http replaces http+sse"


def test_server_tool_token_is_lowercased() -> None:
    action = canonicalise_action("MCP Slides.Get_Frame anchor=x", call_index=0)
    assert action["server"] == "slides"
    assert action["tool"] == "get_frame"


@pytest.mark.parametrize(
    "line",
    [
        "MCP",  # no server.tool
        "MCP slides",  # missing .tool
        "MCP slides.query bareword",  # no '='
        "MCP slides.query x=1 x=2",  # duplicate key
        "BOGUS slides.query x=1",  # not a real verb
        "",
    ],
)
def test_malformed_call_actions_raise_action_parse_error(line: str) -> None:
    with pytest.raises(ActionParseError):
        canonicalise_action(line, call_index=0)


# ---------------------------------------------------------------------------
# 2. extract_action_fence: exactly one fence required.
# ---------------------------------------------------------------------------


def test_extract_action_fence_happy_path() -> None:
    content = "some reasoning\n```action\nMCP slides.query q=hi\n```\nmore reasoning"
    assert extract_action_fence(content) == "MCP slides.query q=hi"


def test_extract_action_fence_no_fence_raises() -> None:
    with pytest.raises(ActionParseError):
        extract_action_fence("just prose, no fence, but it says MCP right here")


def test_extract_action_fence_two_fences_raises() -> None:
    content = "```action\nMCP a.b\n```\n```action\nMCP c.d\n```"
    with pytest.raises(ActionParseError):
        extract_action_fence(content)


def test_extract_action_fence_none_content_raises() -> None:
    with pytest.raises(ActionParseError):
        extract_action_fence(None)


# ---------------------------------------------------------------------------
# 3. ANSWER-specific validation.
# ---------------------------------------------------------------------------


def test_answer_happy_path_has_none_call_index() -> None:
    action = canonicalise_action(
        'ANSWER {"text": "day 26", "cited_anchors": ["Frame:x/w/001"]}', call_index=7
    )
    assert action["kind"] == "answer"
    assert action["call_index"] is None
    assert action["args"]["text"] == "day 26"


def test_answer_without_text_raises() -> None:
    with pytest.raises(ActionParseError):
        canonicalise_action('ANSWER {"cited_anchors": []}', call_index=0)


def test_answer_with_non_json_payload_raises() -> None:
    with pytest.raises(ActionParseError):
        canonicalise_action("ANSWER not json at all", call_index=0)


def test_answer_with_non_object_json_raises() -> None:
    with pytest.raises(ActionParseError):
        canonicalise_action('ANSWER ["day 26"]', call_index=0)


def test_answer_with_bad_cited_anchors_shape_raises() -> None:
    with pytest.raises(ActionParseError):
        canonicalise_action('ANSWER {"text": "hi", "cited_anchors": "not-a-list"}', call_index=0)


def test_answer_extra_fields_pass_through_verbatim() -> None:
    action = canonicalise_action(
        'ANSWER {"text": "d26", "course_day": 26, "track": "P2T2"}', call_index=0
    )
    assert action["args"]["course_day"] == 26
    assert action["args"]["track"] == "P2T2"


# ---------------------------------------------------------------------------
# 4. Agent.run() end to end.
# ---------------------------------------------------------------------------


def test_happy_path_two_calls_then_answer() -> None:
    model = _ScriptedModel(
        [
            _fence("DISCOVER registry.list_servers fields=name"),
            _fence("MCP slides.get_frame anchor=Frame:x/w/001 fields=title,body lease=lse_1"),
            _fence('ANSWER {"text": "day 26, track P2T2", "cited_anchors": ["Frame:x/w/001"]}'),
        ]
    )
    env = _ScriptedEnvironment([_default_observation(), _default_observation(), _answer_observation()])
    agent = Agent(model, env, system_prompt="sys", task="which day covers X?")
    result = agent.run()

    assert result.exit_status == "answered"
    assert result.steps == 3
    assert result.calls == 2
    assert result.answer == {"text": "day 26, track P2T2", "cited_anchors": ["Frame:x/w/001"]}
    assert [a["call_index"] for a in env.seen] == [0, 1, None]
    assert env.seen[-1]["kind"] == "answer"


def test_call_index_has_no_gap_across_a_tolerated_format_error() -> None:
    model = _ScriptedModel(
        [
            _fence("MCP slides.query q=hi fields=title"),
            "this turn has no fence in it at all",  # tolerated format error
            _fence("MCP slides.get_frame anchor=Frame:x/w/001"),
            _fence('ANSWER {"text": "done"}'),
        ]
    )
    env = _ScriptedEnvironment(
        [_default_observation(), _default_observation(), _answer_observation()]
    )
    agent = Agent(model, env, system_prompt="sys", task="task")
    result = agent.run()

    assert result.exit_status == "answered"
    assert result.calls == 2
    # Two real commands were issued; despite the format error in between,
    # their call_index values are 0 and 1 — no gap.
    real_calls = [a for a in env.seen if a["kind"] != "answer"]
    assert [a["call_index"] for a in real_calls] == [0, 1]
    # 4 model turns total (1 real, 1 malformed, 1 real, 1 answer).
    assert result.steps == 4


def test_assistant_history_strips_reasoning_content_and_keeps_only_role_and_content() -> None:
    model = _ScriptedModel([_fence('ANSWER {"text": "done"}')])
    env = _ScriptedEnvironment([_answer_observation()])
    agent = Agent(model, env, system_prompt="sys", task="task")
    agent.run()

    assistant_messages = [m for m in agent.messages if m["role"] == "assistant"]
    assert assistant_messages, "expected at least one assistant message"
    for m in assistant_messages:
        assert set(m.keys()) == {"role", "content"}
        assert "reasoning_content" not in m


def test_observations_are_recorded_and_rendered_into_user_messages() -> None:
    model = _ScriptedModel(
        [_fence("MCP registry.provenance anchor=Frame:x/w/001 fields=etag"), _fence('ANSWER {"text": "d"}')]
    )
    obs = _default_observation(cost=1, credits_left=99)
    env = _ScriptedEnvironment([obs, _answer_observation()])
    agent = Agent(model, env, system_prompt="sys", task="task")
    result = agent.run()

    assert result.observations == (obs, _answer_observation())
    rendered = render_observation(obs)
    assert any(m["role"] == "user" and m["content"] == rendered for m in agent.messages)


# ---------------------------------------------------------------------------
# 5. The three termination exceptions.
# ---------------------------------------------------------------------------


def test_step_limit_raises_limits_exceeded_with_limit_step() -> None:
    # A model that never answers: every turn is another tool call.
    turns = [_fence(f"MCP slides.query q=turn{i} fields=title") for i in range(10)]
    observations = [_default_observation() for _ in range(10)]
    model = _ScriptedModel(turns)
    env = _ScriptedEnvironment(observations)
    agent = Agent(model, env, system_prompt="sys", task="task", limits=Limits(step_limit=2))

    with pytest.raises(LimitsExceeded) as excinfo:
        agent.run()
    exc = excinfo.value
    assert exc.exit_status == "limits_exceeded"
    assert exc.detail["limit"] == "step"
    assert exc.detail["cap"] == 2
    assert isinstance(exc, LoopTerminated)
    # The model was queried exactly step_limit times, never a 3rd.
    assert model.calls == 2


def test_step_limit_is_not_hit_when_answer_lands_on_the_final_turn() -> None:
    model = _ScriptedModel(
        [
            _fence("MCP slides.query q=a fields=title"),
            _fence('ANSWER {"text": "done exactly at the cap"}'),
        ]
    )
    env = _ScriptedEnvironment([_default_observation(), _answer_observation()])
    agent = Agent(model, env, system_prompt="sys", task="task", limits=Limits(step_limit=2))
    result = agent.run()
    assert result.exit_status == "answered"
    assert result.steps == 2


def test_cost_overrun_via_cost_field_raises_limits_exceeded_with_limit_cost() -> None:
    model = _ScriptedModel([_fence(f"MCP slides.query q=t{i} fields=title") for i in range(5)])
    # No credits_left reported -> the loop falls back to summing `cost`.
    observations = [{"ok": True, "cost": 40} for _ in range(5)]
    env = _ScriptedEnvironment(observations)
    agent = Agent(model, env, system_prompt="sys", task="task", limits=Limits(step_limit=10, cost_limit=100))

    with pytest.raises(LimitsExceeded) as excinfo:
        agent.run()
    exc = excinfo.value
    assert exc.detail["limit"] == "cost"
    assert exc.detail["cap"] == 100
    # 40, 80 (ok) then 120 > 100 on the third call.
    assert exc.detail["value"] == 120


def test_credits_left_zero_does_not_block_the_closing_answer() -> None:
    """Deliberate `< 0`, not `<= 0` (agent.py's _track_cost docstring): an
    ANSWER submitted at exactly 0 credits remaining must still go through."""
    model = _ScriptedModel(
        [
            _fence("MCP slides.query q=a fields=title"),
            _fence('ANSWER {"text": "spent my last credit on that last call"}'),
        ]
    )
    env = _ScriptedEnvironment(
        [_default_observation(cost=100, credits_left=0), _answer_observation()]
    )
    agent = Agent(model, env, system_prompt="sys", task="task")
    result = agent.run()
    assert result.exit_status == "answered"


def test_credits_left_negative_raises_limits_exceeded_with_limit_cost() -> None:
    model = _ScriptedModel([_fence("MCP slides.query q=a fields=title")])
    env = _ScriptedEnvironment([_default_observation(cost=101, credits_left=-1)])
    agent = Agent(model, env, system_prompt="sys", task="task")

    with pytest.raises(LimitsExceeded) as excinfo:
        agent.run()
    assert excinfo.value.detail["limit"] == "cost"


def test_wall_time_limit_raises_time_exceeded_via_fake_clock() -> None:
    # Each Agent method call to the clock advances 6s; wall_time_limit=20s
    # is exceeded on the 4th internal read, well before any real model call
    # count matters. No real sleep anywhere in this test.
    model = _ScriptedModel([_fence("MCP slides.query q=a fields=title")] * 10)
    env = _ScriptedEnvironment([_default_observation()] * 10)
    clock = _FakeClock(start=0.0, step=6.0)
    agent = Agent(
        model, env, system_prompt="sys", task="task", limits=Limits(wall_time_limit=20.0), clock=clock
    )

    with pytest.raises(TimeExceeded) as excinfo:
        agent.run()
    exc = excinfo.value
    assert exc.exit_status == "time_exceeded"
    assert exc.detail["cap"] == 20.0
    assert exc.detail["elapsed"] > 20.0


def test_two_consecutive_format_errors_raise_format_error() -> None:
    model = _ScriptedModel(["no fence here", "still no fence here either"])
    env = _ScriptedEnvironment([])
    agent = Agent(model, env, system_prompt="sys", task="task", limits=Limits(max_consecutive_format_errors=1))

    with pytest.raises(FormatError) as excinfo:
        agent.run()
    exc = excinfo.value
    assert exc.exit_status == "format_error"
    assert exc.detail["consecutive"] == 2
    assert exc.detail["cap"] == 1
    # env.execute was never called: both turns failed to parse.
    assert env.seen == []


def test_one_format_error_then_recovery_does_not_raise() -> None:
    model = _ScriptedModel(["no fence here", _fence('ANSWER {"text": "recovered"}')])
    env = _ScriptedEnvironment([_answer_observation()])
    agent = Agent(model, env, system_prompt="sys", task="task", limits=Limits(max_consecutive_format_errors=1))
    result = agent.run()
    assert result.exit_status == "answered"
    # A repair prompt was appended after the malformed turn.
    assert any("repair" in m["content"].lower() for m in agent.messages if m["role"] == "user")


def test_format_error_counter_resets_after_a_valid_action() -> None:
    # error, valid, error, valid -> never two in a row -> no FormatError.
    model = _ScriptedModel(
        [
            "bad turn 1",
            _fence("MCP slides.query q=a fields=title"),
            "bad turn 2",
            _fence('ANSWER {"text": "made it"}'),
        ]
    )
    env = _ScriptedEnvironment([_default_observation(), _answer_observation()])
    agent = Agent(model, env, system_prompt="sys", task="task", limits=Limits(max_consecutive_format_errors=1))
    result = agent.run()
    assert result.exit_status == "answered"


def test_content_none_is_treated_as_a_format_error_not_a_crash() -> None:
    class _NoneContentModel:
        def query(self, messages: list[dict], **kw: Any) -> dict:
            return {"role": "assistant", "content": None}

    env = _ScriptedEnvironment([])
    agent = Agent(_NoneContentModel(), env, system_prompt="sys", task="task", limits=Limits(max_consecutive_format_errors=0))
    with pytest.raises(FormatError):
        agent.run()


# ---------------------------------------------------------------------------
# 6. prompt.py <-> agent.py drift guard.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line", EXAMPLE_ACTIONS)
def test_every_prompt_example_action_parses(line: str) -> None:
    body = extract_action_fence(_fence(line))
    action = canonicalise_action(body, call_index=0)
    stated_verb = line.split(None, 1)[0].upper()
    if stated_verb == "ANSWER":
        assert action["kind"] == "answer"
    else:
        # The verb the prompt shows for a non-ANSWER example must match
        # what the canonicaliser actually derives from (server, tool) —
        # otherwise the prompt is teaching a lie about its own grammar.
        assert action["kind"] == stated_verb.lower()


def test_example_actions_cover_all_four_verbs() -> None:
    verbs = {line.split(None, 1)[0].upper() for line in EXAMPLE_ACTIONS}
    assert verbs == {"MCP", "A2A", "DISCOVER", "ANSWER"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
