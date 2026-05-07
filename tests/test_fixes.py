"""
Tests for the targeted fix session:
  FIX 2 — DeviceManager._pa lock safety for reinit_pyaudio
  FIX 3 — uploader [UPL-*] log codes + log.exception
  FIX 4 — main._finalize_call error_details when no recording resolves
"""
import logging
import sys
import threading
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── FIX 2: DeviceManager reinit race safety ───────────────────────────────────

class TestDeviceManagerReinitSafety:
    def _make_dm(self, mock_pa, mock_ct):
        mock_ct.CoCreateInstance.return_value = MagicMock()
        mock_ct.GUID = MagicMock(side_effect=lambda s: s)
        from recorder import DeviceManager
        return DeviceManager()

    def test_list_input_devices_handles_none_pa(self):
        """list_input_devices() returns [] without raising when _pa is None."""
        with patch("recorder.pyaudio") as mock_pa, \
             patch("recorder.comtypes") as mock_ct:
            mock_pa.PyAudio.return_value = MagicMock()
            mock_pa.paWASAPI = 1
            dm = self._make_dm(mock_pa, mock_ct)
            dm._pa = None  # simulate window between terminate and reinit
            result = dm.list_input_devices()
            dm.stop()
        assert result == []

    def test_get_default_comm_device_handles_none_pa(self):
        """get_default_comm_device_index() returns None without raising when _pa is None."""
        with patch("recorder.pyaudio") as mock_pa, \
             patch("recorder.comtypes") as mock_ct:
            mock_pa.PyAudio.return_value = MagicMock()
            mock_pa.paWASAPI = 1
            dm = self._make_dm(mock_pa, mock_ct)
            dm._pa = None
            result = dm.get_default_comm_device_index()
            dm.stop()
        assert result is None

    def test_reinit_pyaudio_terminates_old_and_creates_new(self):
        """reinit_pyaudio() terminates the old pa instance and installs a fresh one."""
        with patch("recorder.pyaudio") as mock_pa, \
             patch("recorder.comtypes") as mock_ct:
            old_pa = MagicMock()
            new_pa = MagicMock()
            mock_pa.PyAudio.side_effect = [old_pa, new_pa]
            mock_pa.paWASAPI = 1
            dm = self._make_dm(mock_pa, mock_ct)
            assert dm._pa is old_pa
            dm.reinit_pyaudio()
            assert dm._pa is new_pa
            old_pa.terminate.assert_called_once()
            dm.stop()

    def test_reinit_pyaudio_concurrent_with_list_does_not_raise(self):
        """Concurrent reinit + list_input_devices must not raise in either thread."""
        from recorder import DeviceManager

        with patch("recorder.pyaudio") as mock_pa, \
             patch("recorder.comtypes") as mock_ct:
            mock_ct.CoCreateInstance.return_value = MagicMock()
            mock_ct.GUID = MagicMock(side_effect=lambda s: s)

            call_count = [0]

            def make_pa():
                inst = MagicMock()
                inst.get_device_count.return_value = 0
                inst.get_host_api_info_by_type.side_effect = OSError("no wasapi")
                return inst

            def pa_factory():
                call_count[0] += 1
                return make_pa()

            mock_pa.PyAudio.side_effect = pa_factory
            mock_pa.paWASAPI = 1

            dm = DeviceManager()
            errors = []

            def run_reinit():
                try:
                    dm.reinit_pyaudio()
                except Exception as e:
                    errors.append(("reinit", e))

            def run_list():
                try:
                    dm.list_input_devices()
                except Exception as e:
                    errors.append(("list", e))

            t1 = threading.Thread(target=run_reinit)
            t2 = threading.Thread(target=run_list)
            t1.start(); t2.start()
            t1.join(timeout=5); t2.join(timeout=5)
            dm.stop()

        assert not errors, f"Thread errors: {errors}"


# ── FIX 3: uploader [UPL-*] log codes ────────────────────────────────────────

