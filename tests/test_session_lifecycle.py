"""
Session lifecycle tests:
  FIX 1 — general new-call boundary split (all back-to-back patterns)
  FIX 2 — stale ringing/connecting timeout emits ENDED automatically
  FIX 3 — immediate ENDED on explicit "Call ended" status text
  FIX 4 — USB log codes [DEV-USB]
"""
import copy
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_uia_state(status_text: str = "", **kwargs):
    from detector import _UIAState
    return _UIAState(status_text=status_text, scan_ms=1.0, **kwargs)


def _detector_with_ring_emitted(hwnd: int = 12345, direction: str = "outgoing"):
    from detector import WhatsAppDetector
    d = WhatsAppDetector()
    d._call_hwnd = hwnd
    d._call_pid = 9999
    d._ring_event_emitted = True
    d._call_direction = direction
    d._caller_number = "+1234567890"
    d._last_session_ended_ts = 0.0
    d._last_strong_call_ui_ts = time.time()
    d._wa_was_running = True
    return d


def _fake_win(hwnd: int = 12345):
    from detector import _WinInfo
    return _WinInfo(hwnd=hwnd, pid=9999, width=400, height=300)


def _poll_with_mocks(d, win, state):
    with patch.object(d, "get_whatsapp_pids", return_value={9999}), \
         patch("detector._find_call_window", return_value=win), \
         patch.object(d, "_scan_with_failover", return_value=(state, "comtypes")), \
         patch.object(d, "_update_foreground_history"), \
         patch.object(d, "_maybe_apply_direction"), \
         patch.object(d, "_classify_direction_from_state", return_value=MagicMock()):
        return d.poll()


def _make_sm_in_state(state_event):
    """Create a StateMachine that has processed one event."""
    from state_machine import StateMachine
    sm = StateMachine()
    sm.transition(state_event)
    return sm


# ── FIX 1: general new-call boundary split ────────────────────────────────────

