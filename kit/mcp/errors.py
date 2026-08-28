"""kit/mcp/errors.py — the closed MCP error taxonomy (CONTRACTS.md section 3.3).

A tool never raises. It returns ``{"ok": false, "error": {...}, "cost": n}``
and the ``error`` dict's ``code`` is always one of exactly nine values —
never a tenth. This module is the one place those nine codes and their
charged/retry-safe metadata are defined; everything else imports
:class:`ErrorCode` / :data:`ERROR_SPECS` / :func:`make_error` from here
rather than re-typing the strings.

``unavailable`` is the load-bearing one: CONTRACTS.md 3.3 requires its
dict be **exactly** ``{"code": "unavailable"}`` — no ``reason``, no
``detail``, no ``message``, ever. That is the deck's "negotiate on the
FACT of failure, not its stated cause" lesson, and CONTRACTS.md calls out
that it is "trivially softened by a helpful developer" who wants to leave
a debugging breadcrumb. :func:`make_error` does not just document the
rule, it refuses to build a softened ``unavailable`` — passing any
``extra`` keyword for that code raises :class:`ValueError`.

Stdlib only. No network, no randomness, no wall-clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "ErrorCode",
    "ErrorSpec",
    "ERROR_SPECS",
    "make_error",
]


class ErrorCode(StrEnum):
    """The nine-member closed taxonomy (CONTRACTS.md section 3.3, the
    ``code`` column). A tool result's ``error["code"]`` is always one of
    these — nothing else is a legal MCP error code in this game."""

    LEASE_REQUIRED = "lease_required"
    LEASE_EXPIRED = "lease_expired"
    CONFLICT = "conflict"
    PRECONDITION_MISSING = "precondition_missing"
    RATE_LIMITED = "rate_limited"
    UNAUTHORIZED = "unauthorized"
    UNAVAILABLE = "unavailable"
    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    """Charged/retry-safe metadata for one :class:`ErrorCode`
    (CONTRACTS.md section 3.3's table, one row per instance)."""

    code: ErrorCode
    meaning: str
    charged: bool
    # `rate_limited` is charged like every other code AND explicitly never
    # refunded later (the table bolds "yes, no refund" for it alone) —
    # kept as a distinct flag rather than folded into `charged` so that
    # distinction survives instead of collapsing to the same bool as the
    # other eight rows.
    no_refund: bool
    # Whether retrying can ever help, in principle. False only for the two
    # rows the table marks "never" (unauthorized, not_found).
    retry_safe: bool
    # The literal "Retry-safe?" condition from the table — richer than the
    # `retry_safe` bool alone (e.g. "after fixing" vs "once" vs "next
    # window only" are all retry_safe=True but mean different things to a
    # strategy that reads this field).
    retry_note: str
    # True only for `unavailable`: no reason/detail/message key, ever.
    opaque: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.code, ErrorCode):
            raise ValueError(f"ErrorSpec.code must be an ErrorCode, got {self.code!r}")
        if self.opaque and self.code is not ErrorCode.UNAVAILABLE:
            raise ValueError("ErrorSpec.opaque=True is reserved for ErrorCode.UNAVAILABLE")


ERROR_SPECS: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.LEASE_REQUIRED: ErrorSpec(
        code=ErrorCode.LEASE_REQUIRED,
        meaning="get_frame with no live lease",
        charged=True,
        no_refund=False,
        retry_safe=True,
        retry_note="after a fresh locate",
    ),
    ErrorCode.LEASE_EXPIRED: ErrorSpec(
        code=ErrorCode.LEASE_EXPIRED,
        meaning="lease older than 3 calls",
        charged=True,
        no_refund=False,
        retry_safe=True,
        retry_note="after a fresh locate",
    ),
    ErrorCode.CONFLICT: ErrorSpec(
        code=ErrorCode.CONFLICT,
        meaning="If-Match etag stale (HTTP 409 shape)",
        charged=True,
        no_refund=False,
        retry_safe=True,
        retry_note="only after re-reading provenance",
    ),
    ErrorCode.PRECONDITION_MISSING: ErrorSpec(
        code=ErrorCode.PRECONDITION_MISSING,
        meaning="write with no If-Match or no Idempotency-Key",
        charged=True,
        no_refund=False,
        retry_safe=True,
        retry_note="after fixing",
    ),
    ErrorCode.RATE_LIMITED: ErrorSpec(
        code=ErrorCode.RATE_LIMITED,
        meaning="per-tool window exhausted",
        charged=True,
        no_refund=True,
        retry_safe=True,
        retry_note="next window only",
    ),
    ErrorCode.UNAUTHORIZED: ErrorSpec(
        code=ErrorCode.UNAUTHORIZED,
        meaning="scope/act mismatch",
        charged=True,
        no_refund=False,
        retry_safe=False,
        retry_note="never",
    ),
    ErrorCode.UNAVAILABLE: ErrorSpec(
        code=ErrorCode.UNAVAILABLE,
        meaning="opaque. No reason field. Ever.",
        charged=True,
        no_refund=False,
        retry_safe=True,
        retry_note="once",
        opaque=True,
    ),
    ErrorCode.BAD_REQUEST: ErrorSpec(
        code=ErrorCode.BAD_REQUEST,
        meaning="malformed args or unknown field in mask",
        charged=True,
        no_refund=False,
        retry_safe=True,
        retry_note="after fixing",
    ),
    ErrorCode.NOT_FOUND: ErrorSpec(
        code=ErrorCode.NOT_FOUND,
        meaning="anchor does not resolve",
        charged=True,
        no_refund=False,
        retry_safe=False,
        retry_note="never",
    ),
}

