from __future__ import annotations

import calendar
import logging
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
        return str(row["status"]).strip().lower() in {"pending", "failed", "uploaded"}

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
        local_path = getattr(session, "recording_path", None)
        if not local_path:
            return None
        dest_rel = self.plan_destination_rel_path(session, local_path)
        upload_id = self.storage.enqueue_pending_upload(
            call_local_id=call_local_id,
            local_path=local_path,
            dest_rel_path=dest_rel,
            pc_user=getattr(session, "pc_user", None),
            machine_name=getattr(session, "machine_name", None),
            machine_ip=getattr(session, "machine_ip", None),
            max_retries=UPLOAD_RETRY_COUNT,
        )
        if not UPLOAD_ENABLED or not UPLOAD_ROOT_DIR:
            log.info("Uploader: share upload disabled; local file retained at %s", local_path)
            return None
        uploaded, error, attempts_used = self._process_one(local_path=local_path, dest_rel_path=dest_rel, retries_so_far=0)
        if uploaded:
            self.storage.mark_upload_success(upload_id, uploaded)
            return uploaded
        self.storage.mark_upload_failure(upload_id, attempts_used, error or "upload failed")
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
        )
        return True

    def process_pending_uploads(self) -> None:
        if not UPLOAD_ENABLED or not UPLOAD_ROOT_DIR:
            return
        rows = self.storage.get_pending_uploads()
        if not rows:
            return
        for row in rows:
            local_path = row["local_path"]
            dest_rel_path = row["dest_rel_path"]
            retries = int(row["retries"] or 0)
            max_retries = int(row["max_retries"] or UPLOAD_RETRY_COUNT)
            if retries >= max_retries:
                try:
                    local_file = Path(local_path)
                    if local_file.exists() and local_file.stat().st_size > 0:
                        log.info(
                            "Uploader: resetting exhausted upload id=%s | local file now has content | path=%s | size=%s bytes",
                            row["id"],
                            local_path,
                            local_file.stat().st_size,
                        )
                        self.storage.mark_upload_pending_retry(int(row["id"]))
                        retries = 0
                    else:
                        continue
                except Exception:
                    continue
            try:
                uploaded, error, attempts_used = self._process_one(local_path=local_path, dest_rel_path=dest_rel_path, retries_so_far=retries)
                if uploaded:
                    self.storage.mark_upload_success(int(row["id"]), uploaded)
                else:
                    self.storage.mark_upload_failure(int(row["id"]), attempts_used, error or "upload failed")
            except Exception as exc:
                log.exception(
                    "[UPL-001] Uploader: pending upload processing failed"
                    " | id=%s | local=%s | dest=%s",
                    row["id"], local_path, dest_rel_path,
                )
                self.storage.mark_upload_failure(int(row["id"]), retries + 1, str(exc))

    def _process_one(self, *, local_path: str, dest_rel_path: str, retries_so_far: int) -> Tuple[Optional[str], Optional[str], int]:
        src = Path(local_path)
        if not src.exists() or not src.is_file():
            # Before declaring failure: check whether the remote destination already
            # exists. This handles the stale-queue case where upload succeeded,
            # local file was deleted, and the pending row is retried later.
            if UPLOAD_ROOT_DIR:
                try:
                    dst_check = Path(UPLOAD_ROOT_DIR) / dest_rel_path
                    if dst_check.exists() and dst_check.is_file() and dst_check.stat().st_size > 0:
                        log.info(
                            "Uploader: local missing but remote exists; marking uploaded"
                            " | local=%s | remote=%s | size=%d bytes",
                            src, dst_check, dst_check.stat().st_size,
                        )
                        return str(dst_check), None, retries_so_far
                except Exception:
                    pass  # share unreachable; fall through to real [UPL-004]
            log.error("[UPL-004] Uploader: local file missing | path=%s", src)
            message = f"[UPL-004] local file missing: {src}"
            return None, message, retries_so_far + 1

        try:
            src_size = src.stat().st_size
        except OSError:
            src_size = -1

        log.info(
            "Uploader: preparing upload | src=%s | size=%s bytes | dest_rel=%s | retries_so_far=%s",
            src,
            src_size,
            dest_rel_path,
            retries_so_far,
        )

        try:
            self._ensure_share_access()
        except Exception:
            log.exception(
                "[UPL-003] Uploader: share/authentication/path access failed"
                " | dest_rel=%s",
                dest_rel_path,
            )
            raise
        root = Path(UPLOAD_ROOT_DIR)
        dst = root / dest_rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        last_error: Optional[str] = None
        attempts_used = retries_so_far
        for attempt in range(retries_so_far + 1, UPLOAD_RETRY_COUNT + 1):
            attempts_used = attempt
            try:
                temp_dst = dst.with_name(dst.name + ".partial")
                if temp_dst.exists():
                    temp_dst.unlink()
                shutil.copy2(str(src), str(temp_dst))

                try:
                    temp_size = temp_dst.stat().st_size if temp_dst.exists() else -1
                except OSError:
                    temp_size = -1

                log.info(
                    "Uploader: copied to partial | src=%s | partial=%s | partial_size=%s bytes",
                    src,
                    temp_dst,
                    temp_size,
                )

                if UPLOAD_VERIFY_COPY and not self._verify_copy(src, temp_dst):
                    raise IOError("copy verification failed")

                if UPLOAD_VERIFY_COPY:
                    log.info(
                        "Uploader: verification passed | src=%s | partial=%s",
                        src,
                        temp_dst,
                    )

                if dst.exists():
                    dst.unlink()

                temp_dst.replace(dst)
                log.info("Uploader: partial renamed to final | %s -> %s", temp_dst, dst)

                if DELETE_LOCAL_AFTER_SUCCESS and src.exists():
                    src.unlink()
                    log.info("Uploader: local source deleted after successful upload | %s", src)

                log.info("Uploader: upload successful | %s -> %s", src, dst)
                return str(dst), None, attempts_used
            except Exception as exc:
                last_error = str(exc)
                log.exception(
                    "[UPL-002] Uploader: upload attempt %s/%s failed"
                    " | local=%s | remote=%s",
                    attempt, UPLOAD_RETRY_COUNT, src, dst,
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
