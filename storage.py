from __future__ import annotations

import logging
import sqlite3
import threading
import getpass
import platform
import socket
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import CENTRAL_DB_DRIVER, CENTRAL_DB_ENABLED, CENTRAL_DB_MYSQL_URI, CENTRAL_DB_TIMEOUT_SECONDS, DB_PATH

log = logging.getLogger("watcher.storage")

LOCAL_CALLS_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time TEXT,
    end_time TEXT,
    duration INTEGER,
    status TEXT,
    direction TEXT,
    pc_user TEXT,
    machine_name TEXT,
    machine_ip TEXT,
    caller_number TEXT,
    recording_path TEXT,
    uploaded_path TEXT,
    error_details TEXT,
    central_synced INTEGER DEFAULT 0,
    central_synced_at TEXT,
    central_sync_error TEXT
)
"""

PENDING_UPLOADS_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_local_id INTEGER,
    local_path TEXT NOT NULL,
    dest_rel_path TEXT NOT NULL,
    pc_user TEXT,
    machine_name TEXT,
    machine_ip TEXT,
    retries INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    last_error TEXT,
    uploaded_path TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    processing_at TEXT
)
"""

LOCAL_APP_INFO_LOGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_info_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_time TEXT,
    level TEXT,
    logger_name TEXT,
    event_type TEXT,
    message TEXT,
    pc_user TEXT,
    machine_name TEXT,
    machine_ip TEXT,
    call_local_id INTEGER,
    central_synced INTEGER DEFAULT 0,
    central_synced_at TEXT,
    central_sync_error TEXT
)
"""

LOCAL_APP_ERROR_LOGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_error_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_time TEXT,
    level TEXT,
    logger_name TEXT,
    event_type TEXT,
    message TEXT,
    exception_type TEXT,
    stack_trace TEXT,
    pc_user TEXT,
    machine_name TEXT,
    machine_ip TEXT,
    call_local_id INTEGER,
    central_synced INTEGER DEFAULT 0,
    central_synced_at TEXT,
    central_sync_error TEXT
)
"""

LOCAL_CALL_INSERT_SQL = """
INSERT INTO calls (
    start_time, end_time, duration, status, direction,
    pc_user, machine_name, machine_ip, caller_number,
    recording_path, uploaded_path, error_details
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

CENTRAL_CALL_SCHEMA = """
CREATE TABLE IF NOT EXISTS call_logs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    source_call_uid VARCHAR(255) NULL,
    start_time DATETIME NULL,
    end_time DATETIME NULL,
    duration INT NULL,
    status VARCHAR(50) NULL,
    direction VARCHAR(20) NULL,
    pc_user VARCHAR(100) NULL,
    machine_name VARCHAR(100) NULL,
    machine_ip VARCHAR(50) NULL,
    caller_number VARCHAR(50) NULL,
    recording_path TEXT NULL,
    uploaded_path TEXT NULL,
    error_details TEXT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

CENTRAL_INFO_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_info_logs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    log_time DATETIME NULL,
    level VARCHAR(20) NULL,
    logger_name VARCHAR(100) NULL,
    event_type VARCHAR(100) NULL,
    message TEXT NULL,
    pc_user VARCHAR(100) NULL,
    machine_name VARCHAR(100) NULL,
    machine_ip VARCHAR(50) NULL,
    call_local_id BIGINT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

CENTRAL_ERROR_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_error_logs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    log_time DATETIME NULL,
    level VARCHAR(20) NULL,
    logger_name VARCHAR(100) NULL,
    event_type VARCHAR(100) NULL,
    message TEXT NULL,
    exception_type VARCHAR(200) NULL,
    stack_trace LONGTEXT NULL,
    pc_user VARCHAR(100) NULL,
    machine_name VARCHAR(100) NULL,
    machine_ip VARCHAR(50) NULL,
    call_local_id BIGINT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

CENTRAL_CALL_UPSERT = """
INSERT INTO call_logs (
    source_call_uid,
    start_time, end_time, duration, status, direction,
    pc_user, machine_name, machine_ip, caller_number,
    recording_path, uploaded_path, error_details
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    start_time=VALUES(start_time),
    end_time=VALUES(end_time),
    duration=VALUES(duration),
    status=VALUES(status),
    direction=VALUES(direction),
    pc_user=VALUES(pc_user),
    machine_name=VALUES(machine_name),
    machine_ip=VALUES(machine_ip),
    caller_number=VALUES(caller_number),
    recording_path=VALUES(recording_path),
    uploaded_path=VALUES(uploaded_path),
    error_details=VALUES(error_details)
"""