assert set(ERROR_SPECS) == set(ErrorCode), "every ErrorCode must have exactly one ErrorSpec"


def make_error(code: "ErrorCode | str", /, **extra: object) -> dict:
    """Build the ``error`` sub-dict a tool result carries (CONTRACTS.md 3.3).

    ``code`` (positional-only, so a stray ``code=`` keyword lands in
    ``extra`` and is rejected below rather than raising a confusing
    "multiple values" :class:`TypeError`) may be an :class:`ErrorCode`
    member or the matching string (``"not_found"`` etc.); anything else
    raises :class:`ValueError` naming the nine legal codes.

    Any ``extra`` keyword is folded into the returned dict as an additional
    key — e.g. ``make_error("bad_request", reason="unknown field 'x'")`` —
    **except** for :attr:`ErrorCode.UNAVAILABLE`, for which passing *any*
    ``extra`` raises :class:`ValueError`. That refusal is the point of this
    function: CONTRACTS.md 3.3 says ``unavailable``'s dict is exactly
    ``{"code": "unavailable"}``, and a docstring alone does not stop a
    future edit from attaching a well-meant ``reason=str(exc)``.
    """
    try:
        resolved = ErrorCode(code)
    except ValueError as exc:
        raise ValueError(
            f"{code!r} is not one of the nine closed error codes: "
            f"{sorted(c.value for c in ErrorCode)}"
        ) from exc

    if "code" in extra:
        raise ValueError("make_error(): 'code' is not a valid extra key (it is the first argument)")

    if resolved is ErrorCode.UNAVAILABLE and extra:
        raise ValueError(
            "make_error(ErrorCode.UNAVAILABLE, ...) refuses extra keys "
            f"{sorted(extra)}: CONTRACTS.md 3.3 requires its dict be exactly "
            '{"code": "unavailable"} — no reason, detail, or message, ever.'
        )

    return {"code": resolved.value, **extra}


if __name__ == "__main__":
    print("=== The nine-code closed taxonomy ===")
    for c in ErrorCode:
        spec = ERROR_SPECS[c]
        print(
            f"  {c.value:22} charged={spec.charged!s:5} no_refund={spec.no_refund!s:5} "
            f"retry_safe={spec.retry_safe!s:5} retry_note={spec.retry_note!r:28} "
            f"opaque={spec.opaque}"
        )
    assert len(ErrorCode) == 9, f"expected exactly 9 codes, got {len(ErrorCode)}"
    print(f"\n  {len(ErrorCode)} codes total — matches CONTRACTS.md 3.3.")

    print("\n=== make_error() demo, one call per code ===")
    for c in ErrorCode:
        err = make_error(c)
        print(f"  make_error({c.value!r}) -> {err}")
        assert err == {"code": c.value}

    print("\n=== make_error() accepts a plain string too ===")
    err = make_error("not_found")
    print(f"  make_error('not_found') -> {err}")
    assert err == {"code": "not_found"}

    print("\n=== THE invariant: unavailable carries no reason/detail/message ===")
    unavailable_err = make_error(ErrorCode.UNAVAILABLE)
    print(f"  make_error(ErrorCode.UNAVAILABLE) -> {unavailable_err}")
    assert set(unavailable_err.keys()) == {"code"}
    print(f"  set(err.keys()) == {{'code'}} -> {set(unavailable_err.keys()) == {'code'}}")

    print("\n=== The opposite: bad_request DOES accept extra context ===")
    bad_req_err = make_error(
        ErrorCode.BAD_REQUEST, reason="unknown field 'nope' in mask", field="nope"
    )
    print(f"  make_error(BAD_REQUEST, reason=..., field=...) -> {bad_req_err}")
    assert set(bad_req_err.keys()) == {"code", "reason", "field"}
    print(f"  set(err.keys()) == {{'code','reason','field'}} -> "
          f"{set(bad_req_err.keys()) == {'code', 'reason', 'field'}}")

    print("\n=== Rejection demo (each must raise ValueError) ===")

    def _expect_value_error(label: str, fn) -> None:
        try:
            fn()
        except ValueError as exc:
            print(f"  [{label:38}] -> ValueError: {exc}")
        else:
            raise AssertionError(f"expected ValueError for case {label!r}")

    _expect_value_error(
        "unavailable + reason kwarg",
        lambda: make_error(ErrorCode.UNAVAILABLE, reason="db is down"),
    )
    _expect_value_error(
        "unavailable + detail kwarg",
        lambda: make_error("unavailable", detail="timeout after 3s"),
    )
    _expect_value_error(
        "unavailable + message kwarg",
        lambda: make_error("unavailable", message="try again"),
    )
    _expect_value_error("unknown code string", lambda: make_error("teapot"))
    _expect_value_error("code passed again as extra", lambda: make_error("not_found", code="x"))

    print("\nAll errors.py demos passed.")
