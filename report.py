from __future__ import annotations

import getpass
import os
import re
import shutil
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from config import REPORTS_DIR, UPLOAD_DAILY_REPORT_ENABLED, UPLOAD_DAILY_REPORT_FILENAME_PREFIX


_REPORT_IO_LOCK = threading.Lock()


class DailyCallReporter:
    def __init__(self, reports_dir: Optional[Path] = None) -> None:
        self.reports_dir = Path(reports_dir or REPORTS_DIR)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def append_call(self, session) -> None:
        started_at = getattr(session, "started_at", None) or datetime.now()
        ended_at = getattr(session, "ended_at", None)
        report_path = self.get_report_path_for_date(started_at.date())
        date_str = started_at.strftime("%Y-%m-%d")
        start_time_str = started_at.strftime("%I:%M:%S %p")
        end_time_str = ended_at.strftime("%I:%M:%S %p") if ended_at else "-"

        pc_user = _clean(getattr(session, "pc_user", None) or getpass.getuser())
        machine_name = _clean(getattr(session, "machine_name", None) or os.environ.get("COMPUTERNAME", "-"))
        machine_ip = _clean(getattr(session, "machine_ip", None) or "-")
        direction = _clean(getattr(session, "direction", None) or "unknown")
        raw_status = _clean(getattr(session, "status", None) or "unknown")
        caller_number = _clean(getattr(session, "caller_number", None) or "-")
        recording = _clean(getattr(session, "recording_path", None) or "none")
        uploaded = _clean(getattr(session, "uploaded_path", None) or "none")
        error = _clean(getattr(session, "error_details", None) or "-")
        duration = int(session.duration_seconds()) if hasattr(session, "duration_seconds") else 0

        line = (
            f"[{start_time_str}]  user={pc_user}  machine={machine_name}  ip={machine_ip}  "
            f"direction={direction}  duration={duration}s  started={start_time_str}  ended={end_time_str}  "
            f"number={caller_number}  file={recording}  uploaded={uploaded}  error={error}  status={raw_status}"
        )
        with _REPORT_IO_LOCK:
            self._ensure_date_header(report_path, date_str)
            with report_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def get_report_path_for_date(self, report_date: date) -> Path:
        return self.reports_dir / f"{UPLOAD_DAILY_REPORT_FILENAME_PREFIX}-{report_date.strftime('%Y-%m-%d')}.log"

    def enqueue_closed_reports(self, uploader, now: Optional[datetime] = None) -> int:
        if not UPLOAD_DAILY_REPORT_ENABLED:
            return 0
        current_date = (now or datetime.now()).date()
        queued = 0
        for path in sorted(self.reports_dir.glob(f"{UPLOAD_DAILY_REPORT_FILENAME_PREFIX}-*.log")):
            report_date = _extract_report_date(path)
            if report_date is None or report_date >= current_date:
                continue
            if uploader.enqueue_report_file(path, report_date):
                queued += 1
        return queued

    @staticmethod
    def _ensure_date_header(report_path: Path, date_str: str) -> None:
        marker = f"DATE: {date_str}"
        header = f"{'=' * 72}\n{marker}\n{'=' * 72}\n"
        if not report_path.exists() or report_path.stat().st_size == 0:
            with report_path.open("a", encoding="utf-8") as f:
                f.write(header)
            return
        try:
            existing = report_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            existing = ""
        if marker not in existing:
            with report_path.open("a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(header)


def rename_recording_for_session(session, part_index: Optional[int] = None, part_count: Optional[int] = None) -> Optional[str]:
    current_path = getattr(session, "recording_path", None)
    if not current_path or str(current_path).lower() == "none":
        return None
    src = Path(current_path)
    if not src.exists() or not src.is_file():
        return None

    started_at = getattr(session, "started_at", None) or datetime.now()
    pc_user = getattr(session, "pc_user", None) or getpass.getuser()
    direction = getattr(session, "direction", None) or "unknown"
    caller_number = getattr(session, "caller_number", None)
    final_name = _build_filename(
        started_at=started_at,
        pc_user=pc_user,
        direction=direction,
        caller_number=caller_number,
        extension=src.suffix or ".mp4",
        part_index=part_index,
        part_count=part_count,
    )
    dst = _dedupe(src.with_name(final_name))
    try:
        if src.resolve() == dst.resolve():
            return str(src)
    except Exception:
        pass
    try:
        shutil.move(str(src), str(dst))
        return str(dst)
    except Exception:
        return None


def _extract_report_date(path: Path) -> Optional[date]:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _build_filename(*, started_at: datetime, pc_user: str, direction: str, caller_number: Optional[str] = None, extension: str = ".mp4", part_index: Optional[int] = None, part_count: Optional[int] = None) -> str:
    user_clean = _slug(pc_user or "unknown")
    dir_clean = _slug(direction or "unknown")
    timestamp = started_at.strftime("%Y-%m-%d_%H-%M-%S")
    ext = extension if extension.startswith(".") else f".{extension}"
    lead = f"{_normalize_phone(caller_number)}-{user_clean}" if caller_number else user_clean
    part_suffix = f"-part{part_index}" if (part_count or 0) > 1 and part_index else ""
    return f"{lead}-{dir_clean}-{timestamp}{part_suffix}{ext.lower()}"


def _clean(value) -> str:
    return str(value).strip() or "-"


def _slug(value: Optional[str]) -> str:
    text = str(value or "").strip()
    text = re.sub(r'[<>:"/\\|?*]+', "", text)
    text = re.sub(r"\s+", "-", text)
    return text.strip("-_. ") or "unknown"


def _normalize_phone(value: Optional[str]) -> str:
    text = str(value or "").strip().replace("+", "00")
    return re.sub(r"[^\d]", "", text)


def _dedupe(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    counter = 1
    while True:
        candidate = path.with_name(f"{stem}_{counter}{suffix}")
        if not candidate.exists():
            return candidate
        counter += 1