CENTRAL_INFO_INSERT = """
INSERT INTO app_info_logs (
    log_time, level, logger_name, event_type, message,
    pc_user, machine_name, machine_ip, call_local_id
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

CENTRAL_ERROR_INSERT = """
INSERT INTO app_error_logs (
    log_time, level, logger_name, event_type, message,
    exception_type, stack_trace, pc_user, machine_name, machine_ip, call_local_id
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


class Storage:
    """
    Local SQLite is always the source of truth.
    Central MySQL (via SQLAlchemy) is eventually consistent.

    Insert order:
      1. Save to local SQLite (always succeeds).
      2. Try immediate central insert via a fresh pooled connection.
      3. If central fails, mark row as unsynced for background retry.
    """

    def __init__(self) -> None:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._central_lock = threading.Lock()
        self.local_conn = sqlite3.connect(
            str(DB_PATH), check_same_thread=False, isolation_level=None
        )
        self.local_conn.execute("PRAGMA journal_mode=WAL")
        self.local_conn.execute("PRAGMA busy_timeout=5000")
        self.local_conn.row_factory = sqlite3.Row
        self._create_local_tables()
        self._migrate_local_tables()
        self._cached_identity = self._compute_machine_identity()
        self._closed = False

        # Central DB engine (pool — connections are obtained per-operation)
        self._central_engine = None
        if CENTRAL_DB_ENABLED:
            self._init_central_engine()

    # ── Local schema ─────────────────────────────────────────────────────────

    def _create_local_tables(self) -> None:
        cur = self.local_conn.cursor()
        cur.execute(LOCAL_CALLS_SCHEMA)
        cur.execute(PENDING_UPLOADS_SCHEMA)
        cur.execute(LOCAL_APP_INFO_LOGS_SCHEMA)
        cur.execute(LOCAL_APP_ERROR_LOGS_SCHEMA)
        self.local_conn.commit()

    def _migrate_local_tables(self) -> None:
        """Remove legacy answer_state column and add any missing columns."""
        with self._lock:
            cols = [
                row[1]
                for row in self.local_conn.execute("PRAGMA table_info(calls)").fetchall()
            ]
            if "answer_state" in cols:
                log.info("STORAGE → migrating local DB: removing answer_state column")
                self.local_conn.execute(
                    "ALTER TABLE calls RENAME TO calls_legacy_with_answer_state"
                )
                self.local_conn.execute(LOCAL_CALLS_SCHEMA)
                self.local_conn.execute(
                    """
                    INSERT INTO calls (
                        id, start_time, end_time, duration, status, direction,
                        pc_user, machine_name, machine_ip, caller_number,
                        recording_path, uploaded_path, error_details,
                        central_synced, central_synced_at, central_sync_error
                    )
                    SELECT
                        id, start_time, end_time, duration,
                        COALESCE(NULLIF(status, ''), 'ended'),
                        COALESCE(NULLIF(direction, ''), 'unknown'),
                        pc_user, machine_name, machine_ip, caller_number,
                        recording_path, uploaded_path, error_details,
                        central_synced, central_synced_at, central_sync_error
                    FROM calls_legacy_with_answer_state
                    """
                )
                self.local_conn.execute("DROP TABLE calls_legacy_with_answer_state")
                self.local_conn.commit()
                log.info("STORAGE → local DB migration complete")

        # Add processing_at to pending_uploads for existing DBs that pre-date this column.
        with self._lock:
            pu_cols = [
                row[1]
                for row in self.local_conn.execute(
                    "PRAGMA table_info(pending_uploads)"
                ).fetchall()
            ]
            if "processing_at" not in pu_cols:
                log.info(
                    "STORAGE → migrating pending_uploads: adding processing_at column"
                )
                self.local_conn.execute(
                    "ALTER TABLE pending_uploads ADD COLUMN processing_at TEXT"
                )
                self.local_conn.commit()
                log.info("STORAGE → pending_uploads migration complete")

    # ── Central DB (SQLAlchemy pool) ─────────────────────────────────────────

    def _init_central_engine(self) -> None:
        """Create the SQLAlchemy engine and bootstrap the schema once."""
        if not CENTRAL_DB_ENABLED or CENTRAL_DB_DRIVER != "sqlalchemy" or not CENTRAL_DB_MYSQL_URI:
            return
        with self._central_lock:
            old_engine = self._central_engine
            try:
                from sqlalchemy import create_engine, text

                engine = create_engine(
                    CENTRAL_DB_MYSQL_URI,
                    pool_pre_ping=True,
                    pool_recycle=1800,
                    pool_timeout=max(int(CENTRAL_DB_TIMEOUT_SECONDS), 5),
                    pool_size=3,
                    max_overflow=2,
                    connect_args={"connect_timeout": int(CENTRAL_DB_TIMEOUT_SECONDS)},
                )
                with engine.connect() as conn:
                    conn.execute(text(CENTRAL_CALL_SCHEMA))

                    try:
                        conn.execute(text("ALTER TABLE call_logs ADD COLUMN source_call_uid VARCHAR(255) NULL"))
                        conn.commit()
                        log.info("STORAGE → added source_call_uid to central call_logs")
                    except Exception:
                        pass

                    try:
                        conn.execute(text("CREATE UNIQUE INDEX uq_call_logs_source_call_uid ON call_logs (source_call_uid)"))
                        conn.commit()
                        log.info("STORAGE → created source_call_uid unique index on central call_logs")
                    except Exception:
                        pass

                    conn.execute(text(CENTRAL_INFO_SCHEMA))
                    conn.execute(text(CENTRAL_ERROR_SCHEMA))
                    try:
                        conn.execute(text("ALTER TABLE call_logs DROP COLUMN answer_state"))
                        conn.commit()
                        log.info("STORAGE → dropped legacy answer_state from central call_logs")
                    except Exception:
                        pass
                    conn.commit()

                self._central_engine = engine
                if old_engine is not None and old_engine is not engine:
                    try:
                        old_engine.dispose()
                    except Exception:
                        pass
                log.info(
                    "STORAGE → central DB connected via SQLAlchemy (URI: %s)",
                    CENTRAL_DB_MYSQL_URI.split("@")[-1],
                )
            except Exception:
                log.exception("STORAGE → central DB connection failed; local-only mode")
                self._central_engine = None

    def _central_available(self) -> bool:
        return self._central_engine is not None

    def _central_execute(self, sql: str, params: tuple) -> bool:
        if not self._central_available():
            return False
        try:
            engine = self._central_engine
            if engine is None:
                return False
            with engine.connect() as conn:
                conn.exec_driver_sql(sql, params)
                conn.commit()
            return True
        except Exception as exc:
            log.warning("STORAGE → central insert failed: %s", exc)
            try:
                self._init_central_engine()
            except Exception:
                pass
            return False

    def _central_execute_many(self, sql: str, rows: list[tuple]) -> int:
        if not self._central_available() or not rows:
            return 0
        ok_count = 0
        try:
            engine = self._central_engine
            if engine is None:
                return 0
            with engine.connect() as conn:
                for params in rows:
                    try:
                        conn.exec_driver_sql(sql, params)
                        ok_count += 1
                    except Exception as row_exc:
                        log.warning("STORAGE → central row insert failed: %s", row_exc)
                conn.commit()
        except Exception as exc:
            log.warning("STORAGE → central batch failed: %s", exc)
            try:
                self._init_central_engine()
            except Exception:
                pass
        return ok_count

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _compute_machine_identity(self) -> tuple[str, str, str]:
        pc_user = getpass.getuser()
        machine_name = platform.node() or "-"
        machine_ip = "-"
        try:
            hostname = socket.gethostname()
            addresses = socket.getaddrinfo(hostname, None, family=socket.AF_INET)
            for addr in addresses:
                ip = addr[4][0]
                if ip and not ip.startswith("127."):
                    machine_ip = ip
                    break
        except Exception:
            pass
        return pc_user, machine_name, machine_ip

    def _machine_identity(self) -> tuple[str, str, str]:
        return self._cached_identity

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Storage is closed")

    def _dt(self, value: Optional[datetime]) -> Optional[str]:
        return value.strftime("%Y-%m-%d %H:%M:%S") if value else None

    def _call_params(self, session) -> tuple:
        return (
            self._dt(getattr(session, "started_at", None)),
            self._dt(getattr(session, "ended_at", None)),
            session.duration_seconds() if hasattr(session, "duration_seconds") else 0,
            getattr(session, "status", None),
            getattr(session, "direction", None),
            getattr(session, "pc_user", None),
            getattr(session, "machine_name", None),
            getattr(session, "machine_ip", None),
            getattr(session, "caller_number", None),
            getattr(session, "recording_path", None),
            getattr(session, "uploaded_path", None),
            getattr(session, "error_details", None),
        )

    def _call_params_from_row(self, row) -> tuple:
        return (
            row["start_time"], row["end_time"], row["duration"],
            row["status"], row["direction"], row["pc_user"],
            row["machine_name"], row["machine_ip"], row["caller_number"],
            row["recording_path"], row["uploaded_path"], row["error_details"],
        )

    def _source_call_uid(self, local_id: int, machine_name: Optional[str], pc_user: Optional[str] = None) -> str:
        fallback_user, fallback_machine, _ = self._machine_identity()
        safe_machine = machine_name or fallback_machine or "unknown-machine"
        safe_user = pc_user or fallback_user or "unknown-user"
        return f"{safe_machine}:{safe_user}:{local_id}"

    # ── Public write API ─────────────────────────────────────────────────────

    def save_call(self, session) -> int:
        """Save call to local DB first, then try central immediately."""
        self._ensure_open()
        params = self._call_params(session)
        with self._lock:
            cur = self.local_conn.cursor()
            cur.execute(LOCAL_CALL_INSERT_SQL, params)
            self.local_conn.commit()
            local_id = int(cur.lastrowid)
        log.debug("STORAGE → call saved locally (id=%s)", local_id)

        if self._central_available():
            central_params = (self._source_call_uid(local_id, getattr(session, "machine_name", None), getattr(session, "pc_user", None)),) + params
            success = self._central_execute(CENTRAL_CALL_UPSERT, central_params)
            if success:
                self._mark_synced("calls", local_id)
                log.debug("STORAGE → call synced to central (local_id=%s)", local_id)
            else:
                self._mark_unsynced("calls", local_id, "immediate central insert failed")
        return local_id

    def save_info_log(
        self,
        *,
        level: Optional[str],
        logger_name: Optional[str],
        event_type: Optional[str],
        message: Optional[str],
        call_local_id: Optional[int] = None,
    ) -> int:
        self._ensure_open()
        pc_user, machine_name, machine_ip = self._machine_identity()
        params = (
            self._dt(datetime.now()), level, logger_name, event_type, message,
            pc_user, machine_name, machine_ip, call_local_id,
        )
        with self._lock:
            cur = self.local_conn.cursor()
            cur.execute(
                "INSERT INTO app_info_logs "
                "(log_time, level, logger_name, event_type, message, "
                "pc_user, machine_name, machine_ip, call_local_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                params,
            )
            self.local_conn.commit()
            local_id = int(cur.lastrowid)

        if self._central_available():
            self._mark_unsynced("app_info_logs", local_id, "queued for background sync")
        return local_id

    def save_error_log(
        self,
        *,
        level: Optional[str],
        logger_name: Optional[str],
        event_type: Optional[str],
        message: Optional[str],
        exception_type: Optional[str] = None,
        stack_trace: Optional[str] = None,
        call_local_id: Optional[int] = None,
    ) -> int:
        self._ensure_open()
        pc_user, machine_name, machine_ip = self._machine_identity()
        params = (
            self._dt(datetime.now()), level, logger_name, event_type, message,
            exception_type, stack_trace, pc_user, machine_name, machine_ip, call_local_id,
        )
        with self._lock:
            cur = self.local_conn.cursor()
            cur.execute(
                "INSERT INTO app_error_logs "
                "(log_time, level, logger_name, event_type, message, "
                "exception_type, stack_trace, pc_user, machine_name, machine_ip, call_local_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                params,
            )
            self.local_conn.commit()
            local_id = int(cur.lastrowid)

        if self._central_available():
            self._mark_unsynced("app_error_logs", local_id, "queued for background sync")
        return local_id

    # ── Sync ─────────────────────────────────────────────────────────────────

    def _mark_synced(self, table: str, local_id: int) -> None:
        self._ensure_open()
        with self._lock:
            self.local_conn.execute(
                f"UPDATE {table} SET central_synced=1, "
                f"central_synced_at=CURRENT_TIMESTAMP, central_sync_error=NULL WHERE id=?",
                (local_id,),
            )
            self.local_conn.commit()

    def _mark_unsynced(self, table: str, local_id: int, error: str) -> None:
        self._ensure_open()
        with self._lock:
            self.local_conn.execute(
                f"UPDATE {table} SET central_synced=0, central_sync_error=? WHERE id=?",
                ((error or "sync failed")[:1000], local_id),
            )
            self.local_conn.commit()

    def sync_unsynced_calls_to_central(self, limit: int = 100) -> int:
        self._ensure_open()
        if not self._central_available():
            return 0
        with self._lock:
            rows = self.local_conn.execute(
                "SELECT * FROM calls WHERE COALESCE(central_synced,0)=0 ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        synced = 0
        for row in rows:
            params = (self._source_call_uid(int(row["id"]), row["machine_name"], row["pc_user"]),) + self._call_params_from_row(row)
            success = self._central_execute(CENTRAL_CALL_UPSERT, params)
            if success:
                self._mark_synced("calls", int(row["id"]))
                synced += 1
            else:
                self._mark_unsynced("calls", int(row["id"]), "background sync failed")
        return synced

    def sync_unsynced_info_logs_to_central(self, limit: int = 100) -> int:
        self._ensure_open()
        if not self._central_available():
            return 0
        with self._lock:
            rows = self.local_conn.execute(
                "SELECT * FROM app_info_logs WHERE COALESCE(central_synced,0)=0 ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        synced = 0
        for row in rows:
            params = (
                row["log_time"], row["level"], row["logger_name"], row["event_type"],
                row["message"], row["pc_user"], row["machine_name"], row["machine_ip"],
                row["call_local_id"],
            )
            success = self._central_execute(CENTRAL_INFO_INSERT, params)
            if success:
                self._mark_synced("app_info_logs", int(row["id"]))
                synced += 1
            else:
                self._mark_unsynced("app_info_logs", int(row["id"]), "background sync failed")
        return synced

    def sync_unsynced_error_logs_to_central(self, limit: int = 100) -> int:
        self._ensure_open()
        if not self._central_available():
            return 0
        with self._lock:
            rows = self.local_conn.execute(
                "SELECT * FROM app_error_logs WHERE COALESCE(central_synced,0)=0 ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        synced = 0
        for row in rows:
            params = (
                row["log_time"], row["level"], row["logger_name"], row["event_type"],
                row["message"], row["exception_type"], row["stack_trace"],
                row["pc_user"], row["machine_name"], row["machine_ip"], row["call_local_id"],
            )
            success = self._central_execute(CENTRAL_ERROR_INSERT, params)
            if success:
                self._mark_synced("app_error_logs", int(row["id"]))
                synced += 1
            else:
                self._mark_unsynced("app_error_logs", int(row["id"]), "background sync failed")
        return synced

    # ── Cleanup ──────────────────────────────────────────────────────────────

    def cleanup_synced_calls(self, keep_latest: int = 0) -> int:
        """Call rows are retained indefinitely — intentional no-op."""
        return 0

    def cleanup_synced_info_logs(self, keep_latest: int = 0) -> int:
        self._ensure_open()
        if keep_latest > 0:
            q = (
                "DELETE FROM app_info_logs WHERE id NOT IN "
                "(SELECT id FROM app_info_logs ORDER BY id DESC LIMIT ?) "
                "AND COALESCE(central_synced,0)=1"
            )
            with self._lock:
                cur = self.local_conn.execute(q, (keep_latest,))
                self.local_conn.commit()
        else:
            with self._lock:
                cur = self.local_conn.execute(
                    "DELETE FROM app_info_logs WHERE COALESCE(central_synced,0)=1"
                )
                self.local_conn.commit()
        return cur.rowcount or 0

    def cleanup_synced_error_logs(self, keep_latest: int = 0) -> int:
        self._ensure_open()
        if keep_latest > 0:
            q = (
                "DELETE FROM app_error_logs WHERE id NOT IN "
                "(SELECT id FROM app_error_logs ORDER BY id DESC LIMIT ?) "
                "AND COALESCE(central_synced,0)=1"
            )
            with self._lock:
                cur = self.local_conn.execute(q, (keep_latest,))
                self.local_conn.commit()
        else:
            with self._lock:
                cur = self.local_conn.execute(
                    "DELETE FROM app_error_logs WHERE COALESCE(central_synced,0)=1"
                )
                self.local_conn.commit()
        return cur.rowcount or 0

    # ── Upload queue ─────────────────────────────────────────────────────────

    def update_uploaded_path(self, call_local_id: Optional[int], uploaded_path: str) -> None:
        self._ensure_open()
        if not call_local_id:
            return

        with self._lock:
            self.local_conn.execute(
                """
                UPDATE calls
                SET uploaded_path=?,
                    central_synced=0,
                    central_sync_error='uploaded path updated locally'
                WHERE id=?
                """,
                (uploaded_path, call_local_id),
            )
            self.local_conn.commit()

    def update_call_recording_path(
        self,
        call_local_id: int,
        recording_path: str,
        error_details: Optional[str] = None,
    ) -> None:
        self._ensure_open()
        with self._lock:
            self.local_conn.execute(
                """
                UPDATE calls
                SET recording_path=?,
                    error_details=?,
                    central_synced=0,
                    central_sync_error='recording path recovered locally'
                WHERE id=?
                """,
                (recording_path, error_details, call_local_id),
            )
            self.local_conn.commit()     

    def find_call_by_recording_path(self, recording_path: str, exclude_call_local_id: Optional[int] = None):
        self._ensure_open()
        with self._lock:
            if exclude_call_local_id:
                return self.local_conn.execute(
                    """
                    SELECT * FROM calls
                    WHERE recording_path=?
                      AND id<>?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (recording_path, exclude_call_local_id),
                ).fetchone()
            return self.local_conn.execute(
                """
                SELECT * FROM calls
                WHERE recording_path=?
                ORDER BY id DESC
                LIMIT 1
                """,
                (recording_path,),
            ).fetchone()              

    def enqueue_pending_upload(
        self,
        *,
        call_local_id: Optional[int],
        local_path: str,
        dest_rel_path: str,
        pc_user: Optional[str],
        machine_name: Optional[str],
        machine_ip: Optional[str],
        max_retries: int,
        initial_status: str = "pending",
    ) -> int:
        """
        Insert a new pending_uploads row.

        Pass initial_status='processing' from upload_for_session() so the row
        is never visible to the background scan as 'pending'.  The background
        scan uses initial_status='pending' (the default).
        """
        self._ensure_open()
        with self._lock:
            cur = self.local_conn.cursor()
            if initial_status == "processing":
                cur.execute(
                    "INSERT INTO pending_uploads "
                    "(call_local_id, local_path, dest_rel_path, pc_user, machine_name, "
                    " machine_ip, max_retries, status, processing_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'processing', CURRENT_TIMESTAMP)",
                    (
                        call_local_id, local_path, dest_rel_path,
                        pc_user, machine_name, machine_ip, max_retries,
                    ),
                )
            else:
                cur.execute(
                    "INSERT INTO pending_uploads "
                    "(call_local_id, local_path, dest_rel_path, pc_user, machine_name, "
                    " machine_ip, max_retries) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        call_local_id, local_path, dest_rel_path,
                        pc_user, machine_name, machine_ip, max_retries,
                    ),
                )
            self.local_conn.commit()
            return int(cur.lastrowid)

    def get_pending_uploads(self):
        self._ensure_open()
        with self._lock:
            return self.local_conn.execute(
                "SELECT * FROM pending_uploads WHERE status IN ('pending','failed') ORDER BY id ASC"
            ).fetchall()

    def get_upload_by_local_and_dest(self, local_path: str, dest_rel_path: str):
        self._ensure_open()
        with self._lock:
            return self.local_conn.execute(
                "SELECT * FROM pending_uploads WHERE local_path=? AND dest_rel_path=? ORDER BY id DESC LIMIT 1",
                (local_path, dest_rel_path),
            ).fetchone()

    def mark_upload_success(self, upload_id: int, uploaded_path: str) -> None:
        self._ensure_open()
        with self._lock:
            self.local_conn.execute(
                "UPDATE pending_uploads SET uploaded_path=?, status='uploaded', "
                "processing_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (uploaded_path, upload_id),
            )
            self.local_conn.commit()

    def mark_upload_failure(self, upload_id: int, retries: int, error: str) -> None:
        self._ensure_open()
        safe_error = (error or "upload failed")[:1000]
        with self._lock:
            self.local_conn.execute(
                """
                UPDATE pending_uploads
                SET retries=?,
                    status=CASE
                        WHEN ? >= COALESCE(max_retries, 3) THEN 'failed'
                        ELSE 'pending'
                    END,
                    last_error=?,
                    processing_at=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (retries, retries, safe_error, upload_id),
            )
            self.local_conn.commit()

    def mark_upload_pending_retry(self, upload_id: int) -> None:
        """Reset an exhausted upload row so it can be retried when the file becomes valid."""
        self._ensure_open()
        with self._lock:
            self.local_conn.execute(
                "UPDATE pending_uploads SET retries=0, status='pending', last_error=NULL, "
                "processing_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (upload_id,),
            )
            self.local_conn.commit()

    def claim_upload_for_processing(self, upload_id: int) -> bool:
        """
        Atomically transition one upload row from pending/failed → processing.

        A single UPDATE statement under the storage lock guarantees that only
        one caller wins when two threads race on the same row.  Returns True
        only when this caller made the transition (rowcount == 1).
        """
        self._ensure_open()
        with self._lock:
            cur = self.local_conn.execute(
                """
                UPDATE pending_uploads
                SET status='processing',
                    processing_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                  AND status IN ('pending', 'failed')
                """,
                (upload_id,),
            )
            self.local_conn.commit()
            return cur.rowcount == 1

    def reset_stale_processing_uploads(self, stale_after_seconds: int = 900) -> int:
        """
        Reset uploads stuck in 'processing' for longer than stale_after_seconds.

        This handles the case where the app crashed or was killed while an
        upload was in-flight.  Default 900 s (15 min) gives ample time even
        for large files over a slow SMB link.
        """
        self._ensure_open()
        from datetime import timedelta
        stale_before = (
            datetime.utcnow() - timedelta(seconds=stale_after_seconds)
        ).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            cur = self.local_conn.execute(
                """
                UPDATE pending_uploads
                SET status='pending',
                    processing_at=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE status='processing'
                  AND processing_at IS NOT NULL
                  AND processing_at < ?
                """,
                (stale_before,),
            )
            self.local_conn.commit()
            return cur.rowcount or 0

    # ── Read ─────────────────────────────────────────────────────────────────

    def get_calls(self):
        self._ensure_open()
        with self._lock:
            return self.local_conn.execute("SELECT * FROM calls ORDER BY id DESC").fetchall()

    def get_calls_missing_recording_path(self, limit: int = 100):
        self._ensure_open()
        with self._lock:
            return self.local_conn.execute(
                """
                SELECT * FROM calls
                WHERE COALESCE(recording_path, '') = ''
                  AND COALESCE(uploaded_path, '') = ''
                  AND COALESCE(status, '') <> ''
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def get_calls_by_start_time_window(
        self, from_dt: datetime, to_dt: datetime
    ) -> list:
        """
        Return call rows whose start_time falls within [from_dt, to_dt].
        Used by seg-file crash recovery to match WAV files to DB calls.
        """
        self._ensure_open()
        with self._lock:
            return self.local_conn.execute(
                """
                SELECT * FROM calls
                WHERE start_time >= ?
                  AND start_time <= ?
                ORDER BY start_time ASC
                """,
                (
                    from_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    to_dt.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            ).fetchall()

    def get_info_logs(self):
        self._ensure_open()
        with self._lock:
            return self.local_conn.execute(
                "SELECT * FROM app_info_logs ORDER BY id DESC"
            ).fetchall()

    def get_error_logs(self):
        self._ensure_open()
        with self._lock:
            return self.local_conn.execute(
                "SELECT * FROM app_error_logs ORDER BY id DESC"
            ).fetchall()

    def close(self) -> None:
        self._closed = True
        try:
            with self._lock:
                self.local_conn.close()
        finally:
            if self._central_engine is not None:
                try:
                    self._central_engine.dispose()
                except Exception:
                    pass
