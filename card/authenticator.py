from __future__ import annotations

import re
import threading
import time
from typing import Any, Sequence

from terminal.models import AuthenticationResult

try:
    from smartcard.System import readers
    from smartcard.pcsc.PCSCPart10 import FEATURE_CCID_ESC_COMMAND, getFeatureRequest, hasFeature
    from smartcard.scard import SCARD_SHARE_DIRECT
except ImportError as exc:
    _IMPORT_ERROR: ImportError | None = exc
else:
    _IMPORT_ERROR = None

SYSTEM_CODE = 0xFE00
SERVICE_CODE = 0x1A8B
START_SESSION = [0xFF, 0xC2, 0x00, 0x00, 0x02, 0x81, 0x00, 0x00]
END_SESSION = [0xFF, 0xC2, 0x00, 0x00, 0x02, 0x82, 0x00, 0x00]
SWITCH_TO_FELICA = [0xFF, 0xC2, 0x00, 0x02, 0x04, 0x8F, 0x02, 0x03, 0x00, 0x00]


class CardReadError(RuntimeError):
    pass


def find_tlv(data: Sequence[int], target: int) -> list[int] | None:
    values = list(data)
    if len(values) >= 2 and values[-2:] == [0x90, 0x00]:
        values = values[:-2]
    offset = 0
    while offset + 2 <= len(values):
        tag, length = values[offset : offset + 2]
        end = offset + 2 + length
        if end > len(values):
            return None
        if tag == target:
            return values[offset + 2 : end]
        offset = end
    return None


def poll_fcf(connection: Any, escape_code: int) -> list[int] | None:
    packet = [0x06, 0x00, 0xFE, 0x00, 0x00, 0x00]
    command = [0xFF, 0xC2, 0x00, 0x01, len(packet) + 2, 0x95, len(packet), *packet]
    try:
        response = connection.control(escape_code, command)
    except Exception:
        return None
    value = find_tlv(response, 0x97)
    if value is None or len(value) < 18 or value[0] != len(value) or value[1] != 0x01:
        return None
    return value[2:10]


def read_student_number(connection: Any, escape_code: int, idm: Sequence[int]) -> str:
    if len(idm) != 8:
        raise CardReadError("invalid IDm length")
    payload = [0x06, *idm, 0x01, SERVICE_CODE & 0xFF, SERVICE_CODE >> 8, 0x01, 0x80, 0x00]
    packet = [len(payload) + 1, *payload]
    command = [0xFF, 0xC2, 0x00, 0x01, len(packet) + 2, 0x95, len(packet), *packet]
    value = find_tlv(connection.control(escape_code, command), 0x97)
    if value is None or len(value) < 29 or value[0] != len(value) or value[1] != 0x07:
        raise CardReadError("invalid FCF response")
    if value[2:10] != list(idm) or value[10:12] != [0, 0] or value[12] != 1:
        raise CardReadError("FCF read failed")
    try:
        student_number = bytes(value[15:27]).decode("ascii").rstrip("\x00 ")
    except UnicodeDecodeError as exc:
        raise CardReadError("student number is not ASCII") from exc
    if not re.fullmatch(r"[0-9]{10}", student_number):
        raise CardReadError("student number is not 10 digits")
    return student_number


class CardAuthenticator:
    """Persistent RC-S300 connection; no attendance state or event storage."""

    def __init__(self, poll_interval: float = 0.2):
        if _IMPORT_ERROR is not None:
            raise RuntimeError("pyscard is not installed") from _IMPORT_ERROR
        available = list(readers())
        reader = next((item for item in available if "RC-S300" in str(item)), None)
        if reader is None:
            raise RuntimeError("RC-S300 was not found")
        self._connection = reader.createConnection()
        self._connection.connect(mode=SCARD_SHARE_DIRECT)
        feature = hasFeature(getFeatureRequest(self._connection), FEATURE_CCID_ESC_COMMAND)
        if feature is None:
            self._connection.disconnect()
            raise RuntimeError("FEATURE_CCID_ESC_COMMAND is unavailable")
        self._escape_code = feature
        self._connection.control(feature, START_SESSION)
        self._connection.control(feature, SWITCH_TO_FELICA)
        self._poll_interval = poll_interval
        self._closed = False

    def authenticate(self, timeout: float, cancel: threading.Event) -> AuthenticationResult | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not cancel.is_set():
            idm = poll_fcf(self._connection, self._escape_code)
            if idm is not None:
                return AuthenticationResult(read_student_number(self._connection, self._escape_code, idm), "card")
            cancel.wait(self._poll_interval)
        return None

    def wait_for_removal(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if poll_fcf(self._connection, self._escape_code) is None:
                return
            time.sleep(self._poll_interval)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._connection.control(self._escape_code, END_SESSION)
        finally:
            self._connection.disconnect()

