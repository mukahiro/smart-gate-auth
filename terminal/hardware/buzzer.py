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
    """Passive piezo sounder driven by PWM on BCM GPIO 18."""

    _PATTERNS: dict[Sound, tuple[float, ...]] = {
        "accepted": (0.06,),
        "success": (0.08, 0.08, 0.08),
        "auth_failed": (0.18, 0.10, 0.18),
        "api_failed": (0.65,),
        "device_error": (0.20, 0.10, 0.20, 0.10, 0.20),
    }

    def __init__(self, pin: int = 18, frequency_hz: int = 4000):
        if frequency_hz <= 0:
            raise ValueError("buzzer frequency must be positive")
        try:
            import RPi.GPIO as gpio
        except ImportError as exc:
            raise RuntimeError("RPi.GPIO is not installed") from exc
        self._gpio = gpio
        self._pin = pin
        self._lock = threading.Lock()
        gpio.setmode(gpio.BCM)
        gpio.setup(pin, gpio.OUT, initial=gpio.LOW)
        self._pwm = gpio.PWM(pin, frequency_hz)

    def play(self, sound: Sound) -> None:
        with self._lock:
            pattern = self._PATTERNS[sound]
            try:
                self._pwm.start(0.0)
                for index, duration in enumerate(pattern):
                    if index % 2 == 0:
                        # PKM13EPYH4000-A0 is a passive 4 kHz piezo sounder.
                        # A static HIGH only produces a click at each edge.
                        self._pwm.ChangeDutyCycle(50.0)
                    else:
                        self._pwm.ChangeDutyCycle(0.0)
                    time.sleep(duration)
            finally:
                self._pwm.ChangeDutyCycle(0.0)
                self._pwm.stop()
                self._gpio.output(self._pin, self._gpio.LOW)

    def close(self) -> None:
        self._pwm.stop()
        self._gpio.output(self._pin, self._gpio.LOW)
        self._gpio.cleanup(self._pin)
