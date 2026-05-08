from __future__ import annotations

"""WhatsApp call watcher — main loop."""

import copy
import itertools
import json
import logging
import os
import queue
import threading
import time
import traceback
from contextlib import suppress
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
import sys
import ctypes
from pathlib import Path
from typing import Optional, Sequence


from config import (
    ALLOWED_MEDIA_EXTENSIONS,
    CENTRAL_SYNC_INTERVAL_SECONDS,
    CONFIG_LOAD_ERROR,
    DATA_DIR,
    LOCAL_CLEANUP_INTERVAL_SECONDS,
    LOG_BACKUP_COUNT,
    LOG_DIR,
    LOG_LEVEL,
    LOG_MAX_BYTES,
    POLL_INTERVAL_SECONDS,
    STARTUP_PENDING_UPLOAD_SCAN,
    UPLOAD_ENABLED,
    UPLOAD_RETRY_COUNT,
)
from detector import WhatsAppDetector
from recorder import Recorder
from report import DailyCallReporter, rename_recording_for_session
from state_machine import CallEvent, CallState, StateMachine
from storage import Storage
from uploader import UploadManager

LOG_DIR.mkdir(parents=True, exist_ok=True)
log_path = LOG_DIR / "watcher.log"

root_logger = logging.getLogger()
root_logger.setLevel(logging.DEBUG)
root_logger.handlers.clear()

formatter = logging.Formatter("%(asctime)s  %(levelname)-8s  [%(filename)s:%(lineno)d]  %(name)s  %(message)s")

file_handler = RotatingFileHandler(
    log_path,
    maxBytes=LOG_MAX_BYTES,
    backupCount=LOG_BACKUP_COUNT,
    encoding="utf-8",
)
file_handler.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
file_handler.setFormatter(formatter)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)

root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

log = logging.getLogger("watcher")


_single_instance_mutex = None


def _is_unc_path(path: Path) -> bool:
    try:
        return str(path).startswith("\\\\")
    except Exception:
        return False


def _ensure_not_running_from_network_share() -> None:
    exe_path = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
    if _is_unc_path(exe_path):
        raise RuntimeError(
            f"Watcher is running from a network share: {exe_path}. "
            "Copy it to a local folder and run it locally."
        )


def _acquire_single_instance_mutex() -> None:
    global _single_instance_mutex
    kernel32 = ctypes.windll.kernel32
    mutex_name = "WhatsAppWatcher_SingleInstance"
    _single_instance_mutex = kernel32.CreateMutexW(None, False, mutex_name)
    last_error = kernel32.GetLastError()
    ERROR_ALREADY_EXISTS = 183
    if last_error == ERROR_ALREADY_EXISTS:
        raise RuntimeError("Another WhatsApp Watcher instance is already running on this PC.")



# ─── DB log handler ───────────────────────────────────────────────────────────

class StorageLogHandler(logging.Handler):
    def __init__(self, storage: Storage) -> None:
        super().__init__(level=logging.INFO)
        self.storage = storage
        self._queue: "queue.Queue[dict]" = queue.Queue()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._run, name="storage-log-writer", daemon=True)
        self._dropped_count = 0
        self._internal_log = logging.getLogger("watcher.dbqueue")
        self._worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith("watcher.storage") or record.name == "watcher.dbqueue":
                return
            payload = {
                "level": record.levelname,
                "logger_name": record.name,
                "message": self.format(record),
                "is_error": record.levelno >= logging.ERROR,
                "exception_type": record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else None,
                "stack_trace": "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else None,
            }
            self._queue.put_nowait(payload)
        except Exception:
            pass

    def _run(self) -> None:
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                payload = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if payload["is_error"]:
                    self.storage.save_error_log(
                        level=payload["level"],
                        logger_name=payload["logger_name"],
                        event_type="runtime_log",
                        message=payload["message"],
                        exception_type=payload["exception_type"],
                        stack_trace=payload["stack_trace"],
                    )
                else:
                    self.storage.save_info_log(
                        level=payload["level"],
                        logger_name=payload["logger_name"],
                        event_type="runtime_log",
                        message=payload["message"],
                    )
            except Exception:
                pass
            finally:
                self._queue.task_done()

    def close(self) -> None:
        self._stop_event.set()
        try:
            self._worker.join(timeout=15.0)
            if self._worker.is_alive():
                with suppress(Exception):
                    while True:
                        self._queue.get_nowait()
                        self._queue.task_done()
                self._worker.join(timeout=5.0)
            if self._worker.is_alive():
                self._internal_log.warning(
                    "StorageLogHandler worker did not exit cleanly; remaining queued DB logs were abandoned"
                )
        except Exception:
            pass
        super().close()
# ─── Helpers ─────────────────────────────────────────────────────────────────

def _finalize_call(
    session_snap,
    recorder: Recorder,
    recorder_contexts,
    storage: Storage,
    reporter: DailyCallReporter,
    uploader: UploadManager,
) -> None:
    """
    Finalize a call that has a recording.
    Runs in a background thread — multiple concurrent finalizations are safe
    because each has its own recorder_contexts and deepcopy of session.
    """
    try:
        if getattr(session_snap, "ended_at", None) is None:
            session_snap.ended_at = datetime.now()
        log.info(
            "FINALIZE → starting | status=%s | dir=%s | number=%s",
            session_snap.status,
            session_snap.direction or "unknown",
            getattr(session_snap, "caller_number", None) or "-",
        )

        # resolve_final_files sleeps OUTSIDE the recorder lock now (no freeze)
        resolved_paths = recorder.resolve_final_files(recorder_contexts or [])
        renamed_paths = []
        total_parts = len(resolved_paths)

        for idx, raw_path in enumerate(resolved_paths, start=1):
            session_snap.recording_path = raw_path
            renamed = rename_recording_for_session(
                session_snap, part_index=idx, part_count=total_parts
            )
            if renamed:
                renamed_paths.append(renamed)
                log.info("FINALIZE → segment %s renamed to: %s", idx, renamed)
            else:
                renamed_paths.append(raw_path)
                log.warning(
                    "FINALIZE → segment %s rename failed; keeping original: %s",
                    idx, raw_path,
                )
            session_snap.recording_path = renamed_paths[-1]

        if renamed_paths:
            session_snap.recording_path = " | ".join(renamed_paths)

        if not renamed_paths:
            metadata = recorder.get_recording_metadata() if hasattr(recorder, "get_recording_metadata") else {}
            issue = metadata.get("recording_issues") or "[REC-006]"
            msg = f"Recording finalization failed: no valid recording file resolved | issue={issue}"
            if session_snap.error_details:
                session_snap.error_details += " | " + msg
            else:
                session_snap.error_details = msg

        log.info("FINALIZE → saving to local DB")
        call_local_id = storage.save_call(session_snap)
        log.info("FINALIZE → saved to local DB (id=%s)", call_local_id)

        uploaded_paths = []
        for idx, rec_path in enumerate(renamed_paths, start=1):
            tmp_session = copy.deepcopy(session_snap)
            tmp_session.recording_path = rec_path
            log.info(
                "FINALIZE → uploading/queuing segment %s/%s: %s",
                idx, total_parts, rec_path,
            )
            uploaded = uploader.upload_for_session(tmp_session, call_local_id=call_local_id)
            if uploaded:
                uploaded_paths.append(uploaded)
                log.info("FINALIZE → segment %s uploaded: %s", idx, uploaded)
            else:
                log.info("FINALIZE → segment %s upload queued/skipped", idx)

        if uploaded_paths:
            session_snap.uploaded_path = " | ".join(uploaded_paths)
            storage.update_uploaded_path(call_local_id, session_snap.uploaded_path)
        elif not renamed_paths:
            log.warning("FINALIZE → no recording file resolved for this call")

        reporter.append_call(session_snap)
        log.info(
            "FINALIZE → complete | status=%s | dir=%s | dur=%ss | number=%s | "
            "local=%s | uploaded=%s | ip=%s",
            session_snap.status,
            session_snap.direction or "unknown",
            session_snap.duration_seconds(),
            getattr(session_snap, "caller_number", None) or "-",
            session_snap.recording_path or "none",
            getattr(session_snap, "uploaded_path", None) or "none",
            getattr(session_snap, "machine_ip", "-"),
        )
    except Exception:
        log.exception("FINALIZE → unexpected error")


