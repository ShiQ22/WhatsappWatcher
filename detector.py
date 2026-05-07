from __future__ import annotations

"""WhatsApp Desktop call detector.

Uses UIAutomation COM directly via comtypes (primary — fast and reliable).
Falls back to pywinauto if comtypes is unavailable or fails twice.

Session model:
- A call session starts when the WhatsApp voice-call window appears.
- Recording starts immediately on window detection — no answered/not-answered gate.
- Direction is captured from strong UIA proof; weak guesses are avoided.
- UIA is retried once on failure, then pywinauto is tried (also retried once).
- If all 4 scan attempts fail, direction stays unknown but recording continues.
- Scan failures never discard an active session or stop the recording.
- When the call window disappears the session finalizes as ENDED (always kept).
- No answered/not-answered state — all sessions are retained unconditionally.
"""

import ctypes
import ctypes.wintypes
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set

import psutil

from config import WHATSAPP_KEYWORD_ALIASES, WHATSAPP_PROCESS_ALIASES
from state_machine import CallEvent

# ── Direction confidence ranking ─────────────────────────────────────────────
DIRECTION_CONFIDENCE_RANK: dict[str, int] = {
    "none": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
}

# ── comtypes UIAutomation (primary — fast and reliable) ──────────────────────
_UIA = None
_UIA_INIT_DONE = False


def _init_uia():
    """Lazily initialize UIAutomation COM via comtypes."""
    global _UIA, _UIA_INIT_DONE
    if _UIA_INIT_DONE:
        return _UIA
    _UIA_INIT_DONE = True
    try:
        import comtypes
        import comtypes.client
        from comtypes import GUID
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen.UIAutomationClient import IUIAutomation
        _UIA = comtypes.CoCreateInstance(
            GUID("{ff48dba4-60ef-4201-aa87-54103eef594e}"),
            interface=IUIAutomation,
        )
        return _UIA
    except Exception as exc:
        logging.getLogger("watcher.detector").warning("comtypes UIA init failed: %s", exc)
        return None


# ── pywinauto fallback ───────────────────────────────────────────────────────
try:
    from pywinauto import Desktop  # type: ignore
except Exception:  # pragma: no cover
    Desktop = None

log = logging.getLogger("watcher.detector")

CALL_WINDOW_TITLES = {"Voice call", "مكالمة صوتية"}
CALL_WINDOW_TITLE_LENGTHS = {len(title) for title in CALL_WINDOW_TITLES}
CALL_WINDOW_CLASS = "WinUIDesktopWin32WindowClass"

MIN_WINDOW_SIZE = 1
FOREGROUND_OUTGOING_WINDOW_SECS = 3.0

# ── Scan tuning ──────────────────────────────────────────────────────────────
MAX_UIA_ELEMENTS = 500
MAX_UIA_SCAN_SECONDS = 3.0
SESSION_WINDOW_GAP_SECONDS = 2.5
POST_TERMINAL_COOLDOWN_SECONDS = 15.0
POST_TERMINAL_HARD_SUPPRESS_SECONDS = 4.0

ACCEPT_LABELS = {
    "accept call", "answer call", "accept", "answer",
    "قبول المكالمة", "قبول", "رد", "الإجابة على المكالمة", "اجابة", "إجابة",
}
DECLINE_LABELS = {
    "decline call", "decline", "reject call", "reject",
    "رفض المكالمة", "رفض", "رفض الاتصال",
}
RINGING_LABELS = {
    "ringing", "ringing...", "ringing…",
    "calling", "calling...", "calling…",
    "يرن", "يرن...", "يرن…",
    "جار الاتصال", "جار الاتصال...", "جار الاتصال…",
    "جارِ الاتصال", "جارِ الاتصال...", "جارِ الاتصال…",
}
CONNECTING_LABELS = {
    "connecting", "connecting...", "connecting…",
    "جارٍ الاتصال", "جاري الاتصال", "يتم الاتصال", "جاري الربط",
}
# NO_ANSWER_LABELS kept only for logging — no longer gates any logic
NO_ANSWER_LABELS = {
    "no answer", "no reply",
    "لا توجد إجابة", "لا يوجد جواب", "لا إجابة", "لم يتم الرد",
}
ENDED_LABELS = {
    "call ended",
    "انتهت المكالمة",
    "انتهت المكالمه",
}

# Ringing session with no strong call UI proof for this many seconds → ENDED
STALE_RINGING_TIMEOUT_SECONDS = 5.0

CALL_STATUS_AUTO_ID = "CallStatusText"
CALLER_TEXT_AUTO_ID = "CallerTextBlock"
PARTICIPANT_LIST_AUTO_ID = "ParticipantList"
END_CALL_AUTO_ID = "NewEndCallButton"

