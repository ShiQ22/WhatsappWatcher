"""
Tests for detector call session lifecycle — Issue 1 regression guard.

Key invariants verified:
- status=Voice call / phase=call_present must NOT emit ENDED while the window exists.
- Same hwnd must not emit repeated ring events while the session is alive.
- Window disappearance emits exactly one ENDED.
- Explicit call-ended status text emits ENDED while window is present.
- Fast back-to-back calls on different hwnds produce two separate sessions.
- No-answer / window-disappears still finalizes.
- Minimized (invisible) window found via allow_invisible=True preserves the session.

Run with:  python -m pytest tests/test_detector_lifecycle.py -v
"""
from __future__ import annotations

import sys
import types
import unittest
from typing import Optional
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Lightweight stubs so detector.py imports cleanly in a headless environment.
# comtypes / pywinauto are Windows-only; psutil is real.
# ---------------------------------------------------------------------------
def _stub_modules():
    for name in ("comtypes", "comtypes.client", "comtypes.gen",
                 "comtypes.gen.UIAutomationClient"):
        if name not in sys.modules:
            sys.modules[name] = MagicMock()
    if "pywinauto" not in sys.modules:
        sys.modules["pywinauto"] = MagicMock()


_stub_modules()

from detector import (  # noqa: E402  (after stub setup)
    WhatsAppDetector,
    _WinInfo,
    _UIAState,
)
from state_machine import CallEvent  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_win(hwnd: int = 461598, pid: int = 9999,
              w: int = 240, h: int = 200) -> _WinInfo:
    return _WinInfo(hwnd=hwnd, pid=pid, width=w, height=h)


def _make_state(
    *,
    incoming: bool = False,
    outgoing: bool = False,
    answered: bool = False,
    ringing: bool = False,
    connecting: bool = False,
    has_end_call_button: bool = False,
    timer_text: Optional[str] = None,
    status_text: str = "Voice call",
    call_status_raw: str = "Voice call",
    caller_number: str = "+96567644467",
    no_answer: bool = False,
) -> _UIAState:
    s = _UIAState(source="comtypes")
    s.incoming = incoming
    s.outgoing = outgoing
    s.answered = answered
    s.ringing = ringing
    s.connecting = connecting
    s.has_end_call_button = has_end_call_button
    s.timer_text = timer_text
    s.status_text = status_text
    s.call_status_raw = call_status_raw
    s.caller_number = caller_number
    s.no_answer = no_answer
    s.scan_ms = 30.0
    s.details = (
        f"incoming={incoming} | outgoing={outgoing} | ringing={ringing}"
        f" | connecting={connecting} | answered={answered}"
        f" | end_call_btn={has_end_call_button} | status={status_text}"
    )
    return s


def _build_detector() -> WhatsAppDetector:
    det = WhatsAppDetector()
    return det