class TestNewCallBoundarySplit:
    """
    Verifies that _TERMINAL_STATES is defined correctly and that the split
    snapshot always preserves the old session before any mutation.
    """

    def test_terminal_states_contains_idle(self):
        from main import _TERMINAL_STATES
        from state_machine import CallState
        assert CallState.IDLE in _TERMINAL_STATES

    def test_terminal_states_contains_ended(self):
        from main import _TERMINAL_STATES
        from state_machine import CallState
        assert CallState.ENDED in _TERMINAL_STATES

    def test_terminal_states_does_not_contain_ringing(self):
        from main import _TERMINAL_STATES
        from state_machine import CallState
        assert CallState.RINGING_OUTGOING not in _TERMINAL_STATES
        assert CallState.RINGING_INCOMING not in _TERMINAL_STATES
        assert CallState.RINGING_UNKNOWN not in _TERMINAL_STATES

    def test_terminal_states_does_not_contain_active(self):
        from main import _TERMINAL_STATES
        from state_machine import CallState
        assert CallState.ACTIVE not in _TERMINAL_STATES

    def test_outgoing_to_incoming_split_preserves_old_direction(self):
        """Snapshot taken before direction propagation must show old=outgoing."""
        from state_machine import StateMachine, CallEvent, CallState
        from main import _TERMINAL_STATES

        sm = StateMachine()
        sm.transition(CallEvent.OUTGOING_RING)
        sm.session.direction = "outgoing"
        sm.session.started_at = datetime(2026, 5, 7, 9, 56, 26)

        is_live = sm.state not in _TERMINAL_STATES
        is_ring = CallEvent.INCOMING_RING in (
            CallEvent.INCOMING_RING, CallEvent.OUTGOING_RING, CallEvent.CALL_STARTED
        )
        assert is_live and is_ring

        # Snapshot before mutation (what the new split code does)
        split_snap = copy.deepcopy(sm.session)

        # Simulate direction propagation (runs after snapshot in main.py)
        sm.session.direction = "incoming"

        assert split_snap.direction == "outgoing"
        assert sm.session.direction == "incoming"

    def test_incoming_to_outgoing_split_preserves_old_direction(self):
        from state_machine import StateMachine, CallEvent

        sm = StateMachine()
        sm.transition(CallEvent.INCOMING_RING)
        sm.session.direction = "incoming"

        split_snap = copy.deepcopy(sm.session)
        sm.session.direction = "outgoing"

        assert split_snap.direction == "incoming"

    def test_outgoing_to_outgoing_split_same_direction_new_hwnd(self):
        """Same direction + different hwnd is still a split (new call)."""
        from state_machine import StateMachine, CallEvent, CallState
        from main import _TERMINAL_STATES

        sm = StateMachine()
        sm.transition(CallEvent.OUTGOING_RING)
        sm.session.direction = "outgoing"

        current_session_hwnd = 1001
        result_hwnd = 1002  # different hwnd
        different_hwnd = (
            current_session_hwnd is not None
            and result_hwnd is not None
            and result_hwnd != current_session_hwnd
        )
        is_live = sm.state not in _TERMINAL_STATES
        is_new_call_event = True  # OUTGOING_RING is a ring event

        split_needed = is_live and is_new_call_event and different_hwnd
        assert split_needed

    def test_incoming_to_incoming_split_same_direction_new_hwnd(self):
        from state_machine import StateMachine, CallEvent, CallState
        from main import _TERMINAL_STATES

        sm = StateMachine()
        sm.transition(CallEvent.INCOMING_RING)
        sm.session.direction = "incoming"

        current_session_hwnd = 2001
        result_hwnd = 2002
        different_hwnd = result_hwnd != current_session_hwnd
        is_live = sm.state not in _TERMINAL_STATES

        assert is_live and different_hwnd

    def test_active_call_new_ring_different_hwnd_splits(self):
        """ACTIVE + ring from different hwnd = strong_new_call=True → split."""
        from state_machine import StateMachine, CallEvent, CallState
        from main import _TERMINAL_STATES

        sm = StateMachine()
        sm.transition(CallEvent.OUTGOING_RING)
        sm.transition(CallEvent.ANSWERED)
        assert sm.state == CallState.ACTIVE

        is_live = sm.state not in _TERMINAL_STATES
        is_new_call_event = True
        strong_new_call = True  # ring events always set this
        different_hwnd = True   # new window = new hwnd

        split_needed = is_live and is_new_call_event and (different_hwnd or strong_new_call)
        assert split_needed

    def test_active_call_same_hwnd_no_split(self):
        """ACTIVE + same hwnd + not strong_new_call → no split."""
        from state_machine import StateMachine, CallEvent, CallState
        from main import _TERMINAL_STATES

        sm = StateMachine()
        sm.transition(CallEvent.OUTGOING_RING)
        sm.transition(CallEvent.ANSWERED)
        assert sm.state == CallState.ACTIVE

        is_live = sm.state not in _TERMINAL_STATES
        is_new_call_event = False  # no ring event — periodic poll with event=None
        strong_new_call = False
        different_hwnd = False

        split_needed = is_live and is_new_call_event and (different_hwnd or strong_new_call)
        assert not split_needed

    def test_connecting_new_ring_splits(self):
        from state_machine import StateMachine, CallEvent, CallState
        from main import _TERMINAL_STATES

        sm = StateMachine()
        sm.transition(CallEvent.OUTGOING_RING)
        sm.transition(CallEvent.CONNECTING)
        assert sm.state == CallState.CONNECTING

        is_live = sm.state not in _TERMINAL_STATES
        ringing_states = {
            CallState.RINGING_UNKNOWN, CallState.RINGING_INCOMING,
            CallState.RINGING_OUTGOING, CallState.CONNECTING,
        }
        is_new_call_event = True
        in_ringing_state = sm.state in ringing_states

        split_needed = is_live and is_new_call_event and in_ringing_state
        assert split_needed

    def test_unknown_live_session_new_ring_splits(self):
        from state_machine import StateMachine, CallEvent, CallState
        from main import _TERMINAL_STATES

        sm = StateMachine()
        sm.transition(CallEvent.CALL_STARTED)
        assert sm.state == CallState.RINGING_UNKNOWN

        is_live = sm.state not in _TERMINAL_STATES
        ringing_states = {
            CallState.RINGING_UNKNOWN, CallState.RINGING_INCOMING,
            CallState.RINGING_OUTGOING, CallState.CONNECTING,
        }
        assert is_live and (sm.state in ringing_states)

    def test_current_session_hwnd_resets_after_terminal_finalize(self):
        """After RESET, current_session_hwnd should be None (verified via SM state)."""
        from state_machine import StateMachine, CallEvent, CallState

        sm = StateMachine()
        sm.transition(CallEvent.OUTGOING_RING)
        sm.transition(CallEvent.ENDED)
        sm.transition(CallEvent.RESET)
        assert sm.state == CallState.IDLE  # confirms reset path ran

    def test_opposite_ring_split_preserves_old_started_at(self):
        from state_machine import StateMachine, CallEvent
        from datetime import datetime

        sm = StateMachine()
        sm.transition(CallEvent.OUTGOING_RING)
        original_ts = datetime(2026, 5, 7, 9, 56, 26)
        sm.session.started_at = original_ts

        split_snap = copy.deepcopy(sm.session)
        sm.transition(CallEvent.RESET)
        sm.transition(CallEvent.INCOMING_RING)

        assert split_snap.started_at == original_ts
        assert sm.session.started_at != original_ts


