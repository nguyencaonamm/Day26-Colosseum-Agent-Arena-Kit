"""tests/test_mcp_errors.py — the closed error taxonomy (kit/mcp/errors.py)
and the shared request/result types (kit/mcp/types.py), CONTRACTS.md
sections 3.1-3.3 and 4.1.

pytest only (permitted in tests/ per the workspace's hard rules). No
network, no unseeded randomness.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make the repo root importable when pytest is invoked from anywhere.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from kit.mcp.errors import ERROR_SPECS, ErrorCode, ErrorSpec, make_error
from kit.mcp.types import ToolCall, ToolResult, canonicalise_fields

# ---------------------------------------------------------------------------
# The nine-code closed taxonomy (CONTRACTS.md 3.3)
# ---------------------------------------------------------------------------

_ALL_CODE_STRINGS = {
    "lease_required",
    "lease_expired",
    "conflict",
    "precondition_missing",
    "rate_limited",
    "unauthorized",
    "unavailable",
    "bad_request",
    "not_found",
}


def test_error_code_has_exactly_nine_members() -> None:
    assert len(ErrorCode) == 9
    assert {c.value for c in ErrorCode} == _ALL_CODE_STRINGS


def test_error_specs_cover_every_code_exactly_once() -> None:
    assert set(ERROR_SPECS) == set(ErrorCode)
    for code, spec in ERROR_SPECS.items():
        assert isinstance(spec, ErrorSpec)
        assert spec.code is code


@pytest.mark.parametrize("code", list(ErrorCode))
def test_every_code_is_charged(code: ErrorCode) -> None:
    # CONTRACTS.md 3.3: "Cost is still charged except where noted" — and
    # the table's "Charged?" column is "yes" (or "yes, no refund") for all
    # nine rows; there is no code in this table that is ever free.
    assert ERROR_SPECS[code].charged is True


def test_only_rate_limited_is_marked_no_refund() -> None:
    for code, spec in ERROR_SPECS.items():
        expected = code is ErrorCode.RATE_LIMITED
        assert spec.no_refund is expected, f"{code}: no_refund should be {expected}"


def test_retry_safe_false_only_for_never_rows() -> None:
    never_codes = {ErrorCode.UNAUTHORIZED, ErrorCode.NOT_FOUND}
    for code, spec in ERROR_SPECS.items():
        if code in never_codes:
            assert spec.retry_safe is False
            assert spec.retry_note == "never"
        else:
            assert spec.retry_safe is True
            assert spec.retry_note != "never"


def test_only_unavailable_is_opaque() -> None:
    for code, spec in ERROR_SPECS.items():
        assert spec.opaque is (code is ErrorCode.UNAVAILABLE)


# ---------------------------------------------------------------------------
# make_error() — the general contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", list(ErrorCode))
def test_make_error_bare_code_shape(code: ErrorCode) -> None:
    err = make_error(code)
    assert err == {"code": code.value}


def test_make_error_accepts_plain_string_code() -> None:
    assert make_error("not_found") == {"code": "not_found"}
    assert make_error("not_found") == make_error(ErrorCode.NOT_FOUND)


def test_make_error_unknown_code_raises() -> None:
    with pytest.raises(ValueError):
        make_error("teapot")
    with pytest.raises(ValueError):
        make_error("")


def test_make_error_json_serialisable() -> None:
    for code in ErrorCode:
        err = make_error(code)
        # round-trips through json with no surprises (all values are str)
        assert json.loads(json.dumps(err, sort_keys=True)) == err


# ---------------------------------------------------------------------------
# THE central invariant: `unavailable` carries no reason/detail/message —
# ever — and the opposite for `bad_request`.
# ---------------------------------------------------------------------------


def test_unavailable_error_has_exactly_the_code_key() -> None:
    err = make_error(ErrorCode.UNAVAILABLE)
    assert set(err.keys()) == {"code"}
    assert err == {"code": "unavailable"}


def test_bad_request_error_can_carry_extra_keys() -> None:
    err = make_error(ErrorCode.BAD_REQUEST, reason="unknown field 'x' in mask", field="x")
    assert set(err.keys()) != {"code"}
    assert set(err.keys()) == {"code", "reason", "field"}


@pytest.mark.parametrize(
    "extra_kwargs",
    [
        {"reason": "the database connection pool is exhausted"},
        {"detail": "timeout after 3.0s"},
        {"message": "please try again"},
        {"reason": "x", "detail": "y", "message": "z"},
    ],
)
def test_make_error_refuses_to_soften_unavailable(extra_kwargs: dict) -> None:
    """This is the trap a "helpful" future edit would fall into: attaching
    a debugging breadcrumb to `unavailable`. make_error() must refuse it,
    not just document that it shouldn't happen."""
    with pytest.raises(ValueError):
        make_error(ErrorCode.UNAVAILABLE, **extra_kwargs)
    with pytest.raises(ValueError):
        make_error("unavailable", **extra_kwargs)


