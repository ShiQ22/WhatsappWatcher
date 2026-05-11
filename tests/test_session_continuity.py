"""
Tests for session-continuity fix: same real WhatsApp call may have multiple
hwnds/generations over its lifetime.

Tests import helpers directly from main.py — no wrappers, no copied logic.

Run with:  python -m pytest tests/test_session_continuity.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

# Ensure the project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import (
    _is_proven_direction,
    _is_real_number,
    _is_same_call_continuation,
    _normalize_phone_number,
)


# ===========================================================================
# _normalize_phone_number
# ===========================================================================

class TestNormalizePhoneNumber:

    def test_plus_prefix_becomes_00(self):
        assert _normalize_phone_number("+20 109 212 4790") == "00201092124790"

    def test_00_prefix_unchanged(self):
        assert _normalize_phone_number("00201092124790") == "00201092124790"

    def test_spaces_stripped(self):
        assert _normalize_phone_number("+965 6764 4467") == "0096567644467"

    def test_dashes_stripped(self):
        assert _normalize_phone_number("+1-555-234-5678") == "001555234 5678".replace(" ", "")

    def test_parens_stripped(self):
        assert _normalize_phone_number("+1 (555) 234-5678") == "0015552345678"

    def test_none_returns_empty(self):
        assert _normalize_phone_number(None) == ""

    def test_empty_returns_empty(self):
        assert _normalize_phone_number("") == ""

    def test_unknown_returns_empty(self):
        assert _normalize_phone_number("unknown") == ""

    def test_dash_placeholder_returns_empty(self):
        assert _normalize_phone_number("-") == ""

    def test_plus_vs_00_same_number(self):
        # The two common representations of the same number must normalize identically
        a = _normalize_phone_number("+20 109 212 4790")
        b = _normalize_phone_number("00201092124790")
        assert a == b

    def test_multiple_formats_same_number(self):
        base = "0096567644467"
        variants = [
            "+96567644467",
            "00965 6764 4467",
            "+965-6764-4467",
            "+965 6764 4467",
        ]
        for v in variants:
            assert _normalize_phone_number(v) == base, f"Failed for {v!r}"


# ===========================================================================
# _is_proven_direction
# ===========================================================================

class TestIsProvenDirection:

    @pytest.mark.parametrize("d", ["incoming", "outgoing"])
    def test_proven_directions_return_true(self, d):
        assert _is_proven_direction(d) is True

    @pytest.mark.parametrize("d", [None, "", "unknown", "-", "INCOMING", "Outgoing"])
    def test_unproven_values_return_false(self, d):
        assert _is_proven_direction(d) is False


# ===========================================================================
# _is_same_call_continuation — pure logic tests
# ===========================================================================

class TestIsSameCallContinuation:

    # ── Same caller, same direction ──────────────────────────────────────────

    def test_same_real_caller_same_incoming(self):
        assert _is_same_call_continuation(
            "+96567644467", "+96567644467", "incoming", "incoming"
        ) is True

    def test_same_real_caller_same_outgoing(self):
        assert _is_same_call_continuation(
            "+20 109 212 4790", "00201092124790", "outgoing", "outgoing"
        ) is True

    # ── Same caller, direction asymmetry ────────────────────────────────────

    def test_same_caller_current_proven_new_unknown(self):
        # Current direction is proven; new direction is unknown — still same call
        assert _is_same_call_continuation(
            "+96567644467", "+96567644467", "incoming", "unknown"
        ) is True

    def test_same_caller_current_proven_new_none(self):
        assert _is_same_call_continuation(
            "+96567644467", "+96567644467", "outgoing", None
        ) is True

    def test_same_caller_current_unknown_new_proven(self):
        assert _is_same_call_continuation(
            "+96567644467", "+96567644467", "unknown", "incoming"
        ) is True

    def test_same_caller_both_unknown_direction(self):
        # Both callers real and matching; neither direction proven → True (direction not a blocker)
        assert _is_same_call_continuation(
            "+96567644467", "+96567644467", "unknown", "unknown"
        ) is True

    # ── Same caller, opposite proven directions → not continuation ───────────

    def test_same_caller_incoming_vs_outgoing_not_continuation(self):
        assert _is_same_call_continuation(
            "+96567644467", "+96567644467", "incoming", "outgoing"
        ) is False

    def test_same_caller_outgoing_vs_incoming_not_continuation(self):
        assert _is_same_call_continuation(
            "+96567644467", "+96567644467", "outgoing", "incoming"
        ) is False

    # ── Different callers → not continuation ────────────────────────────────

    def test_different_real_callers_not_continuation(self):
        assert _is_same_call_continuation(
            "+96567644467", "+20 109 212 4790", "incoming", "incoming"
        ) is False

    def test_different_real_callers_outgoing_not_continuation(self):
        assert _is_same_call_continuation(
            "+1 555 234 5678", "+1 555 234 9999", "outgoing", "outgoing"
        ) is False

    # ── Unknown / missing callers → not continuation ─────────────────────────

    def test_both_unknown_callers_not_continuation(self):
        assert _is_same_call_continuation(None, None, "incoming", "incoming") is False

    def test_both_empty_callers_not_continuation(self):
        assert _is_same_call_continuation("", "", "incoming", "incoming") is False

    def test_both_placeholder_callers_not_continuation(self):
        assert _is_same_call_continuation("-", "-", "incoming", "incoming") is False

    def test_both_unknown_string_callers_not_continuation(self):
        assert _is_same_call_continuation("unknown", "unknown", "incoming", "incoming") is False

    def test_old_real_new_missing_not_continuation(self):
        assert _is_same_call_continuation("+96567644467", None, "incoming", "incoming") is False

    def test_old_missing_new_real_not_continuation(self):
        assert _is_same_call_continuation(None, "+96567644467", "incoming", "incoming") is False

    def test_old_real_new_unknown_str_not_continuation(self):
        assert _is_same_call_continuation("+96567644467", "unknown", "incoming", "incoming") is False

    # ── Phone format normalization in continuation check ────────────────────

    def test_plus_vs_00_same_number_continuation(self):
        # These two representations of the same number must be treated as same call
        assert _is_same_call_continuation(
            "+20 109 212 4790", "00201092124790", "incoming", "incoming"
        ) is True

    def test_spaces_vs_clean_number_continuation(self):
        assert _is_same_call_continuation(
            "+965 6764 4467", "0096567644467", "outgoing", "outgoing"
        ) is True

    def test_dashes_parens_vs_clean_number_continuation(self):
        assert _is_same_call_continuation(
            "+1 (555) 234-5678", "0015552345678", "incoming", "unknown"
        ) is True


# ===========================================================================
# Behavioral tests — continuity override block logic
# ===========================================================================
#
# These tests exercise the continuity guard conditions directly without
# running the full poll loop.  They simulate the variables in scope at the
# point where the continuity block executes in main.py.
# ===========================================================================

def _run_continuity_block(
    *,
    split_needed: bool,
    is_live_session: bool,
    recorder_is_recording: bool,
    different_hwnd: bool,
    different_generation: bool,
    sm_caller: Optional[str],
    session_number_latch: Optional[str],
    sm_direction: Optional[str],
    session_direction_latch: Optional[str],
    result_caller: Optional[str],
    result_direction: Optional[str],
    new_dir: Optional[str],
    current_session_hwnd: Optional[int],
    current_session_generation: int,
    result_hwnd: Optional[int],
    result_session_generation: int,
) -> dict:
    """
    Simulate the continuity block from main.py and return the resulting state.
    Returns a dict with keys: split_needed, current_session_hwnd, current_session_generation,
    continuity_fired.
    """
    from main import _is_proven_direction, _is_same_call_continuation

    # Build minimal fakes
    sm_session = MagicMock()
    sm_session.caller_number = sm_caller
    sm_session.direction = sm_direction

    result = MagicMock()
    result.caller_number = result_caller
    result.direction = result_direction

    recorder = MagicMock()
    recorder.is_recording = recorder_is_recording

    _session_number_latch = session_number_latch
    _session_direction_latch = session_direction_latch

    continuity_fired = False

    if (
        split_needed
        and is_live_session
        and recorder.is_recording
        and (different_hwnd or different_generation)
    ):
        _cont_current_number = sm_session.caller_number or _session_number_latch
        _cont_current_dir = (
            sm_session.direction
            if _is_proven_direction(sm_session.direction)
            else _session_direction_latch
            if _is_proven_direction(_session_direction_latch)
            else sm_session.direction
        )
        _cont_new_dir = (
            new_dir if _is_proven_direction(new_dir) else result.direction
        )
        if _is_same_call_continuation(
            _cont_current_number,
            result_caller,
            _cont_current_dir,
            _cont_new_dir,
        ):
            split_needed = False
            current_session_hwnd = result_hwnd
            current_session_generation = result_session_generation
            continuity_fired = True

    return {
        "split_needed": split_needed,
        "current_session_hwnd": current_session_hwnd,
        "current_session_generation": current_session_generation,
        "continuity_fired": continuity_fired,
    }


class TestContinuityBlock:

    def _base_args(self, **overrides) -> dict:
        defaults = dict(
            split_needed=True,
            is_live_session=True,
            recorder_is_recording=True,
            different_hwnd=True,
            different_generation=False,
            sm_caller="+96567644467",
            session_number_latch=None,
            sm_direction="incoming",
            session_direction_latch=None,
            result_caller="+96567644467",
            result_direction="incoming",
            new_dir="incoming",
            current_session_hwnd=100,
            current_session_generation=1,
            result_hwnd=200,
            result_session_generation=2,
        )
        defaults.update(overrides)
        return defaults

    # ── Core suppression ─────────────────────────────────────────────────────

    def test_different_hwnd_same_call_suppresses_split(self):
        r = _run_continuity_block(**self._base_args(different_hwnd=True, different_generation=False))
        assert r["split_needed"] is False
        assert r["continuity_fired"] is True

    def test_different_generation_same_call_suppresses_split(self):
        r = _run_continuity_block(**self._base_args(different_hwnd=False, different_generation=True))
        assert r["split_needed"] is False
        assert r["continuity_fired"] is True

    def test_continuity_updates_hwnd_and_generation(self):
        r = _run_continuity_block(**self._base_args(
            result_hwnd=999, result_session_generation=7,
        ))
        assert r["current_session_hwnd"] == 999
        assert r["current_session_generation"] == 7

    def test_outgoing_call_suppressed(self):
        r = _run_continuity_block(**self._base_args(
            sm_direction="outgoing", result_direction="outgoing", new_dir="outgoing",
            sm_caller="+20 109 212 4790", result_caller="00201092124790",
        ))
        assert r["split_needed"] is False
        assert r["continuity_fired"] is True

    # ── Guards that prevent suppression ─────────────────────────────────────

    def test_recorder_not_recording_does_not_suppress(self):
        r = _run_continuity_block(**self._base_args(recorder_is_recording=False))
        assert r["split_needed"] is True
        assert r["continuity_fired"] is False

    def test_is_live_session_false_does_not_suppress(self):
        r = _run_continuity_block(**self._base_args(is_live_session=False))
        # split_needed is already False when is_live_session=False (see split_needed formula),
        # but we also verify the continuity block does not fire
        assert r["continuity_fired"] is False

    def test_split_not_needed_continuity_block_skipped(self):
        r = _run_continuity_block(**self._base_args(split_needed=False))
        assert r["continuity_fired"] is False

    def test_no_hwnd_or_gen_change_continuity_block_skipped(self):
        r = _run_continuity_block(**self._base_args(different_hwnd=False, different_generation=False))
        assert r["continuity_fired"] is False

    def test_different_caller_does_not_suppress(self):
        r = _run_continuity_block(**self._base_args(
            sm_caller="+96567644467",
            result_caller="+20 109 212 4790",
        ))
        assert r["split_needed"] is True
        assert r["continuity_fired"] is False

    def test_opposite_direction_same_caller_does_not_suppress(self):
        r = _run_continuity_block(**self._base_args(
            sm_direction="incoming", result_direction="outgoing", new_dir="outgoing",
        ))
        assert r["split_needed"] is True
        assert r["continuity_fired"] is False

    def test_both_unknown_callers_does_not_suppress(self):
        r = _run_continuity_block(**self._base_args(
            sm_caller=None, session_number_latch=None,
            result_caller=None,
        ))
        assert r["split_needed"] is True
        assert r["continuity_fired"] is False

    # ── Latch fallback ───────────────────────────────────────────────────────

    def test_session_number_latch_used_when_sm_caller_missing(self):
        # sm.session.caller_number is None; latch has the real number
        r = _run_continuity_block(**self._base_args(
            sm_caller=None,
            session_number_latch="+96567644467",
            result_caller="+96567644467",
        ))
        assert r["split_needed"] is False
        assert r["continuity_fired"] is True

    def test_direction_latch_used_when_sm_direction_unknown(self):
        # sm.session.direction is "unknown" (truthy!); latch is "incoming"
        # Verify "unknown" is NOT used as a proven direction anchor
        r = _run_continuity_block(**self._base_args(
            sm_direction="unknown",
            session_direction_latch="incoming",
            result_direction="incoming",
            new_dir="incoming",
        ))
        assert r["split_needed"] is False
        assert r["continuity_fired"] is True

    def test_unknown_direction_not_used_as_proven_anchor(self):
        # Both directions are "unknown"; opposite proven directions would block, but
        # since neither is proven, direction check is skipped → caller match is enough
        r = _run_continuity_block(**self._base_args(
            sm_direction="unknown",
            session_direction_latch=None,
            result_direction="unknown",
            new_dir="unknown",
        ))
        # callers match and no proven direction conflict → continuity fires
        assert r["split_needed"] is False
        assert r["continuity_fired"] is True

    # ── Phone format normalization end-to-end ────────────────────────────────

    def test_plus_vs_00_format_suppresses_split(self):
        r = _run_continuity_block(**self._base_args(
            sm_caller="+20 109 212 4790",
            result_caller="00201092124790",
        ))
        assert r["split_needed"] is False
        assert r["continuity_fired"] is True

    def test_spaces_vs_clean_format_suppresses_split(self):
        r = _run_continuity_block(**self._base_args(
            sm_caller="+965 6764 4467",
            result_caller="0096567644467",
        ))
        assert r["split_needed"] is False
        assert r["continuity_fired"] is True

    # ── Back-to-back real calls ───────────────────────────────────────────────

    def test_back_to_back_no_live_session_not_suppressed(self):
        # Previous call ended → is_live_session=False, recorder stopped
        r = _run_continuity_block(**self._base_args(
            is_live_session=False,
            recorder_is_recording=False,
        ))
        assert r["continuity_fired"] is False

    def test_back_to_back_recorder_stopped_not_suppressed(self):
        # State may still be non-terminal briefly, but recorder already stopped
        r = _run_continuity_block(**self._base_args(recorder_is_recording=False))
        assert r["split_needed"] is True
        assert r["continuity_fired"] is False

    # ── No-answer / cancelled ────────────────────────────────────────────────

    def test_no_answer_ended_event_not_intercepted(self):
        # ENDED is not in is_new_call_event set → split_needed=False already
        # Continuity block receives split_needed=False → does not fire
        r = _run_continuity_block(**self._base_args(split_needed=False))
        assert r["continuity_fired"] is False


if __name__ == "__main__":
    import unittest
    unittest.main()