def _poll_once(det: WhatsAppDetector,
               win: Optional[_WinInfo],
               state: Optional[_UIAState]) -> "DetectionResult":
    """Call det.poll() once with injected window and scan results."""
    # _find_call_window is called twice (allow_invisible=False, allow_invisible=True).
    # Using return_value so both calls return the same value.
    with (
        patch("detector.WhatsAppDetector.get_whatsapp_pids", return_value={9999}),
        patch("detector._find_call_window", return_value=win),
        patch.object(det, "_scan_with_failover",
                     return_value=(state, "comtypes") if state else (None, "none")),
    ):
        return det.poll()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDetectorLifecycle(unittest.TestCase):

    # ────────────────────────────────────────────────────────────────────────
    # Test 1 — Voice call / call_present does NOT emit ENDED while window exists
    # ────────────────────────────────────────────────────────────────────────

    def test_call_present_voice_call_does_not_emit_ended(self):
        """
        Production bug 454520: same hwnd, status=Voice call, all flags False.
        Previously the stale-ringing block fired after 5 s while win was present.
        After fix: no ENDED must be emitted regardless of poll count.
        """
        det = _build_detector()
        win = _make_win(hwnd=461598)
        # All flags False — exactly the bug-triggering state
        state = _make_state(status_text="Voice call")

        events = []
        for _ in range(15):   # well past the old 5 s stale-ringing threshold
            r = _poll_once(det, win, state)
            events.append(r)

        ended = [r for r in events if r.event == CallEvent.ENDED]
        self.assertEqual(
            len(ended), 0,
            f"ENDED must not fire while Voice Call window is present; got: {ended}",
        )

    # ────────────────────────────────────────────────────────────────────────
    # Test 2 — Same hwnd must not emit repeated ring while session alive
    # ────────────────────────────────────────────────────────────────────────

    def test_same_hwnd_no_repeated_call_started(self):
        det = _build_detector()
        win = _make_win(hwnd=461598)
        state = _make_state(status_text="Voice call")

        ring_types = {CallEvent.CALL_STARTED, CallEvent.INCOMING_RING, CallEvent.OUTGOING_RING}
        ring_events = []
        for _ in range(10):
            r = _poll_once(det, win, state)
            if r.event in ring_types:
                ring_events.append(r)

        self.assertEqual(
            len(ring_events), 1,
            f"Exactly one ring event for same hwnd; got {len(ring_events)}",
        )

    # ────────────────────────────────────────────────────────────────────────
    # Test 3 — Window disappearance emits exactly one ENDED
    # ────────────────────────────────────────────────────────────────────────

    def test_window_disappearance_after_voice_call_emits_one_ended(self):
        det = _build_detector()
        win = _make_win(hwnd=461598)
        state = _make_state(status_text="Voice call")

        # Establish session (ring emitted + several call_present polls)
        for _ in range(5):
            _poll_once(det, win, state)

        # Window disappears
        ended_count = 0
        for _ in range(3):
            r = _poll_once(det, None, None)
            if r.event == CallEvent.ENDED:
                ended_count += 1

        self.assertEqual(
            ended_count, 1,
            "Exactly one ENDED when window disappears",
        )

    # ────────────────────────────────────────────────────────────────────────
    # Test 4 — Explicit call-ended status text emits ENDED while window present
    # ────────────────────────────────────────────────────────────────────────

    def test_ended_status_text_emits_ended_while_window_present(self):
        det = _build_detector()
        win = _make_win(hwnd=461598)
        state_voice = _make_state(status_text="Voice call")
        state_ended = _make_state(status_text="call ended", call_status_raw="call ended")

        # Ring first
        r_ring = _poll_once(det, win, state_voice)
        ring_types = {CallEvent.CALL_STARTED, CallEvent.INCOMING_RING, CallEvent.OUTGOING_RING}
        self.assertIn(r_ring.event, ring_types, "First poll must emit a ring event")

        # Status changes to "call ended" — window still present
        r = _poll_once(det, win, state_ended)
        self.assertEqual(
            r.event, CallEvent.ENDED,
            "ENDED must fire when status text shows 'call ended' (window still present)",
        )

    # ────────────────────────────────────────────────────────────────────────
    # Test 5 — Fast back-to-back different hwnd → two separate sessions
    # ────────────────────────────────────────────────────────────────────────

    def test_fast_back_to_back_different_hwnd(self):
        det = _build_detector()
        win_a = _make_win(hwnd=100001)
        win_b = _make_win(hwnd=100002)
        state_ring = _make_state(ringing=True, outgoing=True, status_text="calling...")

        ring_types = {CallEvent.CALL_STARTED, CallEvent.INCOMING_RING, CallEvent.OUTGOING_RING}

        # Session A: ring emitted
        r_a = _poll_once(det, win_a, state_ring)
        self.assertIn(r_a.event, ring_types, "Session A should emit a ring event")

        # Session A: window disappears → ENDED
        r_end_a = _poll_once(det, None, None)
        self.assertEqual(r_end_a.event, CallEvent.ENDED, "Session A should end on window disappearance")

        # Session B: new hwnd → new ring
        r_b = _poll_once(det, win_b, state_ring)
        self.assertIn(r_b.event, ring_types, "Session B should emit a ring event")
        self.assertEqual(r_b.hwnd, win_b.hwnd, "Session B ring hwnd must be win_b")
        self.assertNotEqual(r_a.hwnd, r_b.hwnd, "Two sessions must carry different hwnds")

    # ────────────────────────────────────────────────────────────────────────
    # Test 6 — No-answer window disappearance emits ENDED
    # ────────────────────────────────────────────────────────────────────────

    def test_no_answer_window_disappears_emits_ended(self):
        det = _build_detector()
        win = _make_win(hwnd=200001)
        state_ringing = _make_state(ringing=True, outgoing=True, status_text="calling...")

        ring_types = {CallEvent.CALL_STARTED, CallEvent.OUTGOING_RING}

        r_ring = _poll_once(det, win, state_ringing)
        self.assertIn(r_ring.event, ring_types, "Ring must be emitted")

        # No answer — window disappears
        r_ended = _poll_once(det, None, None)
        self.assertEqual(
            r_ended.event, CallEvent.ENDED,
            "ENDED must be emitted when no-answer window disappears",
        )

    # ────────────────────────────────────────────────────────────────────────
    # Test 7 — Minimized / invisible window preserves session
    # ────────────────────────────────────────────────────────────────────────

    def test_minimized_invisible_window_preserves_session(self):
        """
        _find_call_window is called twice per poll:
          first with allow_invisible=False (returns None — window not visible)
          then with allow_invisible=True  (returns win — found as invisible)
        The session must be preserved; no ENDED emitted.
        """
        det = _build_detector()
        win = _make_win(hwnd=300001)
        state_voice = _make_state(status_text="Voice call")

        ring_types = {CallEvent.CALL_STARTED, CallEvent.INCOMING_RING, CallEvent.OUTGOING_RING}

        # First poll: window visible — ring emitted
        r_ring = _poll_once(det, win, state_voice)
        self.assertIn(r_ring.event, ring_types, "Initial ring must be emitted")

        # Subsequent polls: visible=False but invisible=True finds the window.
        # side_effect=[None, win] → first call returns None, second returns win.
        ended_seen = False
        for _ in range(5):
            with (
                patch("detector.WhatsAppDetector.get_whatsapp_pids", return_value={9999}),
                patch("detector._find_call_window", side_effect=[None, win]),
                patch.object(det, "_scan_with_failover",
                             return_value=(state_voice, "comtypes")),
            ):
                r = det.poll()
                if r.event == CallEvent.ENDED:
                    ended_seen = True

        self.assertFalse(
            ended_seen,
            "Session must be preserved when window is found via allow_invisible=True",
        )


if __name__ == "__main__":
    unittest.main()