class TestUploaderLogCodes:
    def test_upl001_logged_when_process_pending_raises(self, caplog, tmp_path):
        """[UPL-001] appears in logs when process_pending_uploads() inner loop raises."""
        from uploader import UploadManager

        mock_storage = MagicMock()
        mock_storage.get_pending_uploads.return_value = [
            {
                "id": 1,
                "local_path": "/x/y.wav",
                "dest_rel_path": "a/b/y.wav",
                "retries": 0,
                "max_retries": 3,
            }
        ]
        mgr = UploadManager(mock_storage)
        mgr._process_one = MagicMock(side_effect=RuntimeError("boom"))

        with patch("uploader.UPLOAD_ENABLED", True), \
             patch("uploader.UPLOAD_ROOT_DIR", str(tmp_path / "share")), \
             caplog.at_level(logging.ERROR, logger="watcher.uploader"):
            mgr.process_pending_uploads()

        msgs = [r.message for r in caplog.records]
        assert any("[UPL-001]" in m for m in msgs), (
            f"[UPL-001] not found; got: {msgs}"
        )

    def test_upl002_logged_on_copy_failure(self, caplog, tmp_path):
        """[UPL-002] appears in logs when shutil.copy2 raises."""
        from uploader import UploadManager

        src = tmp_path / "call.wav"
        src.write_bytes(b"RIFF" + b"\x00" * 100)

        mock_storage = MagicMock()
        mgr = UploadManager(mock_storage)

        with patch("uploader.UPLOAD_ENABLED", True), \
             patch("uploader.UPLOAD_ROOT_DIR", str(tmp_path / "remote")), \
             patch("uploader.UPLOAD_RETRY_COUNT", 1), \
             patch("uploader.UPLOAD_RETRY_DELAY_SECONDS", 0.0), \
             patch("uploader.UPLOAD_SHARE_AUTH_ENABLED", False), \
             patch("shutil.copy2", side_effect=IOError("disk full")), \
             caplog.at_level(logging.ERROR, logger="watcher.uploader"):
            mgr._process_one(
                local_path=str(src),
                dest_rel_path="a/b/call.wav",
                retries_so_far=0,
            )

        msgs = [r.message for r in caplog.records]
        assert any("[UPL-002]" in m for m in msgs), (
            f"[UPL-002] not found; got: {msgs}"
        )

    def test_upl002_uses_log_exception(self, tmp_path):
        """The [UPL-002] handler must call log.exception (captures traceback)."""
        from uploader import UploadManager

        src = tmp_path / "call.wav"
        src.write_bytes(b"RIFF" + b"\x00" * 100)
        mock_storage = MagicMock()
        mgr = UploadManager(mock_storage)

        exception_calls = []

        class _CapturingLogger(logging.Logger):
            def exception(self_inner, msg, *args, **kw):
                exception_calls.append(msg % args if args else msg)
                super().exception(msg, *args, **kw)

        import uploader as _uploader_mod
        original_log = _uploader_mod.log
        _uploader_mod.log = _CapturingLogger("watcher.uploader.test")

        try:
            with patch("uploader.UPLOAD_ENABLED", True), \
                 patch("uploader.UPLOAD_ROOT_DIR", str(tmp_path / "remote")), \
                 patch("uploader.UPLOAD_RETRY_COUNT", 1), \
                 patch("uploader.UPLOAD_RETRY_DELAY_SECONDS", 0.0), \
                 patch("uploader.UPLOAD_SHARE_AUTH_ENABLED", False), \
                 patch("shutil.copy2", side_effect=IOError("disk full")):
                mgr._process_one(
                    local_path=str(src),
                    dest_rel_path="a/b/call.wav",
                    retries_so_far=0,
                )
        finally:
            _uploader_mod.log = original_log

        assert any("[UPL-002]" in c for c in exception_calls), (
            f"log.exception not called with [UPL-002]: {exception_calls}"
        )

    def test_upl004_logged_on_missing_file(self, caplog):
        """[UPL-004] appears in logs when local file does not exist."""
        from uploader import UploadManager

        mock_storage = MagicMock()
        mgr = UploadManager(mock_storage)

        with caplog.at_level(logging.ERROR, logger="watcher.uploader"):
            result, msg, _ = mgr._process_one(
                local_path="/definitely/does/not/exist.wav",
                dest_rel_path="a/b/exist.wav",
                retries_so_far=0,
            )

        assert result is None
        msgs = [r.message for r in caplog.records]
        assert any("[UPL-004]" in m for m in msgs), (
            f"[UPL-004] not found; got: {msgs}"
        )


# ── FIX 4: main._finalize_call error_details ─────────────────────────────────

def _make_session(**kwargs):
    class _Session:
        started_at = None
        ended_at = None
        status = "ended"
        direction = "incoming"
        caller_number = "+1234"
        recording_path = None
        uploaded_path = None
        error_details = None
        machine_ip = "-"
        pc_user = "test"
        machine_name = "PC"

    s = _Session()
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


class TestFinalizeCallErrorDetails:
    def test_error_details_set_when_no_files_resolve(self):
        """_finalize_call sets error_details when resolve_final_files returns []."""
        from main import _finalize_call

        session = _make_session()
        mock_recorder = MagicMock()
        mock_recorder.resolve_final_files.return_value = []
        mock_recorder.get_recording_metadata.return_value = {
            "recording_issues": "[REC-006]",
        }
        mock_storage = MagicMock()
        mock_storage.save_call.return_value = 1
        mock_reporter = MagicMock()
        mock_uploader = MagicMock()

        _finalize_call(
            session,
            mock_recorder,
            [MagicMock()],
            mock_storage,
            mock_reporter,
            mock_uploader,
        )

        assert session.error_details is not None, "error_details must be set"
        assert "Recording finalization failed" in session.error_details

    def test_error_details_appended_to_existing(self):
        """_finalize_call appends to existing error_details (does not overwrite)."""
        from main import _finalize_call

        session = _make_session(error_details="Recorder failed to start")
        mock_recorder = MagicMock()
        mock_recorder.resolve_final_files.return_value = []
        mock_recorder.get_recording_metadata.return_value = {}
        mock_storage = MagicMock()
        mock_storage.save_call.return_value = 1
        mock_reporter = MagicMock()
        mock_uploader = MagicMock()

        _finalize_call(
            session,
            mock_recorder,
            [MagicMock()],
            mock_storage,
            mock_reporter,
            mock_uploader,
        )

        assert "Recorder failed to start" in session.error_details
        assert "Recording finalization failed" in session.error_details

    def test_error_details_not_added_when_files_resolve(self, tmp_path):
        """_finalize_call must NOT touch error_details when a recording file resolves."""
        from main import _finalize_call

        wav = tmp_path / "call.wav"
        with wave.open(str(wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(b"\x00\x00" * 44100)

        session = _make_session()
        mock_recorder = MagicMock()
        mock_recorder.resolve_final_files.return_value = [str(wav)]
        mock_storage = MagicMock()
        mock_storage.save_call.return_value = 1
        mock_reporter = MagicMock()
        mock_uploader = MagicMock()
        mock_uploader.upload_for_session.return_value = None

        with patch("main.rename_recording_for_session", return_value=str(wav)):
            _finalize_call(
                session,
                mock_recorder,
                [MagicMock()],
                mock_storage,
                mock_reporter,
                mock_uploader,
            )

        assert session.error_details is None, (
            f"error_details should remain None, got: {session.error_details!r}"
        )