TIMER_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
TIME_SEPARATORS = {':', '∶', '：', '٫', '۔'}
PHONE_RE = re.compile(r"(?:\+?\d[\d\s\-(]{7,}\d)")

_WA_EXACT_NAMES: Set[str] = WHATSAPP_PROCESS_ALIASES or {
    "whatsapp.exe", "whatsapp.root.exe", "whatsappbeta.exe",
    "whatsappdesktop.exe", "whatsapphelper.exe", "wahelper.exe",
}
_WA_KEYWORDS = WHATSAPP_KEYWORD_ALIASES or [
    "whatsapp", "whatsappdesktop", "whatsappbeta", "5319275a.whatsappdesktop",
]

_user32 = ctypes.windll.user32
_EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)


@dataclass
class _WinInfo:
    hwnd: int
    pid: int
    width: int
    height: int


@dataclass
class _UIAState:
    source: str = "uia"
    incoming: bool = False
    outgoing: bool = False
    answered: bool = False       # timer seen — call is active
    ringing: bool = False
    connecting: bool = False
    no_answer: bool = False      # informational only — does NOT gate any logic
    has_end_call_button: bool = False
    timer_text: Optional[str] = None
    status_text: Optional[str] = None
    call_status_raw: Optional[str] = None
    caller_number: Optional[str] = None
    details: str = ""
    debug_hits: Optional[List[str]] = None
    scan_ms: float = 0.0


@dataclass
class DetectionResult:
    event: Optional[CallEvent]
    source: str
    details: Optional[str] = None
    caller_number: Optional[str] = None
    direction: Optional[str] = None
    hwnd: Optional[int] = None
    is_new_window: bool = False
    is_strong_new_call: bool = False


@dataclass
class _DirectionDecision:
    direction: Optional[str]
    reason: str
    confidence: str  # high | medium | low | none


# ── Win32 helpers ────────────────────────────────────────────────────────────

def _get_foreground_pid() -> Optional[int]:
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid_val = ctypes.wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_val))
        return int(pid_val.value) if pid_val.value else None
    except Exception:
        return None


def _find_call_window(wa_pids: Set[int], allow_invisible: bool = False) -> Optional[_WinInfo]:
    found: list[_WinInfo] = []

    def _cb(hwnd: int, _: int) -> bool:
        if not allow_invisible and not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length not in CALL_WINDOW_TITLE_LENGTHS:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value not in CALL_WINDOW_TITLES:
            return True
        cbuf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, cbuf, 256)
        if cbuf.value != CALL_WINDOW_CLASS:
            return True
        pid_val = ctypes.wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_val))
        if pid_val.value not in wa_pids:
            return True
        rect = ctypes.wintypes.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = max(0, rect.right - rect.left)
        height = max(0, rect.bottom - rect.top)
        if width < MIN_WINDOW_SIZE or height < MIN_WINDOW_SIZE:
            return True
        found.append(_WinInfo(hwnd=hwnd, pid=pid_val.value, width=width, height=height))
        return True

    _user32.EnumWindows(_EnumWindowsProc(_cb), 0)
    if not found:
        return None
    found.sort(key=lambda w: (w.width * w.height, w.width, w.height), reverse=True)
    return found[0]


# ── Direct comtypes UIAutomation scanner ─────────────────────────────────────