def _finalize_no_recording(
    session_snap, storage: Storage, reporter: DailyCallReporter
) -> None:
    """Finalize a call that has no recording (recorder failed or was not started)."""
    try:
        if getattr(session_snap, "ended_at", None) is None:
            session_snap.ended_at = datetime.now()
        log.info(
            "FINALIZE → no-recording | status=%s | dir=%s | number=%s",
            session_snap.status,
            session_snap.direction or "unknown",
            getattr(session_snap, "caller_number", None) or "-",
        )
        storage.save_call(session_snap)
        reporter.append_call(session_snap)
        log.info(
            "FINALIZE → complete (no recording) | status=%s | dir=%s | dur=%ss | number=%s | ip=%s",
            session_snap.status,
            session_snap.direction or "unknown",
            session_snap.duration_seconds(),
            getattr(session_snap, "caller_number", None) or "-",
            getattr(session_snap, "machine_ip", "-"),
        )
    except Exception:
        log.exception("FINALIZE → unexpected error (no-recording path)")


def _queue_closed_daily_reports(
    reporter: DailyCallReporter, uploader: UploadManager
) -> None:
    queued = reporter.enqueue_closed_reports(uploader, now=datetime.now())
    if queued:
        log.info("REPORTER → queued %s closed daily report(s) for upload", queued)
    else:
        log.debug("REPORTER → no closed daily reports to queue")

def _list_recoverable_recording_files(recorder: Recorder) -> list[Path]:
    try:
        recorder.refresh_bandicam_paths()
        output_dir = recorder.bandicam_output_dir
        if not output_dir:
            return []

        files: list[Path] = []
        for item in output_dir.iterdir():
            if not item.is_file():
                continue
            if item.suffix.lower() not in ALLOWED_MEDIA_EXTENSIONS:
                continue
            files.append(item)

        files.sort(key=lambda p: p.stat().st_mtime)
        return files
    except Exception:
        log.exception("RECOVERY → failed listing recorder output files")
        return []

def _recover_orphan_seg_files(recorder: Recorder, storage: Storage) -> int:
    """
    Recover unfinalized WAV files left over from crashes.
    These are *_seg1.wav files that were never renamed/uploaded.
    Rename them to standard format and trigger upload.
    Returns count of files recovered.
    """
    try:
        _output_dir = recorder.bandicam_output_dir
        if not _output_dir:
            return 0
        output_dir = Path(_output_dir)
        if not output_dir.exists():
            return 0

        recovered = 0
        for seg_file in output_dir.glob("*_seg1.wav"):
            try:
                log.info("RECOVERY → found orphan seg file: %s", seg_file.name)

                stem = seg_file.stem  # e.g. "2026-05-06_18-34-44_seg1"
                timestamp = stem.replace("_seg1", "")

                new_name = f"ORPHAN-{timestamp}.wav"
                new_path = output_dir / new_name

                seg_file.rename(new_path)
                log.info("RECOVERY → renamed to: %s", new_name)

                try:
                    call_dt = datetime.strptime(timestamp, "%Y-%m-%d_%H-%M-%S")
                except ValueError:
                    call_dt = datetime.now()

                class _OrphanSession:
                    pass

                call = _OrphanSession()
                call.started_at = call_dt
                call.ended_at = call_dt
                call.caller_number = "unknown"
                call.direction = "unknown"
                call.recording_path = str(new_path)
                call.uploaded_path = None
                call.error_details = "Recovered from orphan seg1.wav after crash"
                call.machine_ip = "-"
                call.pc_user = None
                call.machine_name = None
                call.status = CallState.ENDED.value

                storage.save_call(call)
                log.info("RECOVERY → saved orphan call to DB | path=%s", new_name)
                recovered += 1

            except Exception:
                log.exception("RECOVERY → failed to recover seg file: %s", seg_file.name)
                continue

        if recovered:
            log.info("RECOVERY → recovered %s orphan seg file(s)", recovered)
        return recovered
    except Exception:
        log.exception("RECOVERY → orphan seg file recovery failed completely")
        return 0

def _pick_recovery_file_for_call(call_row, candidate_files: Sequence[Path]) -> Optional[Path]:
    try:
        start_text = call_row["start_time"]
        end_text = call_row["end_time"]

        if not start_text:
            return None

        start_dt = datetime.strptime(start_text, "%Y-%m-%d %H:%M:%S")
        end_dt = (
            datetime.strptime(end_text, "%Y-%m-%d %H:%M:%S")
            if end_text
            else start_dt
        )

        start_ts = start_dt.timestamp() - 10.0
        end_ts = end_dt.timestamp() + 120.0

        matches: list[Path] = []
        for path in candidate_files:
            try:
                stat = path.stat()
            except OSError:
                continue
            if start_ts <= stat.st_mtime <= end_ts:
                matches.append(path)

        if not matches:
            return None

        matches.sort(key=lambda p: p.stat().st_mtime)
        return matches[-1]
    except Exception:
        log.exception(
            "RECOVERY → failed matching file for call_local_id=%s",
            call_row["id"],
        )
        return None