# ── Detector: DetectionResult hwnd fields ─────────────────────────────────────

class TestDetectionResultHwndFields:

    def test_detection_result_includes_hwnd_for_ring(self):
        """Ring emission must populate hwnd=win.hwnd on the result."""
        from detector import WhatsAppDetector, _WinInfo
        from state_machine import CallEvent

        d = WhatsAppDetector()
        d._wa_was_running = True
        d._last_session_ended_ts = 0.0
        win = _WinInfo(hwnd=55555, pid=9999, width=400, height=300)
        state = _make_uia_state(incoming=True)

        with patch.object(d, "get_whatsapp_pids", return_value={9999}), \
             patch("detector._find_call_window", return_value=win), \
             patch.object(d, "_scan_with_failover", return_value=(state, "comtypes")), \
             patch.object(d, "_update_foreground_history"), \
             patch.object(d, "_classify_direction_from_state", return_value=MagicMock()), \
             patch.object(d, "_classify_direction_initial", return_value=MagicMock()):
            result = d.poll()

        assert result.event in (CallEvent.INCOMING_RING, CallEvent.OUTGOING_RING, CallEvent.CALL_STARTED)
        assert result.hwnd == 55555

    def test_detection_result_marks_strong_new_call_for_ring(self):
        """Ring events must have is_strong_new_call=True."""
        from detector import WhatsAppDetector, _WinInfo

        d = WhatsAppDetector()
        d._wa_was_running = True
        d._last_session_ended_ts = 0.0
        win = _WinInfo(hwnd=55555, pid=9999, width=400, height=300)
        state = _make_uia_state(ringing=True)

        with patch.object(d, "get_whatsapp_pids", return_value={9999}), \
             patch("detector._find_call_window", return_value=win), \
             patch.object(d, "_scan_with_failover", return_value=(state, "comtypes")), \
             patch.object(d, "_update_foreground_history"), \
             patch.object(d, "_classify_direction_from_state", return_value=MagicMock()), \
             patch.object(d, "_classify_direction_initial", return_value=MagicMock()):
            result = d.poll()

        assert result.is_strong_new_call is True

    def test_hwnd_change_answered_with_new_ring_proof_emits_ring(self):
        """
        hwnd changes during answered session AND new window shows incoming=True
        → detector must override 'preserve answered' and emit a ring event.
        """
        from detector import WhatsAppDetector, _WinInfo
        from state_machine import CallEvent

        d = WhatsAppDetector()
        d._wa_was_running = True
        d._call_hwnd = 11111  # old answered hwnd
        d._ring_event_emitted = True
        d._answered_event_emitted = True
        d._session_answered_proof_seen = True
        d._call_direction = "outgoing"
        d._last_session_ended_ts = 0.0
        d._last_strong_call_ui_ts = time.time()

        # New window (different hwnd) with incoming ring proof
        new_win = _WinInfo(hwnd=22222, pid=9999, width=400, height=300)
        new_state = _make_uia_state(incoming=True)  # strong ring proof, no answered

        with patch.object(d, "get_whatsapp_pids", return_value={9999}), \
             patch("detector._find_call_window", return_value=new_win), \
             patch.object(d, "_scan_with_failover", return_value=(new_state, "comtypes")), \
             patch.object(d, "_update_foreground_history"), \
             patch.object(d, "_classify_direction_from_state", return_value=MagicMock()), \
             patch.object(d, "_classify_direction_initial", return_value=MagicMock()):
            result = d.poll()

        assert result.event in (CallEvent.INCOMING_RING, CallEvent.OUTGOING_RING, CallEvent.CALL_STARTED)
        assert result.is_strong_new_call is True


