"""
WhatsApp Watcher — auto-restart launcher.
Run this instead of main.py. Restarts the watcher automatically
if it crashes for any reason.

In frozen (EXE) mode:
  WhatsAppWatcher.exe           → launcher mode (restart loop)
  WhatsAppWatcher.exe --worker  → worker mode (runs main.run() once)

In development mode:
  python launcher.py            → launcher mode
  python launcher.py --worker   → worker mode
"""
import subprocess
import sys
import time
import logging
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

LOG_DIR  = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "launcher.log"

RESTART_DELAY_SECONDS  = 5
MAX_RESTARTS_PER_HOUR  = 20
CLEAN_EXIT_CODE        = 0


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    handler = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s"
    ))
    log = logging.getLogger("launcher")
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(message)s"
        ))
        log.addHandler(console)
    return log


def main() -> None:
    # ── Worker mode ──────────────────────────────────────────────────────────
    # Entered when the EXE/script is invoked with --worker.
    # Calls main.run() once and returns — no restart loop.
    if "--worker" in sys.argv:
        import main as app_main
        app_main.run()
        return

    # ── Launcher mode ─────────────────────────────────────────────────────────
    log = setup_logging()
    log.info("=" * 60)
    log.info("LAUNCHER → WhatsApp Watcher starting (launcher mode)")
    log.info("LAUNCHER → restart delay: %ss | max restarts/hour: %s",
             RESTART_DELAY_SECONDS, MAX_RESTARTS_PER_HOUR)
    log.info("=" * 60)

    # Frozen EXE: same EXE with --worker flag.
    # Dev mode: explicit launcher.py path so Python knows what to run.
    if getattr(sys, "frozen", False):
        worker_cmd = [sys.executable, "--worker"]
    else:
        worker_cmd = [sys.executable, str(BASE_DIR / "launcher.py"), "--worker"]

    restart_timestamps: list = []
    total_restarts = 0

    while True:
        now = time.time()
        restart_timestamps = [t for t in restart_timestamps if now - t < 3600]

        if len(restart_timestamps) >= MAX_RESTARTS_PER_HOUR:
            log.error(
                "LAUNCHER → %s crashes in last hour — stopping to prevent crash loop. "
                "Check logs/crash.log and logs/watcher.log.",
                len(restart_timestamps),
            )
            sys.exit(1)

        total_restarts += 1
        log.info(
            "LAUNCHER → starting worker | attempt=%s | crashes_this_hour=%s",
            total_restarts, len(restart_timestamps),
        )

        start_time = time.time()
        worker_pid = None
        exit_code = -99
        try:
            proc = subprocess.Popen(worker_cmd, cwd=str(BASE_DIR))
            worker_pid = proc.pid
            log.info("LAUNCHER → worker PID=%s", worker_pid)
            proc.wait()
            exit_code = proc.returncode
        except KeyboardInterrupt:
            log.info("LAUNCHER → keyboard interrupt — stopping launcher")
            sys.exit(0)
        except Exception:
            log.exception("LAUNCHER → failed to launch worker")
            exit_code = -99

        uptime_seconds = int(time.time() - start_time)
        restart_timestamps.append(time.time())

        log.info(
            "LAUNCHER → worker exited | PID=%s | exit_code=%s | uptime=%ss | total_runs=%s",
            worker_pid, exit_code, uptime_seconds, total_restarts,
        )

        if exit_code == CLEAN_EXIT_CODE:
            log.info(
                "LAUNCHER → worker exited cleanly (code=0 | uptime=%ss) — launcher stopping",
                uptime_seconds,
            )
            sys.exit(0)

        log.warning(
            "LAUNCHER → worker CRASHED | exit_code=%s | uptime=%ss | "
            "total_crashes=%s | restarting in %ss...",
            exit_code, uptime_seconds, total_restarts, RESTART_DELAY_SECONDS,
        )
        log.warning(
            "LAUNCHER → check logs/watcher.log and logs/crash.log for crash details"
        )

        try:
            time.sleep(RESTART_DELAY_SECONDS)
        except KeyboardInterrupt:
            log.info("LAUNCHER → keyboard interrupt during restart delay — stopping")
            sys.exit(0)


if __name__ == "__main__":
    main()