def _recover_orphan_recordings(
    storage: Storage,
    recorder: Recorder,
    uploader: UploadManager,
    limit: int = 20,
) -> int:
    recovered = 0
    try:
        rows = storage.get_calls_missing_recording_path(limit=limit)
        if not rows:
            return 0

        candidate_files = _list_recoverable_recording_files(recorder)
        if not candidate_files:
            return 0

        used_paths: set[str] = set()

        for row in rows:
            matched = _pick_recovery_file_for_call(row, candidate_files)
            if not matched:
                continue

            matched_resolved = str(matched.resolve())
            if matched_resolved in used_paths:
                continue

            try:
                matched_size = matched.stat().st_size
            except OSError:
                matched_size = -1

            log.info(
                "RECOVERY → matched candidate for call_local_id=%s | path=%s | size=%s bytes",
                row["id"],
                matched,
                matched_size,
            )

            try:
                if matched.stat().st_size <= 0:
                    log.debug(
                        "RECOVERY → skipping call_local_id=%s, file still writing: %s",
                        row["id"],
                        matched,
                    )
                    continue
            except OSError:
                continue

            log.info(
                "RECOVERY → proceeding with non-empty file for call_local_id=%s: %s",
                row["id"],
                matched,
            )

            recovered_path = matched_resolved

            class _RecoveredSession:
                pass

            recovered_session = _RecoveredSession()
            recovered_session.started_at = (
                datetime.strptime(row["start_time"], "%Y-%m-%d %H:%M:%S")
                if row["start_time"]
                else None
            )
            recovered_session.ended_at = (
                datetime.strptime(row["end_time"], "%Y-%m-%d %H:%M:%S")
                if row["end_time"]
                else recovered_session.started_at
            )
            recovered_session.status = row["status"]
            recovered_session.direction = row["direction"]
            recovered_session.caller_number = row["caller_number"]
            recovered_session.recording_path = matched_resolved
            recovered_session.uploaded_path = row["uploaded_path"]
            recovered_session.error_details = row["error_details"]
            recovered_session.pc_user = row["pc_user"]
            recovered_session.machine_name = row["machine_name"]
            recovered_session.machine_ip = row["machine_ip"]

            renamed = rename_recording_for_session(
                recovered_session,
                part_index=1,
                part_count=1,
            )
            if renamed:
                recovered_path = renamed
                log.info(
                    "RECOVERY → renamed orphan recording for call_local_id=%s: %s",
                    row["id"],
                    recovered_path,
                )

            existing_call = storage.find_call_by_recording_path(
                recovered_path,
                exclude_call_local_id=int(row["id"]),
            )
            if existing_call:
                log.warning(
                    "RECOVERY → skipping call_local_id=%s because file is already linked to call_local_id=%s: %s",
                    row["id"],
                    existing_call["id"],
                    recovered_path,
                )
                used_paths.add(str(Path(recovered_path).resolve()))
                continue

            storage.update_call_recording_path(
                int(row["id"]),
                recovered_path,
                error_details=row["error_details"],
            )
            log.info(
                "RECOVERY → updated call_local_id=%s with recovered recording path: %s",
                row["id"],
                recovered_path,
            )

            dest_rel_path = uploader.plan_destination_rel_path(
                recovered_session,
                recovered_path,
            )
            existing_upload = storage.get_upload_by_local_and_dest(recovered_path, dest_rel_path)
            if existing_upload and existing_upload["status"] == "uploaded":
                log.info(
                    "RECOVERY → already uploaded, skipping call_local_id=%s | upload_id=%s | path=%s",
                    row["id"],
                    existing_upload["id"],
                    recovered_path,
                )
                used_paths.add(str(Path(recovered_path).resolve()))
                continue

            upload_row = storage.get_upload_by_local_and_dest(recovered_path, dest_rel_path)
            if not upload_row:
                upload_id = storage.enqueue_pending_upload(
                    call_local_id=int(row["id"]),
                    local_path=recovered_path,
                    dest_rel_path=dest_rel_path,
                    pc_user=row["pc_user"],
                    machine_name=row["machine_name"],
                    machine_ip=row["machine_ip"],
                    max_retries=UPLOAD_RETRY_COUNT,
                )
                log.info(
                    "RECOVERY → queued upload for call_local_id=%s | upload_id=%s | dest_rel=%s",
                    row["id"],
                    upload_id,
                    dest_rel_path,
                )
            else:
                log.info(
                    "RECOVERY → upload row already exists for call_local_id=%s | upload_id=%s | status=%s | dest_rel=%s",
                    row["id"],
                    existing_upload["id"] if 'existing_upload' in locals() and existing_upload else upload_row["id"],
                    upload_row["status"],
                    dest_rel_path,
                )


            used_paths.add(str(Path(recovered_path).resolve()))
            recovered += 1
            log.info(
                "RECOVERY → recovered recording for call_local_id=%s: %s",
                row["id"],
                recovered_path,
            )

        return recovered
    except Exception:
        log.exception("RECOVERY → orphan recording recovery failed")
        return recovered