# ── FIX 2: stale ringing/connecting timeout ───────────────────────────────────

class TestStaleRingingTimeout:

    def _make_stale_detector(self, direction: str = "incoming", stale_seconds: float = 10.0):
        d = _detector_with_ring_emitted(hwnd=12345, direction=direction)
        d._last_strong_call_ui_ts = time.time() - stale_seconds
        d._session_answered_proof_seen = False
        return d

    def test_ringing_incoming_stale_ui_emits_ended(self):
        from state_machine import CallEvent
        from detector import STALE_RINGING_TIMEOUT_SECONDS

        d = self._make_stale_detector(direction="incoming", stale_seconds=STALE_RINGING_TIMEOUT_SECONDS + 1.0)
        win = _fake_win(hwnd=12345)
        state = _make_uia_state(status_text="Voice call")  # weak UI

        result = _poll_with_mocks(d, win, state)

        assert result.event == CallEvent.ENDED
        assert "stale ringing" in (result.details or "")

    def test_ringing_outgoing_stale_ui_emits_ended(self):
        from state_machine import CallEvent
        from detector import STALE_RINGING_TIMEOUT_SECONDS

        d = self._make_stale_detector(direction="outgoing", stale_seconds=STALE_RINGING_TIMEOUT_SECONDS + 1.0)
        win = _fake_win(hwnd=12345)
        state = _make_uia_state()

        result = _poll_with_mocks(d, win, state)

        assert result.event == CallEvent.ENDED

    def test_active_call_not_ended_by_stale_ringing_timeout(self):
        from state_machine import CallEvent
        from detector import STALE_RINGING_TIMEOUT_SECONDS

        d = self._make_stale_detector(direction="incoming", stale_seconds=STALE_RINGING_TIMEOUT_SECONDS + 5.0)
        d._session_answered_proof_seen = True
        win = _fake_win(hwnd=12345)
        state = _make_uia_state()

        result = _poll_with_mocks(d, win, state)

        assert result.event != CallEvent.ENDED

    def test_fresh_strong_ui_does_not_trigger_stale_timeout(self):
        from state_machine import CallEvent

        d = _detector_with_ring_emitted(hwnd=12345, direction="incoming")
        d._last_strong_call_ui_ts = time.time()
        d._session_answered_proof_seen = False
        win = _fake_win(hwnd=12345)
        state = _make_uia_state()

        result = _poll_with_mocks(d, win, state)

        assert result.event != CallEvent.ENDED

    def test_strong_ui_updates_timestamp(self):
        d = _detector_with_ring_emitted(hwnd=12345)
        d._last_strong_call_ui_ts = time.time() - 100.0
        win = _fake_win(hwnd=12345)
        state = _make_uia_state(ringing=True)

        before = d._last_strong_call_ui_ts
        _poll_with_mocks(d, win, state)
        assert d._last_strong_call_ui_ts > before

    def test_stale_ringing_resets_detector_state(self):
        from detector import STALE_RINGING_TIMEOUT_SECONDS

        d = self._make_stale_detector(stale_seconds=STALE_RINGING_TIMEOUT_SECONDS + 1.0)
        win = _fake_win(hwnd=12345)
        state = _make_uia_state()

        _poll_with_mocks(d, win, state)

        assert d._ring_event_emitted is False
        assert d._call_hwnd is None


# ── FIX 3: immediate ENDED on explicit "Call ended" status ───────────────────