def test_make_error_code_is_not_a_valid_extra_key() -> None:
    with pytest.raises(ValueError):
        make_error("not_found", **{"code": "override"})


# ---------------------------------------------------------------------------
# canonicalise_fields() — CONTRACTS.md 4.1
# ---------------------------------------------------------------------------


def test_canonicalise_fields_sorts_dedupes_lowercases() -> None:
    assert canonicalise_fields(("Title", "body", "BODY")) == ("body", "title")
    assert canonicalise_fields(["Body", "Title", "body"]) == ("body", "title")


def test_canonicalise_fields_sentinels_round_trip() -> None:
    assert canonicalise_fields(()) == ()
    assert canonicalise_fields(["*"]) == ("*",)
    assert canonicalise_fields(("*",)) == ("*",)


def test_canonicalise_fields_rejects_bare_string() -> None:
    # A bare "title" is iterable-of-chars, not a one-element field mask —
    # this is exactly the kind of silent bug ("body" -> ('b','o','d','y'))
    # a stdlib-only helper needs to refuse loudly.
    with pytest.raises(TypeError):
        canonicalise_fields("title")


def test_canonicalise_fields_is_idempotent() -> None:
    once = canonicalise_fields(("Zeta", "alpha", "Alpha"))
    twice = canonicalise_fields(once)
    assert once == twice == ("alpha", "zeta")


# ---------------------------------------------------------------------------
# ToolCall — CONTRACTS.md 3.1
# ---------------------------------------------------------------------------


def _make_call(**overrides: object) -> ToolCall:
    kwargs = dict(
        server="slides",
        tool="query",
        args={"q": "streamable http"},
        fields=(),
        headers={},
        lease_id=None,
        call_index=0,
    )
    kwargs.update(overrides)
    return ToolCall(**kwargs)  # type: ignore[arg-type]


def test_toolcall_defaults_match_contracts_3_1() -> None:
    call = ToolCall(server="slides", tool="query", args={"q": "x"})
    assert call.fields == ()
    assert call.headers == {}
    assert call.lease_id is None
    assert call.call_index == 0


def test_toolcall_auto_canonicalises_fields_on_construction() -> None:
    call = _make_call(fields=("Title", "body", "body"))
    assert call.fields == ("body", "title")


def test_toolcall_preserves_wildcard_field_mask() -> None:
    call = _make_call(fields=("*",))
    assert call.fields == ("*",)


def test_toolcall_to_dict_from_dict_round_trip() -> None:
    call = _make_call(
        server="slides",
        tool="get_frame",
        args={"anchor": "Frame:3f2a9c11/w/041"},
        fields=("Title", "body"),
        headers={"mcp-replica": "w", "if-match": "sha256:deadbeef"},
        lease_id="lse_7f21",
        call_index=3,
    )
    dumped = json.dumps(call.to_dict(), sort_keys=True)
    restored = ToolCall.from_dict(json.loads(dumped))
    assert restored == call
    assert restored.fields == ("body", "title")


@pytest.mark.parametrize(
    "overrides",
    [
        {"server": ""},
        {"tool": ""},
        {"args": "not-a-dict"},
        {"headers": "not-a-dict"},
        {"lease_id": 12345},
        {"call_index": -1},
        {"call_index": True},  # bool is an int subclass; must still be rejected
    ],
)
def test_toolcall_rejects_malformed_construction(overrides: dict) -> None:
    with pytest.raises(ValueError):
        _make_call(**overrides)