def _recover_unfinalized_seg_files(
    storage: Storage,
    recorder: Recorder,
    uploader: UploadManager,
) -> int:
    """
    Recover *_seg1.wav files left when app crashed before finalization.
    Matches them to DB calls by timestamp, renames properly, queues upload.
    Skips files modified in last 60s (may be actively recording).
    Runs inside background sync thread — never blocks main loop.
    """
    try:
        output_dir = recorder.bandicam_output_dir
        if not output_dir:
            return 0
        output_dir = Path(output_dir)
        if not output_dir.exists():
            return 0

        recovered = 0
        now = time.time()

        for seg_file in output_dir.glob("*_seg1.wav"):
            try:
                # Skip files touched in last 60s — may be actively recording
                try:
                    stat = seg_file.stat()
                    if now - stat.st_mtime < 60.0:
                        log.debug(
                            "RECOVERY → skipping recent seg file"
                            " (may be active): %s",
                            seg_file.name,
                        )
                        continue
                    if stat.st_size <= 0:
                        log.debug(
                            "RECOVERY → skipping empty seg file: %s",
                            seg_file.name,
                        )
                        continue
                except OSError:
                    continue

                # Parse timestamp: "2026-05-06_18-34-44_seg1" → datetime
                stem = seg_file.stem
                timestamp_str = stem.replace("_seg1", "")
                try:
                    file_dt = datetime.strptime(
                        timestamp_str, "%Y-%m-%d_%H-%M-%S"
                    )
                except ValueError:
                    log.warning(
                        "RECOVERY → cannot parse timestamp from: %s — skipping",
                        seg_file.name,
                    )
                    continue

                log.info(
                    "RECOVERY → found unfinalized seg file: %s | time=%s",
                    seg_file.name,
                    file_dt.strftime("%Y-%m-%d %H:%M:%S"),
                )

                # ±30s window: ring detected a few seconds before recording starts
                rows = storage.get_calls_by_start_time_window(
                    file_dt - timedelta(seconds=30),
                    file_dt + timedelta(seconds=30),
                )

                # Only unlinked calls (no recording_path, no uploaded_path)
                unlinked = [
                    r for r in rows
                    if not (r["recording_path"] or r["uploaded_path"])
                ]

                if not unlinked:
                    log.warning(
                        "RECOVERY → no unlinked DB call for: %s"
                        " | window=±30s around %s",
                        seg_file.name,
                        file_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    )
                    continue

                row = unlinked[0]
                log.info(
                    "RECOVERY → matched seg to call_local_id=%s"
                    " | caller=%s | dir=%s",
                    row["id"], row["caller_number"], row["direction"],
                )

                # Build session — identical pattern to _recover_orphan_recordings
                class _RecoveredSegSession:
                    pass

                sess = _RecoveredSegSession()
                sess.started_at = (
                    datetime.strptime(row["start_time"], "%Y-%m-%d %H:%M:%S")
                    if row["start_time"] else file_dt
                )
                sess.ended_at = (
                    datetime.strptime(row["end_time"], "%Y-%m-%d %H:%M:%S")
                    if row["end_time"] else file_dt
                )
                sess.status         = row["status"]        or "ended"
                sess.direction      = row["direction"]     or "unknown"
                sess.caller_number  = row["caller_number"] or "unknown"
                sess.recording_path = str(seg_file)
                sess.uploaded_path  = None
                sess.error_details  = row["error_details"]
                sess.pc_user        = row["pc_user"]
                sess.machine_name   = row["machine_name"]
                sess.machine_ip     = row["machine_ip"]

                # Rename using existing function — same as normal finalize
                renamed = rename_recording_for_session(
                    sess, part_index=1, part_count=1
                )
                recovered_path = renamed if renamed else str(seg_file)
                if renamed:
                    log.info(
                        "RECOVERY → renamed: %s → %s",
                        seg_file.name, Path(renamed).name,
                    )
                else:
                    log.warning(
                        "RECOVERY → rename failed, keeping: %s",
                        seg_file.name,
                    )

                # Duplicate guard
                existing = storage.find_call_by_recording_path(
                    recovered_path,
                    exclude_call_local_id=int(row["id"]),
                )
                if existing:
                    log.warning(
                        "RECOVERY → path already linked to"
                        " call_local_id=%s — skipping",
                        existing["id"],
                    )
                    continue

                # Link recording to DB call
                storage.update_call_recording_path(
                    int(row["id"]),
                    recovered_path,
                    error_details=row["error_details"],
                )
                log.info(
                    "RECOVERY → linked call_local_id=%s | file=%s",
                    row["id"], Path(recovered_path).name,
                )

                # Queue upload — same as _recover_orphan_recordings
                dest_rel_path = uploader.plan_destination_rel_path(
                    sess, recovered_path
                )
                existing_upload = storage.get_upload_by_local_and_dest(
                    recovered_path, dest_rel_path
                )
                if existing_upload and existing_upload["status"] == "uploaded":
                    log.info(
                        "RECOVERY → already uploaded, skipping call_local_id=%s",
                        row["id"],
                    )
                    continue
                if not existing_upload:
                    upload_id = storage.enqueue_pending_upload(
                        call_local_id=int(row["id"]),
                        local_path=recovered_path,
                        dest_rel_path=dest_rel_path,
                        pc_user=row["pc_user"],
                        machine_name=row["machine_name"],
                        machine_ip=row["machine_ip"],
                        max_retries=UPLOAD_RETRY_COUNT,
                    )
                    log.info(
                        "RECOVERY → queued upload | call_local_id=%s"
                        " | upload_id=%s | dest=%s",
                        row["id"], upload_id, dest_rel_path,
                    )

                recovered += 1
                log.info(
                    "RECOVERY → seg recovered for call_local_id=%s: %s",
                    row["id"], Path(recovered_path).name,
                )

            except Exception:
                log.exception(
                    "RECOVERY → failed processing seg file: %s",
                    seg_file.name,
                )
                continue

        return recovered

    except Exception:
        log.exception("RECOVERY → unfinalized seg file scan failed")
        return 0


def _background_sync_and_upload(
    reporter: DailyCallReporter, uploader: UploadManager, storage: Storage, recorder: Recorder
) -> None:
    try:
        log.debug("SYNC → starting background sync/upload scan")

        recovered_segs = _recover_unfinalized_seg_files(
            storage, recorder, uploader
        )
        if recovered_segs:
            log.info(
                "RECOVERY → recovered %s unfinalized seg file(s)",
                recovered_segs,
            )

        recovered = _recover_orphan_recordings(storage, recorder, uploader)
        if recovered:
            log.info("RECOVERY → repaired %s call(s) with orphan local recordings", recovered)

        synced_calls = storage.sync_unsynced_calls_to_central()
        synced_info = storage.sync_unsynced_info_logs_to_central()
        synced_errors = storage.sync_unsynced_error_logs_to_central()

        if synced_calls:
            log.info("SYNC → synced %s call row(s) to central DB", synced_calls)
        if synced_info:
            log.info("SYNC → synced %s info log row(s) to central DB", synced_info)
        if synced_errors:
            log.info("SYNC → synced %s error log row(s) to central DB", synced_errors)

        if UPLOAD_ENABLED:
            _queue_closed_daily_reports(reporter, uploader)
            log.debug("SYNC → processing pending uploads")
            uploader.process_pending_uploads()
        log.debug("SYNC → scan complete")
    except Exception:
        log.exception("SYNC → background scan failed")


def _background_daily_cleanup(storage: Storage) -> None:
    try:
        deleted_info = storage.cleanup_synced_info_logs(keep_latest=500)
        deleted_errors = storage.cleanup_synced_error_logs(keep_latest=500)
        if deleted_info:
            log.info("CLEANUP → deleted %s synced info log row(s)", deleted_info)
        if deleted_errors:
            log.info("CLEANUP → deleted %s synced error log row(s)", deleted_errors)
    except Exception:
        log.exception("CLEANUP → local DB cleanup failed")


