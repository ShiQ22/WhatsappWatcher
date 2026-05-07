"""
Tests for DeviceManager in recorder.py.
All audio hardware is mocked — these tests must pass without a real soundcard.
"""
import logging
import threading
import time

import pytest


# ── Static scoring tests (no fixtures required) ────────────────────────────

class TestScoring:
    def test_score_usb_headset(self):
        from recorder import DeviceManager
        score = DeviceManager.score_device("Plantronics USB Headset", "WASAPI", True)
        assert score >= 7

    def test_score_builtin_mic(self):
        from recorder import DeviceManager
        score = DeviceManager.score_device("Realtek HD Audio (Built-in)", "MME", False)
        assert score < 0

    def test_highest_scored_device_wins(self):
        from recorder import DeviceManager
        usb = DeviceManager.score_device("Plantronics USB Headset", "Windows WASAPI", True)
        builtin = DeviceManager.score_device("Realtek HD Audio (Built-in)", "Windows WASAPI", False)
        assert usb > builtin

    def test_score_handles_empty_strings(self):
        from recorder import DeviceManager
        # Should not raise on empty device name / host_api.
        assert DeviceManager.score_device("", "", False) == 0

    def test_score_bluetooth_headset(self):
        from recorder import DeviceManager
        # Bluetooth + headset wording → at least bluetooth bonus.
        score = DeviceManager.score_device("Bluetooth Headset", "WASAPI", False)
        assert score >= 3  # bluetooth(1) + headset(2)


# ── Selection tests ────────────────────────────────────────────────────────

class TestDeviceSelection:
    def test_selects_usb_headset_as_first_choice(self, mock_pyaudio):
        """When comm-default is USB headset, it should win."""
        from recorder import DeviceManager
        dm = DeviceManager()
        try:
            best = dm.select_best_device()
            assert best is not None
            assert best["index"] == 0
            assert "Plantronics" in best["name"]
            assert best["is_comm_default"] is True
        finally:
            dm.stop()

    def test_falls_back_to_scoring_when_comm_query_fails(self, mock_pyaudio_no_comm):
        """When comm-default query raises, scoring should still pick the USB headset."""
        from recorder import DeviceManager
        dm = DeviceManager()
        try:
            best = dm.select_best_device()
            assert best is not None
            assert "Plantronics" in best["name"]
            assert best["index"] == 0
            # No comm device known.
            assert best["is_comm_default"] is False
        finally:
            dm.stop()

    def test_no_devices_logs_dev003(self, mock_pyaudio_no_devices, caplog):
        """When zero input devices, select_best_device returns None and logs [DEV-003]."""
        from recorder import DeviceManager
        dm = DeviceManager()
        try:
            with caplog.at_level(logging.ERROR, logger="watcher.recorder"):
                result = dm.select_best_device()
            assert result is None
            assert any("[DEV-003]" in rec.message for rec in caplog.records), (
                f"expected [DEV-003] in logs; got: {[r.message for r in caplog.records]}"
            )
        finally:
            dm.stop()

    def test_list_input_devices_skips_output_only_devices(self, mock_pyaudio):
        """Devices with maxInputChannels == 0 must be filtered out."""
        from recorder import DeviceManager
        _, pa_instance, _ = mock_pyaudio
        # Override: device 1 is now output-only.
        pa_instance.get_device_info_by_index.side_effect = lambda i: [
            {"name": "Plantronics USB Headset", "maxInputChannels": 1,
             "defaultSampleRate": 44100.0, "hostApi": 0, "index": 0},
            {"name": "Realtek Speakers", "maxInputChannels": 0,
             "defaultSampleRate": 44100.0, "hostApi": 0, "index": 1},
        ][i]
        dm = DeviceManager()
        try:
            devices = dm.list_input_devices()
            assert len(devices) == 1
            assert devices[0]["name"] == "Plantronics USB Headset"
        finally:
            dm.stop()


# ── Notification / polling tests ───────────────────────────────────────────

class TestDeviceChange:
    def test_notify_device_change_invokes_callback(self, mock_pyaudio):
        from recorder import DeviceManager
        dm = DeviceManager()
        try:
            calls = []
            # Register without going through COM (avoids touching comtypes).
            dm._change_callback = lambda: calls.append("x")
            dm.notify_device_change("unit-test")
            assert calls == ["x"]
        finally:
            dm.stop()

    def test_device_change_triggers_reselection(self, mock_pyaudio, mock_com_ok):
        """Simulating an IMMNotificationClient callback fires the registered handler."""
        from recorder import DeviceManager
        dm = DeviceManager()
        try:
            calls = []
            dm.register_change_callback(lambda: calls.append("changed"))
            assert dm._use_polling_fallback is False
            dm.notify_device_change("simulated")
            assert calls == ["changed"]
        finally:
            dm.stop()

    def test_polling_fallback_activates_when_com_fails(self, mock_pyaudio, mock_com_fail, caplog):
        """COM registration failure → DEV-004 logged and polling thread started."""
        from recorder import DeviceManager
        dm = DeviceManager()
        try:
            with caplog.at_level(logging.WARNING, logger="watcher.recorder"):
                dm.register_change_callback(lambda: None)
            assert dm._use_polling_fallback is True
            assert dm._polling_thread is not None
            assert dm._polling_thread.is_alive()
            assert any("[DEV-004]" in rec.message for rec in caplog.records), (
                f"expected [DEV-004] in logs; got: {[r.message for r in caplog.records]}"
            )
        finally:
            dm.stop()
            # Thread must exit cleanly.
            if dm._polling_thread is not None:
                assert not dm._polling_thread.is_alive()

    def test_callback_exception_does_not_propagate(self, mock_pyaudio, caplog):
        """A raising callback must be logged, not propagated to caller."""
        from recorder import DeviceManager
        dm = DeviceManager()
        try:
            def boom():
                raise RuntimeError("callback boom")
            dm._change_callback = boom
            with caplog.at_level(logging.ERROR, logger="watcher.recorder"):
                dm.notify_device_change("test")  # must not raise
            assert any("[DEV-CHG]" in rec.message for rec in caplog.records)
        finally:
            dm.stop()


# ── Lifecycle ──────────────────────────────────────────────────────────────

class TestLifecycle:
    def test_stop_terminates_pyaudio(self, mock_pyaudio):
        from recorder import DeviceManager
        _, pa_instance, _ = mock_pyaudio
        dm = DeviceManager()
        dm.stop()
        pa_instance.terminate.assert_called_once()

    def test_stop_joins_polling_thread(self, mock_pyaudio, mock_com_fail):
        from recorder import DeviceManager
        dm = DeviceManager()
        dm.register_change_callback(lambda: None)
        thread = dm._polling_thread
        assert thread is not None and thread.is_alive()
        dm.stop()
        # Thread must exit within the join timeout.
        thread.join(timeout=2.0)
        assert not thread.is_alive()