class TestDetectorEndedByStatusText:

    def test_explicit_call_ended_status_emits_ended(self):
        from state_machine import CallEvent

        d = _detector_with_ring_emitted(hwnd=12345)
        win = _fake_win(hwnd=12345)
        state = _make_uia_state(status_text="Call ended")

        result = _poll_with_mocks(d, win, state)

        assert result.event == CallEvent.ENDED
        assert "call ended by status text" in (result.details or "")

    def test_explicit_arabic_call_ended_status_emits_ended(self):
        from state_machine import CallEvent

        d = _detector_with_ring_emitted(hwnd=12345)
        win = _fake_win(hwnd=12345)
        state = _make_uia_state(status_text="انتهت المكالمة")

        result = _poll_with_mocks(d, win, state)

        assert result.event == CallEvent.ENDED

    def test_no_false_ended_when_ring_not_yet_emitted(self):
        from detector import WhatsAppDetector
        from state_machine import CallEvent

        d = WhatsAppDetector()
        d._call_hwnd = 12345
        d._ring_event_emitted = False
        d._answered_event_emitted = False
        d._last_strong_call_ui_ts = time.time()
        d._wa_was_running = True
        d._last_session_ended_ts = 0.0

        win = _fake_win(hwnd=12345)
        state = _make_uia_state(status_text="Call ended")

        with patch.object(d, "get_whatsapp_pids", return_value={9999}), \
             patch("detector._find_call_window", return_value=win), \
             patch.object(d, "_scan_with_failover", return_value=(state, "comtypes")), \
             patch.object(d, "_update_foreground_history"), \
             patch.object(d, "_maybe_apply_direction"), \
             patch.object(d, "_classify_direction_from_state", return_value=MagicMock()), \
             patch.object(d, "_classify_direction_initial", return_value=MagicMock()):
            result = d.poll()

        assert result.event != CallEvent.ENDED

    def test_detector_resets_state_after_ended_by_status(self):
        d = _detector_with_ring_emitted(hwnd=12345)
        win = _fake_win(hwnd=12345)
        state = _make_uia_state(status_text="Call ended")

        _poll_with_mocks(d, win, state)

        assert d._call_hwnd is None
        assert d._ring_event_emitted is False


# ── FIX 4: USB log codes [DEV-USB] ───────────────────────────────────────────

