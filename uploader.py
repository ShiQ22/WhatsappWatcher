from __future__ import annotations

import calendar
import logging
import os
import shutil
import subprocess
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Tuple

from config import (
    DELETE_LOCAL_AFTER_SUCCESS,
    UPLOAD_DAILY_REPORT_SUBDIR_NAME,
    UPLOAD_ENABLED,
    UPLOAD_RETRY_COUNT,
    UPLOAD_RETRY_DELAY_SECONDS,
    UPLOAD_ROOT_DIR,
    UPLOAD_SHARE_AUTH_ENABLED,
    UPLOAD_SHARE_PASSWORD,
    UPLOAD_SHARE_USERNAME,
    UPLOAD_RECORDINGS_SUBDIR_NAME,
    UPLOAD_VERIFY_COPY,
)

log = logging.getLogger("watcher.uploader")

_STALE_PARTIAL_AGE_SECONDS = 7200  # 2 hours


class UploadManager:
    def __init__(self, storage) -> None:
        self.storage = storage
        self._share_auth_attempted = False
        self._share_auth_ok = False

    def _get_share_base(self) -> Optional[str]:
        root = str(UPLOAD_ROOT_DIR or "").strip().replace("/", "\\")
        if not root.startswith("\\"):
            return None
        parts = [p for p in root.split("\\") if p]
        if len(parts) < 2:
            return None
        return f"\\\\{parts[0]}\\{parts[1]}"

    def _ensure_share_access(self) -> None:
        if not UPLOAD_ENABLED or not UPLOAD_ROOT_DIR or not UPLOAD_SHARE_AUTH_ENABLED:
            return
        if self._share_auth_attempted and self._share_auth_ok:
            return
        share_base = self._get_share_base()
        if not share_base:
            raise RuntimeError(f"Invalid UPLOAD_ROOT_DIR for share auth: {UPLOAD_ROOT_DIR}")
        if not UPLOAD_SHARE_USERNAME or not UPLOAD_SHARE_PASSWORD:
            raise RuntimeError("Share auth is enabled but username/password are missing")
        startupinfo = None
        if hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0

        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        cmd = ["net", "use", share_base, UPLOAD_SHARE_PASSWORD, f"/user:{UPLOAD_SHARE_USERNAME}", "/persistent:no"]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            startupinfo=startupinfo,
            creationflags=flags,
            timeout=15,
        )
        if result.returncode != 0:
            subprocess.run(
                ["net", "use", share_base, "/delete", "/y"],
                capture_output=True,
                text=True,
                startupinfo=startupinfo,
                creationflags=flags,
                timeout=15,
            )
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                startupinfo=startupinfo,
                creationflags=flags,
                timeout=15,
            )
        self._share_auth_attempted = True
        if result.returncode != 0:
            msg = (result.stderr or result.stdout or "unknown share authentication error").strip()
            self._share_auth_ok = False
            raise RuntimeError(f"Share authentication failed for {share_base}: {msg}")
        self._share_auth_ok = True
        log.info("Uploader: authenticated to share %s", share_base)

    def _already_queued_or_uploaded(self, local_path: str, dest_rel_path: str) -> bool:
        row = self.storage.get_upload_by_local_and_dest(local_path, dest_rel_path)
        if not row:
            return False
        return str(row["status"]).strip().lower() in {"pending", "failed", "uploaded", "processing"}

    def _date_parts(self, when: Optional[datetime]) -> Tuple[str, str, str]:
        when = when or datetime.now()
        return when.strftime("%Y"), calendar.month_name[when.month], when.strftime("%d-%m-%Y")

    def plan_destination_rel_path(self, session, local_path: str) -> str:
        local_name = Path(local_path).name
        pc_user = getattr(session, "pc_user", None) or "unknown"
        when = getattr(session, "started_at", None) or datetime.now()
        year, month_name, day_folder = self._date_parts(when)
        base = Path(year) / month_name / day_folder / pc_user
        if UPLOAD_RECORDINGS_SUBDIR_NAME:
            base = base / UPLOAD_RECORDINGS_SUBDIR_NAME
        return str(base / local_name)

    def plan_report_destination_rel_path(self, *, report_date: date, pc_user: str, local_name: str) -> str:
        when = datetime.combine(report_date, datetime.min.time())
        year, month_name, day_folder = self._date_parts(when)
        return str(Path(year) / month_name / day_folder / pc_user / UPLOAD_DAILY_REPORT_SUBDIR_NAME / local_name)

    def upload_for_session(self, session, call_local_id: Optional[int]) -> Optional[str]:
        """
        Called from the finalize thread immediately after a recording is ready.

        The row is inserted with status='processing' so the background pending
        scan never sees it as 'pending' — eliminating the duplicate-upload race.
        If upload is disabled the row is inserted as 'pending' for later retry.
        """
        local_path = getattr(session, "recording_path", None)
        if not local_path:
            return None
        dest_rel = self.plan_destination_rel_path(session, local_path)

        if not UPLOAD_ENABLED or not UPLOAD_ROOT_DIR:
            # Disabled — enqueue as pending so a later run can upload it.
            upload_id = self.storage.enqueue_pending_upload(
                call_local_id=call_local_id,
                local_path=local_path,
                dest_rel_path=dest_rel,
                pc_user=getattr(session, "pc_user", None),
                machine_name=getattr(session, "machine_name", None),
                machine_ip=getattr(session, "machine_ip", None),
                max_retries=UPLOAD_RETRY_COUNT,
                initial_status="pending",
            )
            log.info(
                "Uploader [FINALIZE-PATH]: share upload disabled; enqueued for later"
                " | upload_id=%s | call_local_id=%s | local=%s",
                upload_id, call_local_id, local_path,
            )
            return None

        # Insert as 'processing' — background scan only picks up 'pending'/'failed'.
        # This single INSERT is the ownership gate; no separate claim step is needed.
        upload_id = self.storage.enqueue_pending_upload(
            call_local_id=call_local_id,
            local_path=local_path,
            dest_rel_path=dest_rel,
            pc_user=getattr(session, "pc_user", None),
            machine_name=getattr(session, "machine_name", None),
            machine_ip=getattr(session, "machine_ip", None),
            max_retries=UPLOAD_RETRY_COUNT,
            initial_status="processing",
        )
        log.info(
            "Uploader [FINALIZE-PATH]: starting direct upload"
            " | upload_id=%s | call_local_id=%s | local=%s | dest_rel=%s",
            upload_id, call_local_id, local_path, dest_rel,
        )

        uploaded, error, attempts_used = self._process_one(
            upload_id=upload_id,
            local_path=local_path,
            dest_rel_path=dest_rel,
            retries_so_far=0,
            path_label="FINALIZE-PATH",
        )
        if uploaded:
            self.storage.mark_upload_success(upload_id, uploaded)
            if call_local_id:
                self.storage.update_uploaded_path(call_local_id, uploaded)
            log.info(
                "Uploader [FINALIZE-PATH]: upload succeeded"
                " | upload_id=%s | call_local_id=%s | remote=%s",
                upload_id, call_local_id, uploaded,
            )
            return uploaded

        self.storage.mark_upload_failure(upload_id, attempts_used, error or "upload failed")
        log.warning(
            "Uploader [FINALIZE-PATH]: upload failed; row left for retry by pending scan"
            " | upload_id=%s | call_local_id=%s | error=%s",
            upload_id, call_local_id, error,
        )
        return None

    def enqueue_report_file(self, report_path: Path, report_date: date) -> bool:
        if not report_path.exists() or not report_path.is_file():
            return False
        pc_user = self._extract_username_from_log(report_path)
        dest_rel = self.plan_report_destination_rel_path(report_date=report_date, pc_user=pc_user, local_name=report_path.name)
        if self._already_queued_or_uploaded(str(report_path), dest_rel):
            return False
        self.storage.enqueue_pending_upload(
            call_local_id=None,
            local_path=str(report_path),
            dest_rel_path=dest_rel,
            pc_user=pc_user,
            machine_name=None,
            machine_ip=None,
            max_retries=UPLOAD_RETRY_COUNT,
            initial_status="pending",
        )
        return True

    def process_pending_uploads(self) -> None:
        """
        Background scan — claims pending/failed rows atomically before processing.

        After each successful upload, updates the linked call row's uploaded_path
        so central sync reflects the upload regardless of which path uploaded it.
        """
        if not UPLOAD_ENABLED or not UPLOAD_ROOT_DIR:
            return

        # Reset rows stuck in 'processing' for longer than 15 minutes.
        # This recovers rows where the process was killed mid-upload.
        reset_count = self.storage.reset_stale_processing_uploads(stale_after_seconds=900)
        if reset_count:
            log.info(
                "Uploader [PENDING-PATH]: reset %s stale processing row(s) to pending",
                reset_count,
            )

        rows = self.storage.get_pending_uploads()
        if not rows:
            return

        for row in rows:
            upload_id = int(row["id"])
            local_path = row["local_path"]
            dest_rel_path = row["dest_rel_path"]
            call_local_id = row["call_local_id"]
            retries = int(row["retries"] or 0)
            max_retries = int(row["max_retries"] or UPLOAD_RETRY_COUNT)

            if retries >= max_retries:
                try:
                    local_file = Path(local_path)
                    if local_file.exists() and local_file.stat().st_size > 0:
                        log.info(
                            "Uploader [PENDING-PATH]: resetting exhausted upload;"
                            " file now has content"
                            " | upload_id=%s | local=%s | size=%s bytes",
                            upload_id, local_path, local_file.stat().st_size,
                        )
                        self.storage.mark_upload_pending_retry(upload_id)
                        retries = 0
                    else:
                        continue
                except Exception:
                    continue

            # Atomically claim the row — only one worker can win per row.
            if not self.storage.claim_upload_for_processing(upload_id):
                log.debug(
                    "Uploader [PENDING-PATH]: row already claimed by another worker"
                    " | upload_id=%s",
                    upload_id,
                )
                continue

            log.info(
                "Uploader [PENDING-PATH]: claimed upload for processing"
                " | upload_id=%s | call_local_id=%s | local=%s"
                " | dest_rel=%s | retries=%s/%s",
                upload_id, call_local_id, local_path,
                dest_rel_path, retries, max_retries,
            )

            try:
                uploaded, error, attempts_used = self._process_one(
                    upload_id=upload_id,
                    local_path=local_path,
                    dest_rel_path=dest_rel_path,
                    retries_so_far=retries,
                    path_label="PENDING-PATH",
                )
                if uploaded:
                    self.storage.mark_upload_success(upload_id, uploaded)
                    if call_local_id:
                        # update_uploaded_path already marks central_synced=0
                        self.storage.update_uploaded_path(int(call_local_id), uploaded)
                        log.info(
                            "Uploader [PENDING-PATH]: updated call uploaded_path"
                            " | upload_id=%s | call_local_id=%s | remote=%s",
                            upload_id, call_local_id, uploaded,
                        )
                else:
                    self.storage.mark_upload_failure(
                        upload_id, attempts_used, error or "upload failed"
                    )
            except Exception as exc:
                log.exception(
                    "[UPL-001] Uploader [PENDING-PATH]: processing raised exception"
                    " | upload_id=%s | call_local_id=%s | local=%s | dest_rel=%s",
                    upload_id, call_local_id, local_path, dest_rel_path,
                )
                self.storage.mark_upload_failure(upload_id, retries + 1, str(exc))

    def _process_one(
        self,
        *,
        upload_id: int,
        local_path: str,
        dest_rel_path: str,
        retries_so_far: int,
        path_label: str = "UNKNOWN-PATH",
    ) -> Tuple[Optional[str], Optional[str], int]:
        src = Path(local_path)
        try:
            src_exists = src.exists() and src.is_file()
            src_size = src.stat().st_size if src_exists else -1
        except OSError:
            src_exists = False
            src_size = -1

        log.info(
            "Uploader [%s]: preparing | upload_id=%s | retries_so_far=%s"
            " | src=%s | src_exists=%s | src_size=%s bytes | dest_rel=%s",
            path_label, upload_id, retries_so_far,
            src, src_exists, src_size, dest_rel_path,
        )

        if not src_exists:
            # Before declaring failure: check if the remote destination already
            # exists — handles stale-queue case where upload succeeded, local was
            # deleted, and the row is retried by the pending scan.
            if UPLOAD_ROOT_DIR:
                try:
                    dst_check = Path(UPLOAD_ROOT_DIR) / dest_rel_path
                    dst_check_exists = dst_check.exists() and dst_check.is_file()
                    dst_check_size = dst_check.stat().st_size if dst_check_exists else -1
                    if dst_check_exists and dst_check_size > 0:
                        log.info(
                            "Uploader [%s]: local missing but remote exists — marking uploaded"
                            " | upload_id=%s | local=%s | remote=%s | remote_size=%d bytes",
                            path_label, upload_id, src, dst_check, dst_check_size,
                        )
                        return str(dst_check), None, retries_so_far
                except Exception:
                    pass  # share unreachable; fall through to [UPL-004]
            log.error(
                "[UPL-004] Uploader [%s]: local file missing"
                " | upload_id=%s | path=%s",
                path_label, upload_id, src,
            )
            return None, f"[UPL-004] local file missing: {src}", retries_so_far + 1

        try:
            self._ensure_share_access()
        except Exception:
            log.exception(
                "[UPL-003] Uploader [%s]: share auth/access failed"
                " | upload_id=%s | dest_rel=%s",
                path_label, upload_id, dest_rel_path,
            )
            raise

        root = Path(UPLOAD_ROOT_DIR)
        dst = root / dest_rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)

        last_error: Optional[str] = None
        attempts_used = retries_so_far

        for attempt_num in range(retries_so_far + 1, UPLOAD_RETRY_COUNT + 1):
            attempts_used = attempt_num

            # Unique partial name per upload-id + attempt + pid.
            # Two concurrent workers on the same file will never collide.
            temp_dst = dst.with_name(
                f"{dst.name}.upload-{upload_id}"
                f".attempt-{attempt_num}"
                f".pid-{os.getpid()}.partial"
            )

            # Remove stale partials (> 2 h old) for this destination.
            # Wrapped in try/except — a cleanup failure must never block the upload.
            try:
                stale_cutoff = time.time() - _STALE_PARTIAL_AGE_SECONDS
                for old_partial in dst.parent.glob(f"{dst.name}.*.partial"):
                    try:
                        if old_partial.stat().st_mtime < stale_cutoff:
                            old_partial.unlink()
                            log.debug(
                                "Uploader [%s]: removed stale partial | upload_id=%s | file=%s",
                                path_label, upload_id, old_partial.name,
                            )
                    except Exception:
                        pass
            except Exception:
                log.warning(
                    "Uploader [%s]: stale partial cleanup failed (non-fatal)"
                    " | upload_id=%s | dst=%s",
                    path_label, upload_id, dst,
                )

            try:
                if temp_dst.exists():
                    temp_dst.unlink()

                log.info(
                    "Uploader [%s]: copying to partial"
                    " | upload_id=%s | attempt=%s/%s"
                    " | src=%s | partial=%s",
                    path_label, upload_id, attempt_num, UPLOAD_RETRY_COUNT,
                    src, temp_dst.name,
                )
                shutil.copy2(str(src), str(temp_dst))

                try:
                    temp_size = temp_dst.stat().st_size if temp_dst.exists() else -1
                except OSError:
                    temp_size = -1

                log.info(
                    "Uploader [%s]: partial copied | upload_id=%s | partial_size=%s bytes",
                    path_label, upload_id, temp_size,
                )

                if UPLOAD_VERIFY_COPY and not self._verify_copy(src, temp_dst):
                    raise IOError("copy verification failed")

                if UPLOAD_VERIFY_COPY:
                    log.info(
                        "Uploader [%s]: verification passed | upload_id=%s",
                        path_label, upload_id,
                    )

                if dst.exists():
                    dst.unlink()

                temp_dst.replace(dst)
                log.info(
                    "Uploader [%s]: partial renamed to final"
                    " | upload_id=%s | %s -> %s",
                    path_label, upload_id, temp_dst.name, dst,
                )

                if DELETE_LOCAL_AFTER_SUCCESS:
                    try:
                        if src.exists():
                            src.unlink()
                            log.info(
                                "Uploader [%s]: local deleted after successful upload"
                                " | upload_id=%s | src=%s",
                                path_label, upload_id, src,
                            )
                    except Exception:
                        log.warning(
                            "Uploader [%s]: local delete failed (non-fatal)"
                            " | upload_id=%s | src=%s",
                            path_label, upload_id, src,
                        )

                log.info(
                    "Uploader [%s]: upload successful | upload_id=%s | %s -> %s",
                    path_label, upload_id, src, dst,
                )
                return str(dst), None, attempts_used

            except Exception as exc:
                last_error = str(exc)
                log.exception(
                    "[UPL-002] Uploader [%s]: attempt %s/%s failed"
                    " | upload_id=%s | local=%s | remote=%s",
                    path_label, attempt_num, UPLOAD_RETRY_COUNT, upload_id, src, dst,
                )
                time.sleep(UPLOAD_RETRY_DELAY_SECONDS)

        return None, last_error, attempts_used

    @staticmethod
    def _verify_copy(src: Path, dst: Path) -> bool:
        try:
            src_size = src.stat().st_size
        except OSError:
            return False

        if src_size <= 0:
            return False

        stable_matches = 0
        last_size = -1

        for _ in range(20):
            try:
                if not dst.exists():
                    stable_matches = 0
                else:
                    dst_size = dst.stat().st_size

                    if dst_size == src_size and dst_size > 0:
                        if dst_size == last_size:
                            stable_matches += 1
                        else:
                            stable_matches = 1
                        if stable_matches >= 3:
                            return True
                    else:
                        stable_matches = 0

                    last_size = dst_size
            except OSError:
                stable_matches = 0

            time.sleep(0.5)

        return False

    @staticmethod
    def _extract_username_from_log(report_path: Path) -> str:
        try:
            with report_path.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if "user=" in line:
                        token = line.split("user=", 1)[1].split()[0]
                        if token:
                            return token.strip()
        except Exception:
            pass
        return "unknown"