# ---------------------------------------------------------------------------
# ToolResult — CONTRACTS.md 3.2 (success) and 3.3 (error)
# ---------------------------------------------------------------------------


def test_toolresult_success_envelope_has_all_3_2_keys() -> None:
    result = ToolResult(
        ok=True,
        rows=({"anchor": "Frame:3f2a9c11/w/041", "title": "Streamable HTTP"},),
        anchors=("Frame:3f2a9c11/w/041",),
        cost=6,
        replica="w",
        ttl=30,
    )
    d = result.to_dict()
    assert set(d.keys()) == {
        "ok", "rows", "anchors", "cost", "partial", "continuation",
        "lease_id", "etag", "replica", "ttl", "deprecated", "successor",
    }
    assert d["ok"] is True
    assert d["cost"] == 6


def test_toolresult_success_round_trips_through_json() -> None:
    result = ToolResult(ok=True, rows=({"a": 1},), anchors=("Concept:x",), cost=3)
    restored = ToolResult.from_dict(json.loads(json.dumps(result.to_dict(), sort_keys=True)))
    assert restored == result


def test_toolresult_error_envelope_is_exactly_ok_error_cost() -> None:
    result = ToolResult(ok=False, error=make_error(ErrorCode.RATE_LIMITED), cost=4)
    d = result.to_dict()
    assert set(d.keys()) == {"ok", "error", "cost"}
    assert d == {"ok": False, "error": {"code": "rate_limited"}, "cost": 4}


def test_toolresult_unavailable_error_envelope_matches_make_error_invariant() -> None:
    result = ToolResult(ok=False, error=make_error(ErrorCode.UNAVAILABLE), cost=6)
    d = result.to_dict()
    assert d == {"ok": False, "error": {"code": "unavailable"}, "cost": 6}
    assert set(d["error"].keys()) == {"code"}


def test_toolresult_error_round_trips_through_json() -> None:
    result = ToolResult(ok=False, error=make_error(ErrorCode.NOT_FOUND), cost=2)
    restored = ToolResult.from_dict(json.loads(json.dumps(result.to_dict(), sort_keys=True)))
    assert restored == result


def test_toolresult_ok_true_forbids_error() -> None:
    with pytest.raises(ValueError):
        ToolResult(ok=True, error=make_error(ErrorCode.NOT_FOUND))


def test_toolresult_ok_false_requires_error() -> None:
    with pytest.raises(ValueError):
        ToolResult(ok=False)


def test_toolresult_rejects_unknown_error_code() -> None:
    with pytest.raises(ValueError):
        ToolResult(ok=False, error={"code": "teapot"})


def test_toolresult_rejects_softened_unavailable() -> None:
    with pytest.raises(ValueError):
        ToolResult(ok=False, error={"code": "unavailable", "reason": "db down"})


@pytest.mark.parametrize(
    "success_only_override",
    [
        {"rows": ({"leaked": True},)},
        {"anchors": ("Frame:deadbeef/w/001",)},
        {"partial": True},
        {"continuation": "cont_abc"},
        {"lease_id": "lse_1"},
        {"etag": "sha256:aaaa"},
        {"replica": "w"},
        {"ttl": 30},
        {"deprecated": True},
        {"successor": "Frame:deadbeef/w/002"},
    ],
)
def test_toolresult_error_envelope_forbids_success_only_fields(success_only_override: dict) -> None:
    kwargs = {"ok": False, "error": make_error(ErrorCode.CONFLICT), "cost": 4}
    kwargs.update(success_only_override)
    with pytest.raises(ValueError):
        ToolResult(**kwargs)  # type: ignore[arg-type]


def test_toolresult_rejects_negative_cost() -> None:
    with pytest.raises(ValueError):
        ToolResult(ok=True, cost=-1)


def test_toolresult_rejects_bogus_replica() -> None:
    with pytest.raises(ValueError):
        ToolResult(ok=True, replica="x")


def test_toolresult_accepts_all_nine_error_codes() -> None:
    for code in ErrorCode:
        result = ToolResult(ok=False, error=make_error(code), cost=1)
        assert result.error == {"code": code.value}


if __name__ == "__main__":
    import pytest as _pytest

    raise SystemExit(_pytest.main([__file__, "-v"]))
