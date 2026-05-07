"""
Tests for launcher.py worker/launcher mode logic.
No real subprocess or audio hardware — all external calls are mocked.
"""
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Worker mode ───────────────────────────────────────────────────────────────

class TestWorkerMode:
    def test_worker_mode_calls_main_run(self):
        """--worker in argv → calls main.run() exactly once and returns."""
        mock_main = MagicMock()
        with patch.dict(sys.modules, {"main": mock_main}), \
             patch.object(sys, "argv", ["launcher.py", "--worker"]):
            import launcher
            launcher.main()
        mock_main.run.assert_called_once()

    def test_worker_mode_does_not_recurse(self):
        """Worker mode calls main.run() exactly once — no restart loop entered."""
        call_count = 0

        def fake_run():
            nonlocal call_count
            call_count += 1

        mock_main = MagicMock()
        mock_main.run.side_effect = fake_run
        with patch.dict(sys.modules, {"main": mock_main}), \
             patch.object(sys, "argv", ["launcher.py", "--worker"]):
            import launcher
            launcher.main()

        assert call_count == 1, f"main.run() called {call_count} times; expected exactly 1"

    def test_worker_mode_returns_after_run(self):
        """main() returns (does NOT call sys.exit) when --worker exits normally."""
        mock_main = MagicMock()
        mock_exit = MagicMock()
        with patch.dict(sys.modules, {"main": mock_main}), \
             patch.object(sys, "argv", ["launcher.py", "--worker"]), \
             patch("sys.exit", mock_exit):
            import launcher
            launcher.main()
        mock_exit.assert_not_called()


# ── Launcher mode ─────────────────────────────────────────────────────────────

class TestLauncherMode:
    """Tests for the outer restart-loop mode (no --worker in argv)."""

    def _make_popen(self, exit_codes):
        """Return a Popen factory whose instances return exit_codes in sequence."""
        codes = list(exit_codes)
        call_idx = [0]

        class _Proc:
            def __init__(self):
                self.pid = 100 + call_idx[0]
                self._rc = codes[min(call_idx[0], len(codes) - 1)]
                call_idx[0] += 1

            @property
            def returncode(self):
                return self._rc

            def wait(self):
                pass

        cmds = []

        def fake_popen(cmd, **kw):
            cmds.append(list(cmd))
            return _Proc()

        return fake_popen, cmds

    def test_launcher_builds_worker_command_non_frozen(self):
        """Without --worker and not frozen: command is [python, 'launcher.py', '--worker']."""
        fake_popen, cmds = self._make_popen([0])
        mock_log = MagicMock()
        with patch("subprocess.Popen", side_effect=fake_popen), \
             patch.object(sys, "argv", ["launcher.py"]), \
             patch("launcher.setup_logging", return_value=mock_log), \
             patch("sys.exit", side_effect=SystemExit):
            import launcher
            with pytest.raises(SystemExit):
                launcher.main()

        assert cmds, "Popen was never called"
        assert cmds[0][0] == sys.executable
        assert cmds[0][-1] == "--worker"

    def test_launcher_builds_worker_command_frozen(self, tmp_path):
        """In frozen mode: command is [sys.executable, '--worker'] (same EXE, no script path)."""
        fake_exe = str(tmp_path / "WhatsAppWatcher.exe")
        fake_popen, cmds = self._make_popen([0])
        mock_log = MagicMock()
        with patch("subprocess.Popen", side_effect=fake_popen), \
             patch.object(sys, "argv", [fake_exe]), \
             patch.object(sys, "executable", fake_exe), \
             patch("sys.frozen", True, create=True), \
             patch("launcher.setup_logging", return_value=mock_log), \
             patch("sys.exit", side_effect=SystemExit):
            import importlib, launcher
            importlib.reload(launcher)   # re-run module level with frozen patched
            with pytest.raises(SystemExit):
                launcher.main()

        assert cmds, "Popen was never called"
        assert cmds[0] == [fake_exe, "--worker"], (
            f"expected [{fake_exe!r}, '--worker'], got {cmds[0]}"
        )

    def test_launcher_restarts_worker_on_crash(self):
        """Worker crash (non-zero exit) → launcher spawns another worker."""
        # First run crashes (-1), second exits cleanly (0)
        fake_popen, cmds = self._make_popen([-1, 0])
        mock_log = MagicMock()
        with patch("subprocess.Popen", side_effect=fake_popen), \
             patch.object(sys, "argv", ["launcher.py"]), \
             patch("launcher.setup_logging", return_value=mock_log), \
             patch("time.sleep"), \
             patch("sys.exit", side_effect=SystemExit):
            import launcher
            with pytest.raises(SystemExit):
                launcher.main()

        assert len(cmds) >= 2, (
            f"Expected ≥2 Popen calls (crash + restart), got {len(cmds)}"
        )

    def test_launcher_stops_on_clean_exit(self):
        """Worker clean exit (code=0) → launcher calls sys.exit(0), does NOT restart."""
        fake_popen, cmds = self._make_popen([0])
        mock_log = MagicMock()
        exit_args = []
        with patch("subprocess.Popen", side_effect=fake_popen), \
             patch.object(sys, "argv", ["launcher.py"]), \
             patch("launcher.setup_logging", return_value=mock_log), \
             patch("sys.exit", side_effect=lambda c: exit_args.append(c) or (_ for _ in ()).throw(SystemExit(c))):
            import launcher
            with pytest.raises(SystemExit):
                launcher.main()

        assert len(cmds) == 1, f"Expected exactly 1 Popen call, got {len(cmds)}"
        assert exit_args == [0], f"Expected sys.exit(0), got sys.exit({exit_args})"
