"""
Tests for Bug 1: duplicate upload race / false upload failures.

Run with:  python -m pytest tests/test_upload_race.py -v
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal in-memory Storage stand-in so we can import Storage without
# touching production config files.
# ---------------------------------------------------------------------------

def _make_storage(tmp_db: str):
    """Return a real Storage instance backed by a temp SQLite file."""
    import sys, importlib

    # Point config at temp paths so Storage.__init__ does not fail.
    with patch.dict(
        "os.environ",
        {
            "WATCHER_DB_PATH": tmp_db,
        },
    ):
        # Re-import with patched config constants.
        import sys
        # Patch the config-level constants storage.py reads at module level.
        import storage as _storage_mod
        from storage import Storage
        orig_db = _storage_mod.DB_PATH
        _storage_mod.DB_PATH = Path(tmp_db)
        try:
            s = Storage.__new__(Storage)
            s._lock = threading.Lock()
            s._central_lock = threading.Lock()
            s.local_conn = sqlite3.connect(
                tmp_db, check_same_thread=False, isolation_level=None
            )
            s.local_conn.execute("PRAGMA journal_mode=WAL")
            s.local_conn.execute("PRAGMA busy_timeout=5000")
            s.local_conn.row_factory = sqlite3.Row
            s._create_local_tables()
            s._migrate_local_tables()
            s._cached_identity = ("testuser", "testmachine", "127.0.0.1")
            s._closed = False
            s._central_engine = None
            return s
        finally:
            _storage_mod.DB_PATH = orig_db


class TestUploadRace(unittest.TestCase):
    """Acceptance tests for Bug 1 fixes."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.storage = _make_storage(self._tmp.name)

    def tearDown(self):
        try:
            self.storage.close()
        except Exception:
            pass
        try:
            os.unlink(self._tmp.name)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 1. Unique partial filenames per upload attempt
    # ------------------------------------------------------------------

    def test_unique_partial_names(self):
        """Each upload attempt must produce a distinct .partial filename."""
        from uploader import UploadManager
        mgr = UploadManager(self.storage)

        with tempfile.TemporaryDirectory() as root:
            with tempfile.NamedTemporaryFile(
                dir=root, suffix=".mp3", delete=False
            ) as f:
                f.write(b"x" * 50000)
                src = f.name

            dest_dir = Path(root) / "remote"
            dest_dir.mkdir()
            dest_rel = "test.mp3"

            # Patch UPLOAD_ROOT_DIR to the temp dir and VERIFY=False
            with (
                patch("uploader.UPLOAD_ROOT_DIR", root),
                patch("uploader.UPLOAD_VERIFY_COPY", False),
                patch("uploader.DELETE_LOCAL_AFTER_SUCCESS", False),
                patch("uploader.UPLOAD_RETRY_COUNT", 3),
                patch("uploader.UPLOAD_RETRY_DELAY_SECONDS", 0),
                patch("uploader.UPLOAD_ENABLED", True),
            ):
                partials_seen: list[str] = []
                orig_copy = __import__("shutil").copy2

                def capture_copy(s, d):
                    partials_seen.append(Path(d).name)
                    return orig_copy(s, d)

                with patch("shutil.copy2", side_effect=capture_copy):
                    uid = self.storage.enqueue_pending_upload(
                        call_local_id=None,
                        local_path=src,
                        dest_rel_path=dest_rel,
                        pc_user="u",
                        machine_name="m",
                        machine_ip="i",
                        max_retries=3,
                        initial_status="pending",
                    )
                    # Simulate two separate process_pending_uploads calls
                    # (first one fails, second succeeds).
                    self.storage.mark_upload_failure(uid, 1, "test error")

                    mgr.process_pending_uploads()

            # All partial filenames must embed the upload_id
            self.assertTrue(
                all(f".upload-{uid}." in p for p in partials_seen),
                f"Expected upload_id in partial names; got {partials_seen}",
            )
            # If multiple attempts occurred they must be distinct
            self.assertEqual(
                len(partials_seen), len(set(partials_seen)),
                f"Duplicate partial names found: {partials_seen}",
            )

    # ------------------------------------------------------------------
    # 2. Claim prevents background scan from processing the same row
    # ------------------------------------------------------------------

    def test_claim_is_exclusive(self):
        """Two concurrent callers racing to claim the same row — only one wins."""
        uid = self.storage.enqueue_pending_upload(
            call_local_id=None,
            local_path="/fake/file.mp3",
            dest_rel_path="fake/file.mp3",
            pc_user="u",
            machine_name="m",
            machine_ip="i",
            max_retries=3,
            initial_status="pending",
        )
        results = []

        def try_claim():
            results.append(self.storage.claim_upload_for_processing(uid))

        t1 = threading.Thread(target=try_claim)
        t2 = threading.Thread(target=try_claim)
        t1.start(); t2.start()
        t1.join(); t2.join()

        # Exactly one True and one False
        self.assertEqual(sorted(results), [False, True])

    # ------------------------------------------------------------------
    # 3. enqueue with initial_status='processing' never visible to pending scan
    # ------------------------------------------------------------------

    def test_processing_row_not_returned_by_get_pending(self):
        uid = self.storage.enqueue_pending_upload(
            call_local_id=None,
            local_path="/fake/file.mp3",
            dest_rel_path="fake/file.mp3",
            pc_user="u",
            machine_name="m",
            machine_ip="i",
            max_retries=3,
            initial_status="processing",
        )
        rows = self.storage.get_pending_uploads()
        ids = [int(r["id"]) for r in rows]
        self.assertNotIn(uid, ids, "processing row must not appear in get_pending_uploads()")

    # ------------------------------------------------------------------
    # 4. mark_upload_success / failure / retry clear processing_at
    # ------------------------------------------------------------------

    def _get_row(self, uid: int):
        with self.storage._lock:
            return self.storage.local_conn.execute(
                "SELECT * FROM pending_uploads WHERE id=?", (uid,)
            ).fetchone()

    def test_mark_success_clears_processing_at(self):
        uid = self.storage.enqueue_pending_upload(
            call_local_id=None,
            local_path="/f.mp3",
            dest_rel_path="f.mp3",
            pc_user="u",
            machine_name="m",
            machine_ip="i",
            max_retries=3,
            initial_status="processing",
        )
        self.storage.mark_upload_success(uid, "/remote/f.mp3")
        row = self._get_row(uid)
        self.assertIsNone(row["processing_at"])
        self.assertEqual(row["status"], "uploaded")

    def test_mark_failure_clears_processing_at(self):
        uid = self.storage.enqueue_pending_upload(
            call_local_id=None,
            local_path="/f.mp3",
            dest_rel_path="f.mp3",
            pc_user="u",
            machine_name="m",
            machine_ip="i",
            max_retries=3,
            initial_status="processing",
        )
        self.storage.mark_upload_failure(uid, 1, "some error")
        row = self._get_row(uid)
        self.assertIsNone(row["processing_at"])

    def test_mark_pending_retry_clears_processing_at(self):
        uid = self.storage.enqueue_pending_upload(
            call_local_id=None,
            local_path="/f.mp3",
            dest_rel_path="f.mp3",
            pc_user="u",
            machine_name="m",
            machine_ip="i",
            max_retries=3,
            initial_status="processing",
        )
        self.storage.mark_upload_pending_retry(uid)
        row = self._get_row(uid)
        self.assertIsNone(row["processing_at"])

    # ------------------------------------------------------------------
    # 5. reset_stale_processing_uploads resets only stale rows
    # ------------------------------------------------------------------

    def test_reset_stale_processing(self):
        """Rows stuck in processing > stale_after_seconds should be reset to pending."""
        uid = self.storage.enqueue_pending_upload(
            call_local_id=None,
            local_path="/f.mp3",
            dest_rel_path="f.mp3",
            pc_user="u",
            machine_name="m",
            machine_ip="i",
            max_retries=3,
            initial_status="processing",
        )

        # Manually backdate processing_at to simulate a stale row
        with self.storage._lock:
            self.storage.local_conn.execute(
                "UPDATE pending_uploads SET processing_at=datetime('now','-1000 seconds') WHERE id=?",
                (uid,),
            )
            self.storage.local_conn.commit()

        reset = self.storage.reset_stale_processing_uploads(stale_after_seconds=900)
        self.assertEqual(reset, 1)

        row = self._get_row(uid)
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["processing_at"])

    def test_reset_stale_does_not_reset_fresh_processing(self):
        """Fresh processing rows (within stale window) must not be reset."""
        uid = self.storage.enqueue_pending_upload(
            call_local_id=None,
            local_path="/f.mp3",
            dest_rel_path="f.mp3",
            pc_user="u",
            machine_name="m",
            machine_ip="i",
            max_retries=3,
            initial_status="processing",
        )
        reset = self.storage.reset_stale_processing_uploads(stale_after_seconds=900)
        self.assertEqual(reset, 0)
        row = self._get_row(uid)
        self.assertEqual(row["status"], "processing")

    # ------------------------------------------------------------------
    # 6. background pending upload updates call uploaded_path
    # ------------------------------------------------------------------

    def test_pending_upload_updates_call_uploaded_path(self):
        """
        When process_pending_uploads() uploads a row with call_local_id,
        calls.uploaded_path must be updated (fix for uploaded_path stays NULL).
        """
        from uploader import UploadManager

        # Insert a fake call row
        call_id = self.storage.save_call(
            SimpleNamespace(
                started_at=None,
                ended_at=None,
                duration_seconds=lambda: 60,
                status="ended",
                direction="incoming",
                pc_user="u",
                machine_name="m",
                machine_ip="i",
                caller_number="123",
                recording_path=None,
                uploaded_path=None,
                error_details="Recorder failed to start",
            )
        )

        with tempfile.TemporaryDirectory() as root:
            with tempfile.NamedTemporaryFile(
                dir=root, suffix=".mp3", delete=False
            ) as f:
                f.write(b"y" * 50000)
                src = f.name

            uid = self.storage.enqueue_pending_upload(
                call_local_id=call_id,
                local_path=src,
                dest_rel_path="test.mp3",
                pc_user="u",
                machine_name="m",
                machine_ip="i",
                max_retries=3,
                initial_status="pending",
            )

            with (
                patch("uploader.UPLOAD_ROOT_DIR", root),
                patch("uploader.UPLOAD_VERIFY_COPY", False),
                patch("uploader.DELETE_LOCAL_AFTER_SUCCESS", False),
                patch("uploader.UPLOAD_RETRY_COUNT", 3),
                patch("uploader.UPLOAD_RETRY_DELAY_SECONDS", 0),
                patch("uploader.UPLOAD_ENABLED", True),
                patch("uploader.UPLOAD_SHARE_AUTH_ENABLED", False),
            ):
                mgr = UploadManager(self.storage)
                mgr.process_pending_uploads()

        # uploaded_path on the call row must now be populated
        with self.storage._lock:
            call_row = self.storage.local_conn.execute(
                "SELECT * FROM calls WHERE id=?", (call_id,)
            ).fetchone()
        self.assertIsNotNone(
            call_row["uploaded_path"],
            "calls.uploaded_path must be set after background pending upload",
        )

    # ------------------------------------------------------------------
    # 7. Short rejected recordings do not enqueue upload
    # ------------------------------------------------------------------

    def test_short_rejected_recording_not_enqueued(self):
        """
        Validation-rejected calls (recording_path=NULL, error has RH-VAL)
        must not appear in pending_uploads.
        """
        call_id = self.storage.save_call(
            SimpleNamespace(
                started_at=None,
                ended_at=None,
                duration_seconds=lambda: 1,
                status="ended",
                direction="incoming",
                pc_user="u",
                machine_name="m",
                machine_ip="i",
                caller_number="123",
                recording_path=None,
                uploaded_path=None,
                error_details="[RH-VAL] output too short",
            )
        )
        rows = self.storage.get_pending_uploads()
        self.assertEqual(len(rows), 0, "No upload row should be created for a rejected recording")


