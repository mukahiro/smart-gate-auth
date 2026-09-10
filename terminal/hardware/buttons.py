from __future__ import annotations

import time
from typing import Callable

from terminal.models import EventType


class ButtonController:
    """Two active-low buttons with callback-side debouncing only."""

    def __init__(
        self,
        callback: Callable[[EventType], None],
        *,
        check_in_pin: int = 17,
        check_out_pin: int = 27,
        debounce_ms: int = 250,
    ):
        try:
            import RPi.GPIO as gpio
        except ImportError as exc:
            raise RuntimeError("RPi.GPIO is not installed") from exc
        self._gpio = gpio
        self._pins = (check_in_pin, check_out_pin)
        self._callback = callback
        self._last_press = 0.0
        self._debounce = debounce_ms / 1000.0
        gpio.setmode(gpio.BCM)
        for pin in self._pins:
            gpio.setup(pin, gpio.IN, pull_up_down=gpio.PUD_UP)
        gpio.add_event_detect(check_in_pin, gpio.FALLING, callback=lambda _: self._pressed("check_in"), bouncetime=debounce_ms)
        gpio.add_event_detect(check_out_pin, gpio.FALLING, callback=lambda _: self._pressed("check_out"), bouncetime=debounce_ms)

    def _pressed(self, event_type: EventType) -> None:
        now = time.monotonic()
        if now - self._last_press < self._debounce:
            return
        self._last_press = now
        self._callback(event_type)

    def close(self) -> None:
        for pin in self._pins:
            self._gpio.remove_event_detect(pin)
            self._gpio.cleanup(pin)