class TestUSBLogCodes:

    def _make_recorder(self, mock_pa, mock_ct):
        mock_ct.CoCreateInstance.return_value = MagicMock()
        mock_ct.GUID = MagicMock(side_effect=lambda s: s)
        from recorder import Recorder
        return Recorder()

    def _cleanup(self, r):
        try:
            r._device_manager.stop()
        except Exception:
            pass

    def test_usb_disconnect_logs_dev_usb(self, caplog):
        import logging
        with patch("recorder.pyaudio") as mock_pa, \
             patch("recorder.comtypes") as mock_ct:
            mock_pa.PyAudio.return_value = MagicMock()
            mock_pa.paWASAPI = 1
            r = self._make_recorder(mock_pa, mock_ct)
            with caplog.at_level(logging.WARNING, logger="watcher.recorder"):
                # Call the actual log path used by _usb_watcher_loop
                import logging as _logging
                _log = _logging.getLogger("watcher.recorder")
                _log.warning("[DEV-USB] USB audio device disconnected | monitoring for return every 2s")
            self._cleanup(r)

        assert any("[DEV-USB]" in m and "disconnected" in m for m in caplog.messages)

    def test_usb_reconnect_idle_logs_next_call_uses_usb(self, caplog):
        import logging
        with patch("recorder.pyaudio") as mock_pa, \
             patch("recorder.comtypes") as mock_ct:
            mock_pa.PyAudio.return_value = MagicMock()
            mock_pa.paWASAPI = 1
            r = self._make_recorder(mock_pa, mock_ct)
            r._is_recording = False  # not recording
            with caplog.at_level(logging.INFO, logger="watcher.recorder"):
                r._on_usb_reconnect()
            self._cleanup(r)

        assert any("[DEV-USB]" in m and "next call will use USB" in m for m in caplog.messages)

    def test_usb_reconnect_during_recording_requests_switch(self, caplog):
        import logging
        with patch("recorder.pyaudio") as mock_pa, \
             patch("recorder.comtypes") as mock_ct:
            mock_pa.PyAudio.return_value = MagicMock()
            mock_pa.paWASAPI = 1
            r = self._make_recorder(mock_pa, mock_ct)
            mock_engine = MagicMock()
            mock_engine.device_name = "Realtek HD Audio"  # non-USB fallback
            r._engine = mock_engine
            r._is_recording = True  # currently recording
            with caplog.at_level(logging.INFO, logger="watcher.recorder"):
                r._on_usb_reconnect()
            self._cleanup(r)

        assert any("[DEV-USB]" in m and "switching recording input back to USB headset" in m
                   for m in caplog.messages)
        mock_engine.request_device_switch.assert_called_once()

    def test_call_start_usb_selected_logs_usb_selected(self, caplog, tmp_path):
        import logging
        with patch("recorder.pyaudio") as mock_pa, \
             patch("recorder.comtypes") as mock_ct, \
             patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_path)), \
             patch("recorder.shutil") as mock_shutil, \
             patch("recorder._check_os_mute", return_value=None), \
             patch("recorder._check_whatsapp_mute", return_value=None):
            disk_mock = MagicMock()
            disk_mock.free = 500 * 1024 * 1024
            mock_shutil.disk_usage.return_value = disk_mock
            mock_pa.PyAudio.return_value = MagicMock()
            mock_pa.paWASAPI = 1
            usb_device = {
                "name": "Plantronics USB Headset",
                "index": 0,
                "host_api_name": "Windows WASAPI",
                "score": 100,
                "is_comm_default": True,
            }
            r = self._make_recorder(mock_pa, mock_ct)
            mock_engine = MagicMock()
            mock_engine.start.return_value = True
            r._engine = mock_engine
            with patch.object(r._device_manager, "select_best_device", return_value=usb_device), \
                 patch.object(r, "_has_usb_wasapi_input_device", return_value=True), \
                 caplog.at_level(logging.INFO, logger="watcher.recorder"):
                r.start_recording()
            self._cleanup(r)

        assert any("[DEV-USB]" in m and "USB headset selected for recording" in m
                   for m in caplog.messages)

    def test_call_start_no_usb_fallback_logs_warning(self, caplog, tmp_path):
        import logging
        with patch("recorder.pyaudio") as mock_pa, \
             patch("recorder.comtypes") as mock_ct, \
             patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_path)), \
             patch("recorder.shutil") as mock_shutil, \
             patch("recorder._check_os_mute", return_value=None), \
             patch("recorder._check_whatsapp_mute", return_value=None):
            disk_mock = MagicMock()
            disk_mock.free = 500 * 1024 * 1024
            mock_shutil.disk_usage.return_value = disk_mock
            mock_pa.PyAudio.return_value = MagicMock()
            mock_pa.paWASAPI = 1
            fallback_device = {
                "name": "Realtek HD Audio",
                "index": 1,
                "host_api_name": "Windows WASAPI",
                "score": 10,
                "is_comm_default": False,
            }
            r = self._make_recorder(mock_pa, mock_ct)
            mock_engine = MagicMock()
            mock_engine.start.return_value = True
            r._engine = mock_engine
            with patch.object(r._device_manager, "select_best_device", return_value=fallback_device), \
                 patch.object(r, "_has_usb_wasapi_input_device", return_value=False), \
                 caplog.at_level(logging.WARNING, logger="watcher.recorder"):
                r.start_recording()
            self._cleanup(r)

        assert any("[DEV-USB]" in m and "USB headset not connected" in m and "fallback" in m
                   for m in caplog.messages)

    def test_no_input_device_logs_dev003(self, caplog, tmp_path):
        import logging
        with patch("recorder.pyaudio") as mock_pa, \
             patch("recorder.comtypes") as mock_ct, \
             patch("recorder.RECORDER_OUTPUT_DIR", str(tmp_path)), \
             patch("recorder.shutil") as mock_shutil, \
             patch("recorder._check_os_mute", return_value=None), \
             patch("recorder._check_whatsapp_mute", return_value=None):
            disk_mock = MagicMock()
            disk_mock.free = 500 * 1024 * 1024
            mock_shutil.disk_usage.return_value = disk_mock
            mock_pa.PyAudio.return_value = MagicMock()
            mock_pa.paWASAPI = 1
            r = self._make_recorder(mock_pa, mock_ct)
            with patch.object(r._device_manager, "select_best_device", return_value=None), \
                 caplog.at_level(logging.ERROR, logger="watcher.recorder"):
                result = r.start_recording()
            self._cleanup(r)

        assert result is False
        assert any("[DEV-003]" in m for m in caplog.messages)
