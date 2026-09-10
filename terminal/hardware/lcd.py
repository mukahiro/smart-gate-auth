from __future__ import annotations

import logging
import threading
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