def _comtypes_scan(hwnd: int) -> Optional[_UIAState]:
    """Walk the UIA tree via comtypes — fast (~100ms) and reliable."""
    uia = _init_uia()
    if uia is None:
        return None

    scan_start = time.time()
    try:
        element = uia.ElementFromHandle(hwnd)
        if not element:
            return None

        condition = uia.CreateTrueCondition()
        tree_walker = uia.CreateTreeWalker(condition)

        labels: set[str] = set()
        phone_candidates: list[str] = []
        timer_candidates: list[str] = []
        status_candidates: list[str] = []
        debug_hits: list[str] = []
        has_end_call = False
        call_status_raw: Optional[str] = None

        stack = [(element, 0)]
        count = 0

        while stack and count < MAX_UIA_ELEMENTS:
            if (time.time() - scan_start) > MAX_UIA_SCAN_SECONDS:
                break
            node, depth = stack.pop()
            count += 1

            try:
                name = node.CurrentName or ""
                auto_id = node.CurrentAutomationId or ""
            except Exception:
                continue

            norm_name = _norm_label(name)
            norm_status = _norm_status(name)

            if norm_name:
                labels.add(norm_name)

            if auto_id == CALL_STATUS_AUTO_ID:
                call_status_raw = norm_status
                if norm_status:
                    status_candidates.insert(0, norm_status)
                    if len(debug_hits) < 20:
                        debug_hits.append(f"CallStatusText={norm_status!r}")
                if TIMER_RE.match(norm_status):
                    timer_candidates.insert(0, norm_status)

            elif auto_id == CALLER_TEXT_AUTO_ID:
                if PHONE_RE.search(name):
                    phone_candidates.insert(0, re.sub(r"\s+", " ", name.strip()))
                if len(debug_hits) < 20:
                    debug_hits.append(f"CallerTextBlock={name!r}")

            elif auto_id == END_CALL_AUTO_ID:
                has_end_call = True
                if len(debug_hits) < 20:
                    debug_hits.append("EndCallButton=present")

            else:
                if TIMER_RE.match(norm_status):
                    timer_candidates.append(norm_status)
                    if len(debug_hits) < 20:
                        debug_hits.append(f"timer:auto_id={auto_id!r} text={norm_status!r}")
                if PHONE_RE.search(name):
                    phone_candidates.append(re.sub(r"\s+", " ", name.strip()))
                if norm_status:
                    status_candidates.append(norm_status)

            if depth < 15:
                try:
                    child = tree_walker.GetFirstChildElement(node)
                    while child:
                        stack.append((child, depth + 1))
                        child = tree_walker.GetNextSiblingElement(child)
                except Exception:
                    pass

        timer_text = next((t for t in timer_candidates if TIMER_RE.match(t)), None)
        status_text = timer_text
        if status_text is None:
            for c in status_candidates:
                if c:
                    status_text = c
                    break

        norm_st = _norm_label(status_text or "")
        is_ringing = norm_st in RINGING_LABELS or any(l in norm_st for l in RINGING_LABELS if l)
        is_connecting = norm_st in CONNECTING_LABELS or any(l in norm_st for l in CONNECTING_LABELS if l)
        is_no_answer = norm_st in NO_ANSWER_LABELS or any(l in norm_st for l in NO_ANSWER_LABELS if l)

        call_status_label = _norm_label(call_status_raw or "")
        status_is_outgoing_ring = (
            call_status_label in RINGING_LABELS
            or any(lbl in call_status_label for lbl in RINGING_LABELS if lbl)
        )

        state = _UIAState(source="comtypes", debug_hits=debug_hits)
        state.incoming = bool(labels & ACCEPT_LABELS and labels & DECLINE_LABELS)
        state.outgoing = bool(
            ((labels & RINGING_LABELS) or status_is_outgoing_ring)
            and not state.incoming
        )
        state.timer_text = timer_text
        state.status_text = status_text
        state.call_status_raw = call_status_raw
        state.answered = timer_text is not None   # timer present = call is active
        state.ringing = state.incoming or state.outgoing or is_ringing
        state.connecting = is_connecting and not state.answered
        state.no_answer = is_no_answer   # informational only
        state.has_end_call_button = has_end_call
        state.caller_number = phone_candidates[0] if phone_candidates else None
        state.scan_ms = (time.time() - scan_start) * 1000

        detail_parts = [
            f"incoming={state.incoming}", f"outgoing={state.outgoing}",
            f"ringing={state.ringing}", f"connecting={state.connecting}",
            f"answered={state.answered}", f"end_call_btn={has_end_call}",
            f"elements={count}",
        ]
        if state.no_answer:
            detail_parts.append("no_answer=True")
        if state.status_text:
            detail_parts.append(f"status={state.status_text}")
        if state.timer_text:
            detail_parts.append(f"timer={state.timer_text}")
        if state.caller_number:
            detail_parts.append(f"caller={state.caller_number}")
        state.details = " | ".join(detail_parts)
        return state

    except Exception as exc:
        elapsed = (time.time() - scan_start) * 1000
        log.warning("DETECTOR → comtypes scan failed in %.0fms: %s", elapsed, exc)
        return None


