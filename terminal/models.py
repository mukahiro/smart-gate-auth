from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import uuid4


AuthMethod = Literal["face", "card"]
EventType = Literal["check_in", "check_out"]


class TerminalState(str, Enum):
    IDLE = "IDLE"
    AUTHENTICATING = "AUTHENTICATING"
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    API_FAILED = "API_FAILED"
    AUTH_FAILED = "AUTH_FAILED"
    DEVICE_ERROR = "DEVICE_ERROR"
    COOLDOWN = "COOLDOWN"


@dataclass(frozen=True)
class AuthenticationResult:
    student_number: str
    method: AuthMethod
    confidence: float | None = None

    def __post_init__(self) -> None:
        if self.method not in ("face", "card"):
            raise ValueError("method must be face or card")
        if len(self.student_number) != 10 or not self.student_number.isascii() or not self.student_number.isdigit():
            raise ValueError("student_number must be exactly 10 ASCII digits")
        if self.method == "face":
            if self.confidence is None or not 0.0 <= self.confidence <= 1.0:
                raise ValueError("face authentication requires confidence between 0 and 1")
        elif self.confidence is not None:
            raise ValueError("card authentication must not include confidence")


@dataclass(frozen=True)
class AttendanceEvent:
    event_id: str
    student_number: str
    device_id: str
    method: AuthMethod
    event_type: EventType
    authenticated_at: str
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.event_id or not self.device_id:
            raise ValueError("event_id and device_id must not be empty")
        if self.event_type not in ("check_in", "check_out"):
            raise ValueError("event_type must be check_in or check_out")
        # Reuse the same identity/method/confidence validation for events made
        # directly by callers as well as from_authentication().
        AuthenticationResult(self.student_number, self.method, self.confidence)

    @classmethod
    def from_authentication(
        cls,
        result: AuthenticationResult,
        *,
        device_id: str,
        event_type: EventType,
    ) -> "AttendanceEvent":
        return cls(
            event_id=str(uuid4()),
            student_number=result.student_number,
            device_id=device_id,
            method=result.method,
            event_type=event_type,
            authenticated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            confidence=result.confidence,
        )

    def payload(self) -> dict[str, str | float]:
        payload: dict[str, str | float] = {
            "eventId": self.event_id,
            "studentNumber": self.student_number,
            "deviceId": self.device_id,
            "method": self.method,
            "eventType": self.event_type,
            "authenticatedAt": self.authenticated_at,
        }
        if self.confidence is not None:
            payload["confidence"] = self.confidence
        return payload
