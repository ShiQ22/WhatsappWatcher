from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import getpass
import platform
import socket
from typing import Optional


_CACHED_PC_USER = getpass.getuser()
_CACHED_MACHINE_NAME = platform.node()


class CallState(str, Enum):
    IDLE = "idle"
    RINGING_UNKNOWN = "ringing_unknown"
    RINGING_INCOMING = "ringing_incoming"
    RINGING_OUTGOING = "ringing_outgoing"
    CONNECTING = "connecting"
    ACTIVE = "active"
    ENDED = "ended"
    RECORDER_ERROR = "recorder_error"
    DETECTOR_ERROR = "detector_error"


class CallEvent(str, Enum):
    CALL_STARTED = "call_started"
    INCOMING_RING = "incoming_ring"
    OUTGOING_RING = "outgoing_ring"
    ANSWERED = "answered"
    CONNECTING = "connecting"
    ENDED = "ended"
    MISSED = "missed"
    CANCELLED = "cancelled"
    DETECTOR_FAIL = "detector_fail"
    RECORDER_FAIL = "recorder_fail"
    RESET = "reset"


def _get_machine_ip() -> str:
    try:
        hostname = socket.gethostname()
        addresses = socket.getaddrinfo(hostname, None, family=socket.AF_INET)
        for addr in addresses:
            ip = addr[4][0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if ip:
                return ip
    except Exception:
        pass
    return "-"


_CACHED_MACHINE_IP = _get_machine_ip()


@dataclass
class CallSession:
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    status: str = "idle"
    direction: Optional[str] = None
    pc_user: str = field(default_factory=lambda: _CACHED_PC_USER)
    machine_name: str = field(default_factory=lambda: _CACHED_MACHINE_NAME)
    machine_ip: str = field(default_factory=lambda: _CACHED_MACHINE_IP)
    caller_number: Optional[str] = None
    recording_path: Optional[str] = None
    uploaded_path: Optional[str] = None
    error_details: Optional[str] = None

    def duration_seconds(self) -> int:
        if not self.started_at or not self.ended_at:
            return 0
        return max(0, int((self.ended_at - self.started_at).total_seconds()))


@dataclass
class StateMachine:
    state: CallState = CallState.IDLE
    session: CallSession = field(default_factory=CallSession)

    def _ensure_started(self, now: datetime, direction: Optional[str] = None) -> None:
        if self.session.started_at is None:
            self.session.started_at = now
        if direction:
            self.session.direction = direction

    def transition(self, event: CallEvent, error_details: Optional[str] = None) -> CallState:
        now = datetime.now()

        if event == CallEvent.RESET:
            self.state = CallState.IDLE
            self.session = CallSession()
            return self.state

        if self.state == CallState.IDLE:
            if event == CallEvent.CALL_STARTED:
                self.state = CallState.RINGING_UNKNOWN
                self.session = CallSession(started_at=now, status=self.state.value, direction="unknown")
            elif event == CallEvent.INCOMING_RING:
                self.state = CallState.RINGING_INCOMING
                self.session = CallSession(started_at=now, status=self.state.value, direction="incoming")
            elif event == CallEvent.OUTGOING_RING:
                self.state = CallState.RINGING_OUTGOING
                self.session = CallSession(started_at=now, status=self.state.value, direction="outgoing")
            elif event == CallEvent.ANSWERED:
                self.state = CallState.ACTIVE
                self.session = CallSession(started_at=now, status=self.state.value, direction=self.session.direction or "unknown")
            elif event == CallEvent.CONNECTING:
                self.state = CallState.CONNECTING
                self.session = CallSession(started_at=now, status=self.state.value, direction=self.session.direction or "unknown")
            elif event == CallEvent.DETECTOR_FAIL:
                self.state = CallState.DETECTOR_ERROR
                self.session.error_details = error_details
                self.session.status = self.state.value
                self.session.ended_at = now
            return self.state

        if self.state in (
            CallState.RINGING_UNKNOWN,
            CallState.RINGING_INCOMING,
            CallState.RINGING_OUTGOING,
            CallState.CONNECTING,
        ):
            self._ensure_started(now)
            if event == CallEvent.INCOMING_RING:
                self.state = CallState.RINGING_INCOMING
                self.session.status = self.state.value
                self.session.direction = "incoming"
            elif event == CallEvent.OUTGOING_RING:
                self.state = CallState.RINGING_OUTGOING
                self.session.status = self.state.value
                self.session.direction = "outgoing"
            elif event == CallEvent.CALL_STARTED:
                self.state = CallState.RINGING_UNKNOWN
                self.session.status = self.state.value
                self.session.direction = self.session.direction or "unknown"
            elif event == CallEvent.ANSWERED:
                self.state = CallState.ACTIVE
                self.session.status = self.state.value
            elif event == CallEvent.CONNECTING:
                self.state = CallState.CONNECTING
                self.session.status = self.state.value
            elif event in (CallEvent.MISSED, CallEvent.CANCELLED, CallEvent.ENDED):
                self.state = CallState.ENDED
                self.session.ended_at = now
                self.session.status = self.state.value
            elif event == CallEvent.RECORDER_FAIL:
                self.state = CallState.RECORDER_ERROR
                self.session.error_details = error_details
                self.session.ended_at = now
                self.session.status = self.state.value
            elif event == CallEvent.DETECTOR_FAIL:
                self.state = CallState.DETECTOR_ERROR
                self.session.error_details = error_details
                self.session.ended_at = now
                self.session.status = self.state.value
            return self.state

        if self.state == CallState.ACTIVE:
            self._ensure_started(now)
            if event == CallEvent.ENDED:
                self.state = CallState.ENDED
                self.session.ended_at = now
                self.session.status = self.state.value
            elif event == CallEvent.RECORDER_FAIL:
                self.state = CallState.RECORDER_ERROR
                self.session.error_details = error_details
                self.session.ended_at = now
                self.session.status = self.state.value
            elif event == CallEvent.DETECTOR_FAIL:
                self.state = CallState.DETECTOR_ERROR
                self.session.error_details = error_details
                self.session.ended_at = now
                self.session.status = self.state.value
            return self.state

        return self.state

    def is_terminal_state(self) -> bool:
        return self.state in (
            CallState.ENDED,
            CallState.RECORDER_ERROR,
            CallState.DETECTOR_ERROR,
        )
