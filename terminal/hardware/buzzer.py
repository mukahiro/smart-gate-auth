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

    # (frequency ratio against the configured resonance, duration, following gap)
    _MELODIES: dict[Sound, tuple[tuple[float, float, float], ...]] = {
        "accepted": ((1.00, 0.07, 0.00),),
        "success": (
            (0.80, 0.08, 0.035),
            (1.00, 0.10, 0.035),
            (1.20, 0.16, 0.00),
        ),
        "auth_failed": (
            (1.00, 0.14, 0.06),
            (0.72, 0.24, 0.00),
        ),
        "api_failed": (
            (0.75, 0.22, 0.08),
            (0.62, 0.34, 0.00),
        ),
        "device_error": (
            (0.58, 0.15, 0.07),
            (0.58, 0.15, 0.07),
            (0.58, 0.28, 0.00),
        ),
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
        self._frequency_hz = frequency_hz
        self._lock = threading.Lock()
        gpio.setmode(gpio.BCM)
        gpio.setup(pin, gpio.OUT, initial=gpio.LOW)
        self._pwm = gpio.PWM(pin, frequency_hz)

    def play(self, sound: Sound) -> None:
        with self._lock:
            try:
                self._pwm.start(0.0)
                for ratio, duration, gap in self._MELODIES[sound]:
                    frequency = max(1, round(self._frequency_hz * ratio))
                    self._pwm.ChangeFrequency(frequency)
                    self._pwm.ChangeDutyCycle(50.0)
                    time.sleep(duration)
                    self._pwm.ChangeDutyCycle(0.0)
                    if gap > 0:
                        time.sleep(gap)
                if sound == "accepted":
                    # Leave a tiny tail so the single acknowledgement does not
                    # sound like an electrical click.
                    time.sleep(0.015)
            finally:
                self._pwm.ChangeDutyCycle(0.0)
                self._pwm.stop()
                self._gpio.output(self._pin, self._gpio.LOW)

    def close(self) -> None:
        self._pwm.stop()
        self._gpio.output(self._pin, self._gpio.LOW)
        self._gpio.cleanup(self._pin)
