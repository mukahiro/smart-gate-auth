from __future__ import annotations

import logging
import threading
import time
from typing import Protocol


class Display(Protocol):
    def show(self, line1: str, line2: str = "") -> None: ...
    def close(self) -> None: ...


class ConsoleLcd:
    """Safe fallback until the LCD model and character encoding are selected."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def show(self, line1: str, line2: str = "") -> None:
        with self._lock:
            logging.info("LCD | %s | %s", line1, line2)

    def close(self) -> None:
        pass


class I2cLcd:
    """HD44780-compatible LCD2004 through a PCF8574 I2C backpack."""

    _RS = 0x01
    _ENABLE = 0x04
    _BACKLIGHT = 0x08
    _ROW_OFFSETS = (0x00, 0x40, 0x14, 0x54)
    _PHRASES = {
        "入室/退出を選択": "IN / OUT WO SENTAKU",
        "顔を向けるか": "KAO OR CARD",
        "カードをかざしてください": "CARD WO KAZASU",
        "認証しています": "AUTHENTICATING...",
        "認証できませんでした": "AUTH FAILED",
        "もう一度操作してください": "PLEASE TRY AGAIN",
        "端末エラー": "DEVICE ERROR",
        "管理者に連絡": "CALL ADMINISTRATOR",
        "通信エラー 未記録": "NETWORK: NOT SAVED",
        "カードを離してください": "REMOVE CARD",
        "入室しました": "CHECK IN OK",
        "退出しました": "CHECK OUT OK",
        "認証しました": "AUTHENTICATED",
    }

    def __init__(
        self,
        *,
        bus_number: int = 1,
        address: int = 0x27,
        columns: int = 20,
        rows: int = 4,
        bus: object | None = None,
    ) -> None:
        if not 0x03 <= address <= 0x77:
            raise ValueError("LCD I2C address must be a 7-bit address")
        if columns <= 0 or not 1 <= rows <= 4:
            raise ValueError("LCD dimensions are invalid")
        if bus is None:
            try:
                from smbus2 import SMBus
            except ImportError:
                try:
                    from smbus import SMBus
                except ImportError as exc:
                    raise RuntimeError("smbus2 or python3-smbus is not installed") from exc
            bus = SMBus(bus_number)
        self._bus = bus
        self._address = address
        self._columns = columns
        self._rows = rows
        self._backlight = self._BACKLIGHT
        self._lock = threading.Lock()
        self._closed = False
        self._initialize()

    def _write_expander(self, value: int) -> None:
        self._bus.write_byte(self._address, value | self._backlight)

    def _pulse_enable(self, value: int) -> None:
        self._write_expander(value | self._ENABLE)
        time.sleep(0.000001)
        self._write_expander(value & ~self._ENABLE)
        time.sleep(0.00005)

    def _write_nibble(self, value: int) -> None:
        self._write_expander(value)
        self._pulse_enable(value)

    def _send(self, value: int, mode: int = 0) -> None:
        self._write_nibble((value & 0xF0) | mode)
        self._write_nibble(((value << 4) & 0xF0) | mode)

    def _command(self, value: int) -> None:
        self._send(value)
        if value in (0x01, 0x02):
            time.sleep(0.002)

    def _initialize(self) -> None:
        # HD44780 power-on and 8-bit to 4-bit initialization sequence.
        time.sleep(0.05)
        self._write_expander(0)
        # Match the reference LiquidCrystal_I2C sequence. Some inexpensive
        # LCD2004/backpack combinations need substantially longer than the
        # HD44780 minimum after the expander first becomes accessible.
        time.sleep(1.0)
        self._write_nibble(0x30)
        time.sleep(0.0045)
        self._write_nibble(0x30)
        time.sleep(0.0045)
        self._write_nibble(0x30)
        time.sleep(0.00015)
        self._write_nibble(0x20)
        self._command(0x28 if self._rows > 1 else 0x20)
        self._command(0x0C)
        self._command(0x01)
        self._command(0x06)

    @classmethod
    def _encode_text(cls, value: str, width: int) -> bytes:
        value = cls._PHRASES.get(value, value)
        encoded = bytearray()
        for character in value:
            codepoint = ord(character)
            if 0x20 <= codepoint <= 0x7E:
                encoded.append(codepoint)
            elif 0xFF61 <= codepoint <= 0xFF9F:
                # HD44780 Japanese ROM codes match JIS X 0201 kana here.
                encoded.append(codepoint - 0xFF61 + 0xA1)
            else:
                encoded.append(ord("?"))
            if len(encoded) == width:
                break
        return bytes(encoded).ljust(width, b" ")

    def _set_cursor(self, column: int, row: int) -> None:
        self._command(0x80 | (self._ROW_OFFSETS[row] + column))

    def show(self, line1: str, line2: str = "") -> None:
        lines = (line1, line2, "", "")
        with self._lock:
            if self._closed:
                raise RuntimeError("LCD is closed")
            for row in range(self._rows):
                self._set_cursor(0, row)
                for value in self._encode_text(lines[row], self._columns):
                    self._send(value, self._RS)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._command(0x01)
                self._backlight = 0
                self._write_expander(0)
            finally:
                self._closed = True
                self._bus.close()


class ResilientDisplay:
    """Keep authentication running if the LCD disconnects at runtime."""

    def __init__(self, primary: Display, fallback: Display | None = None) -> None:
        self._primary = primary
        self._fallback = fallback or ConsoleLcd()
        self._failed = False
        self._lock = threading.Lock()

    def show(self, line1: str, line2: str = "") -> None:
        with self._lock:
            if not self._failed:
                try:
                    self._primary.show(line1, line2)
                    return
                except Exception:
                    self._failed = True
                    logging.exception("LCD unavailable; using log output")
            self._fallback.show(line1, line2)

    def close(self) -> None:
        try:
            self._primary.close()
        except Exception:
            logging.exception("failed to close LCD")
        self._fallback.close()
