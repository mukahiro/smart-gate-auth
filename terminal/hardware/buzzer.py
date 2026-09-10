from __future__ import annotations

import logging
import threading
import time
from typing import Literal, Protocol


Sound = Literal["accepted", "success", "auth_failed", "api_failed", "device_error"]


class Buzzer(Protocol):
    def play(self, sound: Sound) -> None: ...
    def close(self) -> None: ...


class ConsoleBuzzer:
    def play(self, sound: Sound) -> None:
        logging.info("BUZZER | %s", sound)

    def close(self) -> None:
        pass


class GpioBuzzer:
    """Active buzzer on BCM GPIO 18. Calls are serialized."""

    _PATTERNS: dict[Sound, tuple[float, ...]] = {
        "accepted": (0.06,),
        "success": (0.08, 0.08, 0.08),
        "auth_failed": (0.18, 0.10, 0.18),
        "api_failed": (0.65,),
        "device_error": (0.20, 0.10, 0.20, 0.10, 0.20),
    }

    def __init__(self, pin: int = 18):
        try:
            import RPi.GPIO as gpio
        except ImportError as exc:
            raise RuntimeError("RPi.GPIO is not installed") from exc
        self._gpio = gpio
        self._pin = pin
        self._lock = threading.Lock()
        gpio.setmode(gpio.BCM)
        gpio.setup(pin, gpio.OUT, initial=gpio.LOW)

    def play(self, sound: Sound) -> None:
        with self._lock:
            pattern = self._PATTERNS[sound]
            for index, duration in enumerate(pattern):
                self._gpio.output(self._pin, self._gpio.HIGH if index % 2 == 0 else self._gpio.LOW)
                time.sleep(duration)
            self._gpio.output(self._pin, self._gpio.LOW)

    def close(self) -> None:
        self._gpio.output(self._pin, self._gpio.LOW)
        self._gpio.cleanup(self._pin)