def _norm_label(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def _norm_status(value: str) -> str:
    v = value or ""
    trans = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
    v = v.translate(trans)
    for sep in TIME_SEPARATORS:
        if sep != ':':
            v = v.replace(sep, ':')
    v = v.replace('…', '...')
    return v.strip()


# ── Detector class ───────────────────────────────────────────────────────────

class WhatsAppDetector:
    def __init__(self) -> None:
        self._last_whatsapp_foreground_ts = 0.0
        self._wa_was_running: Optional[bool] = None
        self._last_session_ended_ts: float = 0.0
        self._reset_internal_state()

    def _reset_internal_state(self) -> None:
        self._call_hwnd: Optional[int] = None
        self._call_pid: Optional[int] = None
        self._call_direction: Optional[str] = None
        self._call_direction_confidence: str = "none"
        self._direction_frozen = False
        self._session_answered_proof_seen = False  # timer proof for ACTIVE state
        self._session_answered_timer: Optional[str] = None
        self._session_answered_at_ts: Optional[float] = None
        self._caller_number: Optional[str] = None
        self._last_window_seen_ts: Optional[float] = None
        self._ring_event_emitted = False
        self._answered_event_emitted = False
        self._last_uia_phase: Optional[str] = None
        self._last_window_signature: Optional[str] = None
        # UIA failure tracking for failover
        self._uia_consecutive_failures: int = 0
        # Stale-ringing detection: last time strong call UI proof was seen
        self._last_strong_call_ui_ts: float = 0.0
        # Set True when hwnd changes during an answered session; cleared after scan check
        self._new_hwnd_during_answered: bool = False

    def get_whatsapp_pids(self) -> Set[int]:
        pids: Set[int] = set()
        self_pid = psutil.Process().pid

        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                pid = proc.info.get("pid")
                name = (proc.info.get("name") or "").lower().strip()
                exe = (proc.info.get("exe") or "").lower().strip()

                if not pid or pid == self_pid:
                    continue

                exe_name = Path(exe).name.lower().strip() if exe else ""

                if name in _WA_EXACT_NAMES or exe_name in _WA_EXACT_NAMES:
                    pids.add(pid)

            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        return pids

    def reset(self) -> None:
        self._reset_internal_state()

    def _update_foreground_history(self, wa_pids: Set[int]) -> None:
        fg_pid = _get_foreground_pid()
        if fg_pid and fg_pid in wa_pids:
            self._last_whatsapp_foreground_ts = time.time()

    def _was_whatsapp_foreground_recently(self) -> bool:
        return (time.time() - self._last_whatsapp_foreground_ts) <= FOREGROUND_OUTGOING_WINDOW_SECS

    # ── Direction ────────────────────────────────────────────────────────

    def _classify_direction_initial(self, win: _WinInfo) -> _DirectionDecision:
        return _DirectionDecision(None, f"direction unavailable (window {win.width}x{win.height})", "none")

    def _classify_direction_from_state(self, win: _WinInfo, state: Optional[_UIAState]) -> _DirectionDecision:
        if state and state.incoming:
            return _DirectionDecision("incoming", "accept/decline buttons present", "high")
        if state and state.outgoing:
            return _DirectionDecision("outgoing", "outgoing ringing/calling label", "high")
        # CallStatusText says "Calling..." → outgoing
        if state and state.call_status_raw:
            csr = _norm_label(state.call_status_raw)
            if csr in RINGING_LABELS or any(lbl in csr for lbl in RINGING_LABELS if lbl):
                return _DirectionDecision("outgoing", f"CallStatusText='{state.call_status_raw}'", "high")
        # no_answer hint: treat as outgoing (low confidence only)
        if state and state.no_answer:
            if self._call_direction_confidence in {"high", "medium"} and self._call_direction:
                return _DirectionDecision(
                    self._call_direction,
                    f"preserving stronger prior over no_answer ({state.status_text})",
                    self._call_direction_confidence,
                )
            return _DirectionDecision("outgoing", f"no_answer hint ({state.status_text})", "low")
        if self._call_direction:
            return _DirectionDecision(self._call_direction, "preserving prior", self._call_direction_confidence)
        return self._classify_direction_initial(win)

    def _maybe_apply_direction(self, decision: _DirectionDecision) -> None:
        if not decision.direction:
            return
        current_rank = DIRECTION_CONFIDENCE_RANK.get(self._call_direction_confidence, 0)
        new_rank = DIRECTION_CONFIDENCE_RANK.get(decision.confidence, 0)
        if self._call_direction is None:
            self._call_direction = decision.direction
            self._call_direction_confidence = decision.confidence
            self._direction_frozen = decision.confidence == "high"
            log.info(
                "DETECTOR → direction=%s (%s, %s)",
                self._call_direction, decision.reason, decision.confidence,
            )
            return
        if self._direction_frozen and new_rank < DIRECTION_CONFIDENCE_RANK.get("high", 3):
            return
        if decision.direction != self._call_direction:
            if new_rank > current_rank or decision.confidence == "high":
                old = self._call_direction
                self._call_direction = decision.direction
                self._call_direction_confidence = decision.confidence
                self._direction_frozen = decision.confidence == "high"
                log.info(
                    "DETECTOR → direction upgraded %s→%s (%s, %s)",
                    old, self._call_direction, decision.reason, decision.confidence,
                )
            return
        if new_rank > current_rank:
            self._call_direction_confidence = decision.confidence
            self._direction_frozen = decision.confidence == "high"

    # ── Logging ──────────────────────────────────────────────────────────

    def _log_phase_once(self, phase: str, details: str) -> None:
        if phase != self._last_uia_phase:
            self._last_uia_phase = phase
            log.info("DETECTOR → phase=%s | %s", phase, details)

    # ── UIA scan with failover ───────────────────────────────────────────

    def _scan_with_failover(self, hwnd: int) -> tuple[Optional[_UIAState], str]:
        """
        Try comtypes first (twice), then pywinauto (twice).
        Returns (state, source_name).
        If all fail, returns (None, 'none') — recording still continues.
        """
        # Attempt 1: comtypes
        state = _comtypes_scan(hwnd)
        if state is not None:
            self._uia_consecutive_failures = 0
            return state, "comtypes"

        # Attempt 2: comtypes retry
        log.debug("DETECTOR → comtypes failed; retry 1...")
        time.sleep(0.05)
        state = _comtypes_scan(hwnd)
        if state is not None:
            self._uia_consecutive_failures = 0
            return state, "comtypes-retry"

        # comtypes failed twice — go to pywinauto fallback
        log.debug("DETECTOR → comtypes failed twice; trying pywinauto fallback...")
        state = self._pywinauto_scan(hwnd)
        if state is not None:
            self._uia_consecutive_failures = 0
            return state, "pywinauto"

        # Attempt 4: pywinauto retry
        log.debug("DETECTOR → pywinauto failed; retry 1...")
        time.sleep(0.05)
        state = self._pywinauto_scan(hwnd)
        if state is not None:
            self._uia_consecutive_failures = 0
            return state, "pywinauto-retry"

        # All 4 attempts failed
        self._uia_consecutive_failures += 1
        log.warning(
            "DETECTOR → all UIA scans failed (consecutive failures: %d); "
            "direction=unknown, recording continues",
            self._uia_consecutive_failures,
        )
        return None, "none"

    # ── Main poll ────────────────────────────────────────────────────────

    def poll(self) -> DetectionResult:
        now_ts = time.time()
        wa_pids = self.get_whatsapp_pids()
        wa_running = bool(wa_pids)

        if self._wa_was_running is None:
            log.info(
                "DETECTOR → WhatsApp %s (PIDs: %s)",
                "found" if wa_running else "NOT found",
                sorted(wa_pids),
            )
            self._wa_was_running = wa_running
        elif wa_running != self._wa_was_running:
            log.info("DETECTOR → WhatsApp %s", "detected again" if wa_running else "no longer detected")
            self._wa_was_running = wa_running

        if not wa_pids:
            if self._call_hwnd is not None:
                log.warning("DETECTOR → WhatsApp gone during session — resetting")
            self._reset_internal_state()
            return DetectionResult(None, "detector", "WhatsApp not running")

        self._update_foreground_history(wa_pids)
        win = _find_call_window(wa_pids, allow_invisible=False) or _find_call_window(wa_pids, allow_invisible=True)

        # ── Window gone ──────────────────────────────────────────────────
        if win is None:
            if self._call_hwnd is None:
                return DetectionResult(None, "detector", "No call window")

            gap = now_ts - (self._last_window_seen_ts or now_ts)
            if gap <= SESSION_WINDOW_GAP_SECONDS:
                return DetectionResult(
                    None, "detector",
                    f"window missing {gap:.1f}s; preserving session",
                    caller_number=self._caller_number,
                    direction=self._call_direction,
                )

            # Window gone long enough — finalize session (always ENDED, always kept)
            direction = self._call_direction or "unknown"
            number = self._caller_number
            hwnd_at_end = self._call_hwnd
            if self._session_answered_proof_seen:
                details = f"session ended | dir={direction} | timer={self._session_answered_timer or '-'}"
                log.info(
                    "DETECTOR → session ended (was active) | dir=%s | timer=%s | number=%s",
                    direction, self._session_answered_timer or "-", number or "-",
                )
            else:
                details = f"session ended before timer proof | dir={direction}"
                log.info(
                    "DETECTOR → session ended (no timer proof) | dir=%s | number=%s",
                    direction, number or "-",
                )
            self._last_session_ended_ts = now_ts
            self._reset_internal_state()
            return DetectionResult(
                CallEvent.ENDED, "detector", details,
                caller_number=number, direction=direction, hwnd=hwnd_at_end,
            )

        # ── Window found ─────────────────────────────────────────────────
        self._last_window_seen_ts = now_ts
        sig = f"{win.hwnd}:{win.width}x{win.height}:{win.pid}"
        new_window = self._call_hwnd is None or self._call_hwnd != win.hwnd
        previous_hwnd = self._call_hwnd  # capture before any assignment so logs always show the real old value
        previous_direction = self._call_direction  # capture before hwnd-change reset for use in ENDED result

        if new_window:
            if self._call_hwnd is None:
                log.info(
                    "DETECTOR → call window | hwnd=%s | %dx%d | pid=%s",
                    win.hwnd, win.width, win.height, win.pid,
                )
                self._call_direction = None
                self._call_direction_confidence = "none"
                self._direction_frozen = False
                self._session_answered_proof_seen = False
                self._session_answered_timer = None
                self._session_answered_at_ts = None
                self._caller_number = None
                self._ring_event_emitted = False
                self._answered_event_emitted = False
                self._last_uia_phase = None
                self._uia_consecutive_failures = 0
            else:
                if self._session_answered_proof_seen:
                    log.warning(
                        "DETECTOR → hwnd changed %s→%s | answered session — will check new window for new ring",
                        self._call_hwnd, win.hwnd,
                    )
                    self._new_hwnd_during_answered = True
                else:
                    log.warning(
                        "DETECTOR → hwnd changed %s→%s | resetting weak session state",
                        self._call_hwnd, win.hwnd,
                    )
                    self._call_direction = None
                    self._call_direction_confidence = "none"
                    self._direction_frozen = False
                    self._caller_number = None
                    self._ring_event_emitted = False
                    self._answered_event_emitted = False
                    self._last_uia_phase = None
                    self._uia_consecutive_failures = 0
            self._call_hwnd = win.hwnd
            self._call_pid = win.pid

        if sig != self._last_window_signature:
            self._last_window_signature = sig
            log.debug("DETECTOR → sig=%s", sig)

        # ── Scan UI with failover (comtypes × 2, pywinauto × 2) ─────────
        state, scan_source = self._scan_with_failover(win.hwnd)

        if state:
            log.debug(
                "DETECTOR → %s scan %.0fms | answered=%s | status=%s | timer=%s | hits=%s",
                scan_source, state.scan_ms, state.answered,
                state.status_text or "-", state.timer_text or "-",
                state.debug_hits[:5] if state.debug_hits else "[]",
            )

        if state and state.caller_number:
            if state.caller_number != self._caller_number:
                log.info("DETECTOR → caller: %s", state.caller_number)
            self._caller_number = state.caller_number

        # ── FIX 2: hwnd changed during answered session ───────────────────
        # If the new window shows fresh ring proof, override "preserve answered"
        # so main.py receives a new ring event and splits the session.
        if self._new_hwnd_during_answered:
            self._new_hwnd_during_answered = False
            if state and (state.incoming or state.outgoing or state.ringing) and not state.answered:
                old_dir = self._call_direction or "unknown"
                new_dir_hint = "incoming" if state.incoming else "outgoing" if state.outgoing else "unknown"
                log.warning(
                    "DETECTOR → hwnd changed during answered session; new call proof found"
                    " | old_hwnd=%s | new_hwnd=%s | old_dir=%s | new_dir=%s",
                    previous_hwnd, win.hwnd, old_dir, new_dir_hint,
                )
                self._session_answered_proof_seen = False
                self._session_answered_timer = None
                self._session_answered_at_ts = None
                self._ring_event_emitted = False
                self._answered_event_emitted = False
                self._call_direction = None
                self._call_direction_confidence = "none"
                self._direction_frozen = False
                self._last_uia_phase = None

        elapsed_since_end = now_ts - self._last_session_ended_ts
        strong_new_session = bool(
            state and (state.incoming or state.outgoing or state.answered or state.connecting)
        )
        session_started_this_poll = new_window and elapsed_since_end >= 0
        if session_started_this_poll and elapsed_since_end < POST_TERMINAL_HARD_SUPPRESS_SECONDS and not strong_new_session:
            remaining = POST_TERMINAL_HARD_SUPPRESS_SECONDS - elapsed_since_end
            return DetectionResult(None, "detector", f"post-terminal hard suppress {remaining:.1f}s")
        if (
            session_started_this_poll
            and elapsed_since_end < POST_TERMINAL_COOLDOWN_SECONDS
            and not strong_new_session
            and not self._was_whatsapp_foreground_recently()
        ):
            remaining = POST_TERMINAL_COOLDOWN_SECONDS - elapsed_since_end
            return DetectionResult(None, "detector", f"post-terminal cooldown {remaining:.1f}s")

        # Update direction (best-effort; unknown is acceptable)
        self._maybe_apply_direction(self._classify_direction_from_state(win, state))

        # Track strong call UI presence for stale-ringing detection.
        # Strong UI = any proof the call is genuinely active/ringing/connecting.
        if state and (
            state.incoming or state.outgoing or state.answered
            or state.connecting or state.ringing or state.has_end_call_button
            or bool(state.timer_text)
        ):
            self._last_strong_call_ui_ts = now_ts

        # ── Immediate ENDED when status text says call is over ────────────
        # Guard: fire if a ring was emitted for this session OR if the hwnd
        # just changed from a tracked session (new_window=True, previous_hwnd
        # non-None) — covers the case where a non-answered session's hwnd changes
        # and the new window immediately shows "Call ended" (ring_event_emitted
        # was reset to False by the hwnd-change handler).
        if (
            state
            and state.status_text
            and _norm_label(state.status_text) in ENDED_LABELS
            and (
                self._ring_event_emitted
                or self._answered_event_emitted
                or (new_window and previous_hwnd is not None)
            )
        ):
            direction = self._call_direction or previous_direction or "unknown"
            number = self._caller_number
            log.info(
                "DETECTOR → ended by UI status | status=%s | dir=%s | number=%s | hwnd=%s",
                state.status_text, direction, number or "-", previous_hwnd or win.hwnd,
            )
            self._last_session_ended_ts = now_ts
            self._reset_internal_state()
            return DetectionResult(
                CallEvent.ENDED, scan_source,
                f"call ended by status text | dir={direction}",
                caller_number=number, direction=direction,
                hwnd=previous_hwnd or win.hwnd,
            )

        # ── Stale ringing/connecting timeout ──────────────────────────────
        # When ring was emitted but no answered proof exists and the call UI
        # stops showing any strong proof for ≥ STALE_RINGING_TIMEOUT_SECONDS,
        # the call is over (missed, cancelled, or window went stale).
        if (
            self._ring_event_emitted
            and not self._session_answered_proof_seen
            and self._last_strong_call_ui_ts > 0.0
            and now_ts - self._last_strong_call_ui_ts >= STALE_RINGING_TIMEOUT_SECONDS
        ):
            direction = self._call_direction or "unknown"
            number = self._caller_number
            elapsed = now_ts - self._last_strong_call_ui_ts
            log.info(
                "DETECTOR → session ended by stale ringing UI | last_strong=%.1fs | dir=%s | number=%s",
                elapsed, direction, number or "-",
            )
            self._last_session_ended_ts = now_ts
            self._reset_internal_state()
            return DetectionResult(
                CallEvent.ENDED, "detector",
                f"session ended by stale ringing UI | dir={direction}",
                caller_number=number, direction=direction,
                hwnd=win.hwnd,
            )

        # ── Emit ring event (first time window seen) ─────────────────────
        if not self._ring_event_emitted:
            # Never treat a "Call ended" window as a new call — return None so
            # main.py does not create a phantom session.
            _status_label = _norm_label(state.status_text or "") if state else ""
            if _status_label in ENDED_LABELS:
                log.info(
                    "DETECTOR → ended-status window; skipping ring emission | status=%s | hwnd=%s",
                    (state.status_text if state else "?"), win.hwnd,
                )
                return DetectionResult(
                    None, scan_source if state else "detector",
                    "ended status; ignored for ring emission",
                    hwnd=win.hwnd,
                    is_strong_new_call=False,
                )
            if self._call_direction is None:
                self._maybe_apply_direction(self._classify_direction_initial(win))
            ring_dir = self._call_direction or "unknown"
            if ring_dir == "incoming":
                event = CallEvent.INCOMING_RING
            elif ring_dir == "outgoing":
                event = CallEvent.OUTGOING_RING
            else:
                event = CallEvent.CALL_STARTED
            # is_strong_new_call is True for known directions; for unknown direction
            # require positive UIA proof (ringing/connecting/incoming/outgoing UI) —
            # a bare unknown window without call proof is not strong evidence of a
            # new call and must not split an existing live session in main.py.
            _strong_proof = bool(
                state and (
                    state.incoming or state.outgoing or state.ringing
                    or state.connecting or state.has_end_call_button
                    or (state.status_text and _norm_label(state.status_text) in RINGING_LABELS)
                )
            )
            _is_strong_new_call = ring_dir != "unknown" or _strong_proof
            self._ring_event_emitted = True
            if self._last_strong_call_ui_ts <= 0.0:
                self._last_strong_call_ui_ts = now_ts
            log.info(
                "DETECTOR → ring emitted | dir=%s | hwnd=%s | scan=%s | strong=%s",
                ring_dir, win.hwnd, scan_source if state else "none", _is_strong_new_call,
            )
            return DetectionResult(
                event,
                scan_source if state else "detector",
                f"call window | {win.width}x{win.height} | dir={ring_dir}",
                caller_number=self._caller_number,
                direction=self._call_direction or "unknown",
                hwnd=win.hwnd,
                is_new_window=new_window,
                is_strong_new_call=_is_strong_new_call,
            )

        # ── Lock answered state when timer proof seen ────────────────────
        # Note: no_answer does NOT block this — it's informational only.
        if (
            state and state.answered
            and not self._session_answered_proof_seen
        ):
            self._session_answered_proof_seen = True
            self._session_answered_timer = state.timer_text
            self._session_answered_at_ts = now_ts
            self._answered_event_emitted = False
            self._direction_frozen = True
            log.info(
                "DETECTOR → ACTIVE locked (timer proof) | timer=%s | dir=%s | number=%s | source=%s",
                state.timer_text or "-",
                self._call_direction or "?",
                self._caller_number or "-",
                scan_source,
            )

        # ── Emit ANSWERED event (once) when timer proof confirmed ────────
        if self._session_answered_proof_seen:
            if state:
                self._log_phase_once("answered", state.details)
            if not self._answered_event_emitted:
                self._answered_event_emitted = True
                return DetectionResult(
                    CallEvent.ANSWERED,
                    scan_source,
                    f"answered | timer={self._session_answered_timer or '-'} | dir={self._call_direction or '?'}",
                    caller_number=self._caller_number,
                    direction=self._call_direction,
                    hwnd=win.hwnd,
                )
            return DetectionResult(
                None, "detector",
                f"answered active | timer={self._session_answered_timer or '-'}",
                caller_number=self._caller_number,
                direction=self._call_direction,
                hwnd=win.hwnd,
            )

        # ── Log ongoing phase ────────────────────────────────────────────
        if state:
            if state.no_answer:
                self._log_phase_once("no_answer_pending", state.details)
            elif state.connecting:
                self._log_phase_once("connecting", state.details)
            elif state.ringing:
                p = "ringing_incoming" if self._call_direction == "incoming" else "ringing_outgoing"
                self._log_phase_once(p, state.details)
            else:
                self._log_phase_once("call_present", state.details)
        else:
            log.debug("DETECTOR → scan unavailable; direction=unknown; session preserved; recording continues")

        return DetectionResult(
            None, "detector",
            state.details if state else "scan unavailable; direction unknown; recording continues",
            caller_number=self._caller_number,
            direction=self._call_direction,
            hwnd=win.hwnd,
        )

    # ── pywinauto fallback scan ──────────────────────────────────────────

    def _pywinauto_scan(self, hwnd: int) -> Optional[_UIAState]:
        if Desktop is None:
            return None
        scan_start = time.time()
        try:
            window = Desktop(backend="uia").window(handle=hwnd)
            elements = []
            try:
                elements = window.descendants(depth=12)
            except Exception:
                return None

            labels: set[str] = set()
            phone_candidates: list[str] = []
            timer_candidates: list[str] = []
            status_candidates: list[str] = []
            debug_hits: list[str] = []
            has_end_call = False
            call_status_raw: Optional[str] = None

            for elem in elements:
                try:
                    info = getattr(elem, "element_info", None)
                    auto_id = str(getattr(info, "automation_id", "") or "")
                    name = str(getattr(info, "name", "") or "")
                except Exception:
                    continue
                norm = _norm_label(name)
                norm_s = _norm_status(name)
                if norm:
                    labels.add(norm)
                if TIMER_RE.match(norm_s):
                    timer_candidates.append(norm_s)
                if PHONE_RE.search(name):
                    phone_candidates.append(re.sub(r"\s+", " ", name.strip()))
                if auto_id == CALL_STATUS_AUTO_ID:
                    call_status_raw = norm_s
                    if norm_s:
                        status_candidates.insert(0, norm_s)
                elif auto_id in (CALLER_TEXT_AUTO_ID, PARTICIPANT_LIST_AUTO_ID):
                    if norm_s:
                        status_candidates.insert(0, norm_s)
                if auto_id == END_CALL_AUTO_ID:
                    has_end_call = True

            timer_text = next((t for t in timer_candidates if TIMER_RE.match(t)), None)
            status_text = timer_text or (status_candidates[0] if status_candidates else None)
            norm_st = _norm_label(status_text or "")

            # Check outgoing from CallStatusText in pywinauto too
            call_status_label = _norm_label(call_status_raw or "")
            status_is_outgoing_ring = (
                call_status_label in RINGING_LABELS
                or any(lbl in call_status_label for lbl in RINGING_LABELS if lbl)
            )

            state = _UIAState(source="pywinauto", debug_hits=debug_hits)
            state.incoming = bool(labels & ACCEPT_LABELS and labels & DECLINE_LABELS)
            state.outgoing = bool(
                ((labels & RINGING_LABELS) or status_is_outgoing_ring)
                and not state.incoming
            )
            state.timer_text = timer_text
            state.status_text = status_text
            state.call_status_raw = call_status_raw
            state.answered = timer_text is not None
            state.ringing = state.incoming or state.outgoing or norm_st in RINGING_LABELS
            state.connecting = norm_st in CONNECTING_LABELS and not state.answered
            state.no_answer = norm_st in NO_ANSWER_LABELS    # informational only
            state.has_end_call_button = has_end_call
            state.caller_number = phone_candidates[0] if phone_candidates else None
            state.scan_ms = (time.time() - scan_start) * 1000

            detail_parts = [f"answered={state.answered}", f"elements={len(elements)}"]
            if state.status_text:
                detail_parts.append(f"status={state.status_text}")
            if state.timer_text:
                detail_parts.append(f"timer={state.timer_text}")
            if state.no_answer:
                detail_parts.append("no_answer=True")
            state.details = " | ".join(detail_parts)
            return state
        except Exception as exc:
            log.warning("DETECTOR → pywinauto fallback failed: %s", exc)
            return None