def _should_start_recording(sm: StateMachine) -> bool:
    return sm.state in (
        CallState.RINGING_UNKNOWN,
        CallState.RINGING_INCOMING,
        CallState.RINGING_OUTGOING,
        CallState.CONNECTING,
        CallState.ACTIVE,
    )


_FINALIZE_COUNTER = itertools.count(1)

# States that have no live call — no split needed when entering a ring from here.
_TERMINAL_STATES: frozenset = frozenset({
    CallState.IDLE,
    CallState.ENDED,
    CallState.RECORDER_ERROR,
    CallState.DETECTOR_ERROR,
})

_LATCH_PATH = DATA_DIR / "active_call_session.json"


def _should_update_direction(new_dir: Optional[str], current_dir: Optional[str]) -> bool:
    """Return True only when new_dir is a genuine improvement over current_dir.
    "unknown" must never overwrite a proven "incoming"/"outgoing" direction.
    """
    if not new_dir:
        return False
    if new_dir == current_dir:
        return False
    if new_dir == "unknown" and current_dir in ("incoming", "outgoing"):
        return False
    return True


def _save_session_latch(
    direction: str,
    hwnd: Optional[int],
    session_generation: int,
    started_at,
) -> None:
    try:
        data = {
            "direction": direction,
            "hwnd": hwnd,
            "session_generation": session_generation,
            "started_at": started_at.isoformat() if hasattr(started_at, "isoformat") else str(started_at),
            "saved_at": datetime.now().isoformat(),
        }
        _LATCH_PATH.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        log.exception("[LATCH] Failed to save session latch")