class TestNoRecordingErrorDetails(unittest.TestCase):
    """Acceptance tests for Bug 3 — long call with NULL error_details."""

    def _make_session(self, dur: int, error: str | None = None):
        s = SimpleNamespace()
        s.started_at = None
        s.ended_at = None
        s.duration_seconds = lambda: dur
        s.status = "ended"
        s.direction = "incoming"
        s.pc_user = "u"
        s.machine_name = "m"
        s.machine_ip = "i"
        s.caller_number = "123"
        s.recording_path = None
        s.uploaded_path = None
        s.error_details = error
        return s

    def test_no_recording_always_has_error_details(self):
        """Any finalized call with no recording must have non-empty error_details."""
        import sys, importlib
        # We test _finalize_no_recording directly by inspecting what it sets on session.
        # Import main without running run().
        from main import _finalize_no_recording

        storage_mock = MagicMock()
        storage_mock.save_call.return_value = 1
        reporter_mock = MagicMock()

        snap = self._make_session(dur=5, error=None)
        _finalize_no_recording(snap, storage_mock, reporter_mock)

        self.assertIsNotNone(snap.error_details)
        self.assertNotEqual(snap.error_details.strip(), "")

    def test_short_call_no_error_log(self):
        """
        A sub-threshold call (< MIN_VALID_RECORDING_SECONDS) with no recording
        must still have error_details set and must NOT raise.
        """
        from main import _finalize_no_recording

        storage_mock = MagicMock()
        storage_mock.save_call.return_value = 1
        reporter_mock = MagicMock()

        snap = self._make_session(dur=1, error=None)
        _finalize_no_recording(snap, storage_mock, reporter_mock)

        self.assertIsNotNone(snap.error_details)

    def test_existing_error_details_preserved(self):
        """If error_details is already set, it must not be overwritten."""
        from main import _finalize_no_recording

        storage_mock = MagicMock()
        storage_mock.save_call.return_value = 1
        reporter_mock = MagicMock()

        snap = self._make_session(dur=200, error="Recorder failed to start")
        _finalize_no_recording(snap, storage_mock, reporter_mock)

        self.assertIn("Recorder failed to start", snap.error_details)


if __name__ == "__main__":
    unittest.main()