def _load_session_latch() -> Optional[dict]:
    try:
        if not _LATCH_PATH.exists():
            return None
        return json.loads(_LATCH_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _clear_session_latch() -> None:
    try:
        if _LATCH_PATH.exists():
            _LATCH_PATH.unlink()
    except Exception:
        log.exception("[LATCH] Failed to clear session latch")


def _prune_finalize_threads(threads: set[threading.Thread]) -> None:
    """Remove completed finalize threads from the tracking set."""
    done = {t for t in threads if not t.is_alive()}
    threads -= done


def _wait_for_thread_group(threads: list[threading.Thread], *, per_thread_timeout: float, label: str) -> list[threading.Thread]:
    stuck: list[threading.Thread] = []
    for t in threads:
        if t is None:
            continue
        try:
            t.join(timeout=per_thread_timeout)
        except Exception:
            log.exception("SHUTDOWN → join failed for %s thread %s", label, getattr(t, "name", "?"))
        if t.is_alive():
            stuck.append(t)
    return stuck



# ─── Main run loop ────────────────────────────────────────────────────────────

def run() -> None:
    try:
        _ensure_not_running_from_network_share()
        _acquire_single_instance_mutex()
    except Exception as exc:
        log.error("STARTUP → %s", exc)
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                str(exc),
                "WhatsApp Watcher",
                0x00000010,
            )
        except Exception:
            pass
        return

    log.info("=== WhatsApp Watcher starting ===")
    log.info("STARTUP → PID: %s", os.getpid())
    log.info("STARTUP → executable: %s", Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve())
    log.info("STARTUP → log: %s", log_path)
    log.info("STARTUP → poll interval: %.1fs", POLL_INTERVAL_SECONDS)

    if CONFIG_LOAD_ERROR:
        log.warning("STARTUP → config warning: %s", CONFIG_LOAD_ERROR)

    detector = WhatsAppDetector()
    sm = StateMachine()
    recorder = Recorder()

    log.info("STARTUP → initializing local SQLite storage")
    storage = Storage()
    log.info("STARTUP → local storage ready")

    db_log_handler = StorageLogHandler(storage)
    db_log_handler.setLevel(logging.INFO)
    db_log_handler.setFormatter(formatter)
    root_logger.addHandler(db_log_handler)

    reporter = DailyCallReporter()
    uploader = UploadManager(storage)

    if recorder.bandicam_path:
        log.info("STARTUP → Recorder ready | path=%s", recorder.bandicam_path)
    else:
        log.warning("STARTUP → Recorder NOT ready — recording will fail")

    if recorder.bandicam_output_dir:
        log.info("STARTUP → Recorder output dir: %s", recorder.bandicam_output_dir)
    else:
        log.warning("STARTUP → Recorder output dir not found")

    last_sync_ts = 0.0
    last_cleanup_ts = 0.0
    current_session_hwnd: Optional[int] = None
    current_session_generation: int = 0
    sync_thread: Optional[threading.Thread] = None
    cleanup_thread: Optional[threading.Thread] = None
    finalize_threads: set[threading.Thread] = set()
    sync_lock = threading.Lock()
    cleanup_lock = threading.Lock()
    _direction_latched: bool = False
    _startup_latch: Optional[dict] = _load_session_latch()
    if _startup_latch:
        log.info(
            "[LATCH] Found active session latch at startup | direction=%s | saved_at=%s",
            _startup_latch.get("direction"), _startup_latch.get("saved_at"),
        )

    # Recover any orphan seg1.wav files from crashes before finalization
    orphan_count = _recover_orphan_seg_files(recorder, storage)
    if orphan_count:
        log.info("STARTUP → recovered %s orphan call(s) | will upload in background sync",
                 orphan_count)

    if STARTUP_PENDING_UPLOAD_SCAN:
        log.info("STARTUP → running startup sync/upload scan")
        try:
            t = threading.Thread(
                target=_background_sync_and_upload,
                args=(reporter, uploader, storage, recorder),
                daemon=True,
                name="startup-sync",
            )
            t.start()
            last_sync_ts = time.time()
        except Exception:
            log.exception("STARTUP → startup scan failed to start")

    log.info("STARTUP → entering main poll loop")
    _last_logged_state: Optional[str] = None

    try:
        while True:
            try:
                now_ts = time.time()

                # ── Periodic sync/upload ──────────────────────────────────
                if now_ts - last_sync_ts >= max(30.0, CENTRAL_SYNC_INTERVAL_SECONDS):
                    try:
                        with sync_lock:
                            if sync_thread is None or not sync_thread.is_alive():
                                sync_thread = threading.Thread(
                                    target=_background_sync_and_upload,
                                    args=(reporter, uploader, storage, recorder),
                                    daemon=True,
                                    name="periodic-sync",
                                )
                                sync_thread.start()
                    except Exception:
                        log.exception("SYNC → failed to start background thread")
                    finally:
                        last_sync_ts = now_ts

                # ── Periodic cleanup ──────────────────────────────────────
                if now_ts - last_cleanup_ts >= max(300.0, LOCAL_CLEANUP_INTERVAL_SECONDS):
                    try:
                        with cleanup_lock:
                            if cleanup_thread is None or not cleanup_thread.is_alive():
                                cleanup_thread = threading.Thread(
                                    target=_background_daily_cleanup,
                                    args=(storage,),
                                    daemon=True,
                                    name="daily-cleanup",
                                )
                                cleanup_thread.start()
                    except Exception:
                        log.exception("CLEANUP → failed to start thread")
                    finally:
                        last_cleanup_ts = now_ts

                # ── Prune completed finalize threads ──────────────────────
                _prune_finalize_threads(finalize_threads)

                # ── Poll detector ─────────────────────────────────────────
                result = detector.poll()

                # ── New-call boundary split ────────────────────────────────
                # Snapshot old session BEFORE direction propagation so old
                # direction is never overwritten before the finalize thread reads it.
                result_hwnd = getattr(result, "hwnd", None)
                is_new_call_event = result.event in (
                    CallEvent.INCOMING_RING, CallEvent.OUTGOING_RING, CallEvent.CALL_STARTED,
                )
                is_live_session = sm.state not in _TERMINAL_STATES
                different_hwnd = (
                    current_session_hwnd is not None
                    and result_hwnd is not None
                    and result_hwnd != current_session_hwnd
                )
                # Same hwnd, but generation incremented → detector saw a new ring
                # event on the same window handle (rapid reuse by WhatsApp).
                different_generation = (
                    current_session_hwnd is not None
                    and result_hwnd is not None
                    and result_hwnd == current_session_hwnd
                    and current_session_generation != 0
                    and getattr(result, "session_generation", 0) != current_session_generation
                )
                strong_new_call = bool(getattr(result, "is_strong_new_call", False))
                new_dir = result.direction or (
                    "incoming" if result.event == CallEvent.INCOMING_RING
                    else "outgoing" if result.event == CallEvent.OUTGOING_RING
                    else "unknown"
                )
                # A CALL_STARTED with no strong proof (unknown direction, no UIA
                # call evidence) must not split a live session — it could be a
                # stale or ambiguous window.  It can still start a new session
                # from IDLE (is_live_session=False path).
                weak_call_started = (
                    result.event == CallEvent.CALL_STARTED
                    and not strong_new_call
                )
                split_needed = (
                    is_live_session
                    and is_new_call_event
                    and not weak_call_started
                    and (
                        different_hwnd
                        or different_generation
                        or strong_new_call
                        or sm.state in (
                            CallState.RINGING_UNKNOWN,
                            CallState.RINGING_INCOMING,
                            CallState.RINGING_OUTGOING,
                            CallState.CONNECTING,
                        )
                    )
                )

                if split_needed:
                    split_snap = copy.deepcopy(sm.session)  # snapshot before any mutation
                    old_dir = split_snap.direction or "unknown"
                    log.warning(
                        "SESSION SPLIT → new call boundary"
                        " | old_state=%s | old_dir=%s | new_dir=%s"
                        " | old_hwnd=%s | new_hwnd=%s | strong_new=%s"
                        " | old_gen=%s | new_gen=%s",
                        sm.state.value, old_dir, new_dir,
                        current_session_hwnd, result_hwnd, strong_new_call,
                        current_session_generation,
                        getattr(result, "session_generation", 0),
                    )
                    if split_snap.ended_at is None:
                        split_snap.ended_at = datetime.now()
                    split_snap.status = CallState.ENDED.value
                    split_was_recording = recorder.is_recording
                    split_recorder_contexts = []
                    if split_was_recording:
                        split_ok = recorder.stop_recording()
                        split_recorder_contexts = recorder.detach_contexts()
                        if not split_ok:
                            log.error("RECORDER → /stop failed during session split")
                    sm.transition(CallEvent.RESET)
                    current_session_hwnd = None
                    current_session_generation = 0
                    # Do NOT call detector.reset() — detector already tracks the new window.
                    _last_logged_state = None
                    if split_was_recording and split_recorder_contexts:
                        split_t = threading.Thread(
                            target=_finalize_call,
                            args=(split_snap, recorder, split_recorder_contexts, storage, reporter, uploader),
                            daemon=True,
                            name=f"finalize-split-{next(_FINALIZE_COUNTER)}-{datetime.now().strftime('%H%M%S')}",
                        )
                    else:
                        split_t = threading.Thread(
                            target=_finalize_no_recording,
                            args=(split_snap, storage, reporter),
                            daemon=True,
                            name=f"finalize-split-norec-{next(_FINALIZE_COUNTER)}-{datetime.now().strftime('%H%M%S')}",
                        )
                    finalize_threads.add(split_t)
                    split_t.start()
                    _clear_session_latch()
                    _direction_latched = False
                    log.info("SESSION SPLIT → old session finalized; processing new ring as fresh call")

                # ── Latch restore (crash-recovery only, first ring after restart) ──
                # If we crashed mid-call, a proven direction was saved to the latch.
                # Restore it only when hwnd or session_generation matches the latch,
                # confirming this ring event belongs to the same ongoing call.
                if is_new_call_event and _startup_latch is not None and not _direction_latched:
                    _latch_dir = _startup_latch.get("direction", "")
                    _latch_hwnd = _startup_latch.get("hwnd")
                    _latch_gen = _startup_latch.get("session_generation", 0)
                    _latch_saved_str = _startup_latch.get("saved_at", "")
                    try:
                        _latch_ts = datetime.fromisoformat(_latch_saved_str)
                        _freshness_ok = (datetime.now() - _latch_ts).total_seconds() <= 3600
                    except Exception:
                        _freshness_ok = False
                    _hwnd_match = (_latch_hwnd is not None and result_hwnd is not None
                                   and _latch_hwnd == result_hwnd)
                    _gen_match = (_latch_gen != 0
                                  and getattr(result, "session_generation", 0) != 0
                                  and getattr(result, "session_generation", 0) == _latch_gen)
                    if (_freshness_ok and (_hwnd_match or _gen_match)
                            and _latch_dir in ("incoming", "outgoing")
                            and result.direction in (None, "unknown")):
                        result.direction = _latch_dir
                        log.info(
                            "[LATCH] Restored direction from latch | dir=%s"
                            " | hwnd_match=%s | gen_match=%s",
                            _latch_dir, _hwnd_match, _gen_match,
                        )
                    _startup_latch = None  # consume once regardless of match

                # Propagate direction / caller into session (best-effort).
                # _should_update_direction guards against "unknown" overwriting
                # a proven direction.
                if _should_update_direction(result.direction, sm.session.direction):
                    sm.session.direction = result.direction
                    if sm.session.direction in ("incoming", "outgoing") and not _direction_latched:
                        _save_session_latch(
                            sm.session.direction,
                            current_session_hwnd,
                            current_session_generation,
                            sm.session.started_at,
                        )
                        _direction_latched = True
                if result.caller_number:
                    sm.session.caller_number = result.caller_number

                # ── Recorder health check (idle cycles only) ─────────────
                # Skip when an actual event is present — the event must be
                # processed immediately; health checks are deferred until the
                # next idle poll.  (ensure_recording_alive's mute probe was a
                # 20-30 s UIA traversal before being made async; this guard is
                # additional defence so no health work blocks event handling.)
                if result.event is None and _should_start_recording(sm) and recorder.is_recording:
                    recorder.ensure_recording_alive()

                if (
                    result.event is None
                    and result.details == "WhatsApp not running"
                    and recorder.is_recording
                    and _should_start_recording(sm)
                ):
                    log.warning("RECORDER → WhatsApp disappeared during active call; forcing stop/finalize")
                    stop_ok = recorder.stop_recording()
                    forced_stop_ok = False
                    if not stop_ok:
                        log.warning("RECORDER → /stop failed after WhatsApp disappeared; attempting force stop")
                        forced_stop_ok = recorder.force_stop_recording()
                    recorder_contexts = recorder.detach_contexts()
                    session_snap = copy.deepcopy(sm.session)
                    session_snap.ended_at = session_snap.ended_at or datetime.now()
                    error_parts = [part.strip() for part in (session_snap.error_details or "").split("|") if part.strip()]
                    error_parts.append("WhatsApp crashed or closed during active recording")
                    if not stop_ok and forced_stop_ok:
                        error_parts.append("Recorder stop command failed; recorder was force-stopped")
                    elif not stop_ok:
                        error_parts.append("Recorder stop failed after WhatsApp crash")
                    session_snap.error_details = " | ".join(error_parts)
                    session_snap.status = CallState.ENDED.value
                    sm.transition(CallEvent.RESET)
                    detector.reset()
                    current_session_hwnd = None
                    current_session_generation = 0
                    _last_logged_state = None
                    can_finalize_recording = bool(recorder_contexts) and (stop_ok or forced_stop_ok)
                    if can_finalize_recording:
                        t = threading.Thread(
                            target=_finalize_call,
                            args=(session_snap, recorder, recorder_contexts, storage, reporter, uploader),
                            daemon=True,
                            name=f"finalize-crash-{next(_FINALIZE_COUNTER)}-{datetime.now().strftime('%H%M%S')}",
                        )
                    else:
                        if recorder_contexts and not (stop_ok or forced_stop_ok):
                            log.error("RECORDER → abandoning recording-file finalization — recorder could not be stopped cleanly after WhatsApp crash")
                        t = threading.Thread(
                            target=_finalize_no_recording,
                            args=(session_snap, storage, reporter),
                            daemon=True,
                            name=f"finalize-crash-norec-{next(_FINALIZE_COUNTER)}-{datetime.now().strftime('%H%M%S')}",
                        )
                    finalize_threads.add(t)
                    t.start()
                    _clear_session_latch()
                    _direction_latched = False
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

                if result.event is None:
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

                log.info(
                    "DETECTOR → event=%-22s | source=%-12s | %s | number=%s",
                    result.event,
                    result.source,
                    result.details,
                    result.caller_number or sm.session.caller_number or "-",
                )

                prev_state = sm.state.value
                sm.transition(result.event)

                # Track which hwnd and generation own the active session for split detection
                if is_new_call_event and result_hwnd:
                    current_session_hwnd = result_hwnd
                    current_session_generation = getattr(result, "session_generation", 0)

                # Re-apply direction after transition (state machine may have reset).
                # _should_update_direction guards against "unknown" overwriting
                # a proven direction.
                if _should_update_direction(result.direction, sm.session.direction):
                    sm.session.direction = result.direction
                    if sm.session.direction in ("incoming", "outgoing") and not _direction_latched:
                        _save_session_latch(
                            sm.session.direction,
                            current_session_hwnd,
                            current_session_generation,
                            sm.session.started_at,
                        )
                        _direction_latched = True
                if result.caller_number:
                    sm.session.caller_number = result.caller_number

                new_state = sm.state.value
                if new_state != _last_logged_state:
                    log.info(
                        "STATE    → %s (from %s) | dir=%s | number=%s",
                        new_state, prev_state,
                        sm.session.direction or "-",
                        sm.session.caller_number or "-",
                    )
                    _last_logged_state = new_state

                # ── Start recorder (synchronous — owns lifecycle) ─────────
                if (
                    _should_start_recording(sm)
                    and not recorder.is_recording
                    and "Recorder failed to start" not in (sm.session.error_details or "")
                ):
                    log.info("RECORDER → starting (state=%s)", sm.state.value)
                    _rec_t0 = time.monotonic()
                    _started = recorder.start_recording()
                    _rec_elapsed = time.monotonic() - _rec_t0
                    if _rec_elapsed > 2.0:
                        log.warning("[REC-011] Recording start took too long | total=%.1fs", _rec_elapsed)
                    if _started:
                        log.info("RECORDER → started successfully")
                    else:
                        sm.session.error_details = (
                            ((sm.session.error_details or "") + " | Recorder failed to start")
                        ).strip(" |")
                        log.error("RECORDER → failed to start; call will be saved without recording")

                # ── [REC-012] Orphan recorder guard ───────────────────────
                # recorder.is_recording should be False in any non-live state.
                # If it is not, stop it immediately before it contaminates the
                # next session.
                # MUST NOT fire when sm.is_terminal_state() — the terminal
                # finalization block below owns stop/detach/finalize for normal
                # ended calls.  Firing here would steal the recording and leave
                # the terminal block with no recorder, producing a duplicate
                # no-recording finalize entry.
                if recorder.is_recording and not _should_start_recording(sm) and not sm.is_terminal_state():
                    log.warning(
                        "[REC-012] Recorder active without live session — stopping orphan recording"
                        " | state=%s",
                        sm.state.value,
                    )
                    _orph_ok = recorder.stop_recording()
                    if not _orph_ok:
                        recorder.force_stop_recording()
                    _orph_ctxs = recorder.detach_contexts()
                    _orph_snap = copy.deepcopy(sm.session)
                    _orph_snap.ended_at = _orph_snap.ended_at or datetime.now()
                    _orph_snap.status = CallState.ENDED.value
                    _orph_err_parts = [p.strip() for p in (_orph_snap.error_details or "").split("|") if p.strip()]
                    _orph_err_parts.append(
                        "Recorder was active without a live call session; stopped by orphan guard"
                    )
                    _orph_snap.error_details = " | ".join(_orph_err_parts)
                    if _orph_ctxs:
                        _orph_t = threading.Thread(
                            target=_finalize_call,
                            args=(_orph_snap, recorder, _orph_ctxs, storage, reporter, uploader),
                            daemon=True,
                            name=f"finalize-orphan-{next(_FINALIZE_COUNTER)}-{datetime.now().strftime('%H%M%S')}",
                        )
                    else:
                        _orph_t = threading.Thread(
                            target=_finalize_no_recording,
                            args=(_orph_snap, storage, reporter),
                            daemon=True,
                            name=f"finalize-orphan-norec-{next(_FINALIZE_COUNTER)}-{datetime.now().strftime('%H%M%S')}",
                        )
                    finalize_threads.add(_orph_t)
                    _orph_t.start()
                    _clear_session_latch()
                    _direction_latched = False
                    if sm.state not in _TERMINAL_STATES:
                        sm.transition(CallEvent.RESET)
                        current_session_hwnd = None
                        current_session_generation = 0
                        _last_logged_state = None

                # ── Finalize on terminal state ────────────────────────────
                if sm.is_terminal_state():
                    log.info(
                        "CALL END → %s | dir=%s | dur=%ss",
                        sm.state.value,
                        sm.session.direction or "unknown",
                        sm.session.duration_seconds(),
                    )
                    was_recording = recorder.is_recording
                    recorder_contexts = []

                    if was_recording:
                        log.info("RECORDER → stopping for terminal state %s", sm.state.value)
                        ok = recorder.stop_recording()
                        recorder_contexts = recorder.detach_contexts()
                        if not ok:
                            log.error("RECORDER → /stop command failed")
                    else:
                        log.info("RECORDER → not active at terminal state %s", sm.state.value)

                    session_snap = copy.deepcopy(sm.session)
                    sm.transition(CallEvent.RESET)
                    detector.reset()
                    current_session_hwnd = None
                    current_session_generation = 0
                    _last_logged_state = None

                    # Each finalize gets its own deepcopy + contexts — safe to run concurrently
                    if was_recording and recorder_contexts:
                        t = threading.Thread(
                            target=_finalize_call,
                            args=(session_snap, recorder, recorder_contexts, storage, reporter, uploader),
                            daemon=True,
                            name=f"finalize-{next(_FINALIZE_COUNTER)}-{datetime.now().strftime('%H%M%S')}",
                        )
                    else:
                        t = threading.Thread(
                            target=_finalize_no_recording,
                            args=(session_snap, storage, reporter),
                            daemon=True,
                            name=f"finalize-norec-{next(_FINALIZE_COUNTER)}-{datetime.now().strftime('%H%M%S')}",
                        )
                    finalize_threads.add(t)
                    t.start()
                    _clear_session_latch()
                    _direction_latched = False
                    log.info("STATE    → reset to idle (finalize running in background)")

                time.sleep(POLL_INTERVAL_SECONDS)

            except Exception:
                log.exception("MAIN LOOP → cycle failed; watcher continues")

                # Try to salvage any active recording
                try:
                    if recorder.is_recording:
                        recorder.stop_recording()
                        ctx = recorder.detach_contexts()
                        if ctx:
                            snap = copy.deepcopy(sm.session)
                            if snap.ended_at is None:
                                snap.ended_at = datetime.now()
                            if not snap.status or snap.status == CallState.IDLE.value:
                                snap.status = CallState.DETECTOR_ERROR.value
                            if not getattr(snap, "error_details", None):
                                snap.error_details = "Interrupted by main-loop exception"
                            t = threading.Thread(
                                target=_finalize_call,
                                args=(snap, recorder, ctx, storage, reporter, uploader),
                                daemon=True,
                                name="finalize-after-exception",
                            )
                            finalize_threads.add(t)
                            t.start()
                            _clear_session_latch()
                            _direction_latched = False
                except Exception:
                    log.exception("RECORDER → failed to stop after main-loop exception")

                try:
                    detector.reset()
                except Exception:
                    log.exception("DETECTOR → reset failed")

                try:
                    sm.transition(CallEvent.RESET)
                    _last_logged_state = None
                except Exception:
                    log.exception("STATE → reset failed")

                time.sleep(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        log.info("=== Stopped by user ===")
    finally:
        log.info("SHUTDOWN → cleaning up...")
        if recorder.is_recording:
            try:
                recorder.stop_recording()
                ctx = recorder.detach_contexts()
                if ctx:
                    snap = copy.deepcopy(sm.session)
                    if snap.ended_at is None:
                        snap.ended_at = datetime.now()
                    if not snap.status or snap.status == CallState.IDLE.value:
                        snap.status = CallState.DETECTOR_ERROR.value
                    if not getattr(snap, "error_details", None):
                        snap.error_details = "Forced finalization on shutdown"
                    t = threading.Thread(
                        target=_finalize_call,
                        args=(snap, recorder, ctx, storage, reporter, uploader),
                        daemon=True,
                        name="finalize-on-shutdown",
                    )
                    finalize_threads.add(t)
                    t.start()
                    _clear_session_latch()
            except Exception:
                log.exception("SHUTDOWN → failed to finalize active recording")

        try:
            root_logger.removeHandler(db_log_handler)
        except Exception:
            pass
        try:
            db_log_handler.close()
        except Exception:
            log.exception("SHUTDOWN → failed closing DB log handler")

        stuck_finalize = _wait_for_thread_group(sorted(finalize_threads, key=lambda t: t.name), per_thread_timeout=90.0, label="finalize")
        stuck_sync = _wait_for_thread_group([sync_thread] if sync_thread else [], per_thread_timeout=30.0, label="sync")
        stuck_cleanup = _wait_for_thread_group([cleanup_thread] if cleanup_thread else [], per_thread_timeout=30.0, label="cleanup")
        stuck = stuck_finalize + stuck_sync + stuck_cleanup

        if stuck:
            for t in stuck:
                log.error("SHUTDOWN → thread did not finish in time: %s", getattr(t, "name", "?"))
            log.warning("SHUTDOWN → skipping storage.close() because lingering daemon threads may still touch SQLite")
        else:
            log.info("SHUTDOWN → all background threads finished cleanly")
            try:
                storage.close()
            except Exception:
                log.exception("SHUTDOWN → storage close failed")
        log.info("=== WhatsApp Watcher stopped ===")


if __name__ == "__main__":
    run()
