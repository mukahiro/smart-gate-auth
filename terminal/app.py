from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import Any

from terminal.attendance_api import AttendanceApi
from terminal.hardware.buttons import ButtonController
from terminal.hardware.buzzer import ConsoleBuzzer, GpioBuzzer
from terminal.hardware.lcd import ConsoleLcd
from terminal.models import EventType
from terminal.state_machine import SessionController


class TerminalApp:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.api = AttendanceApi(args.api_url, args.token, args.api_timeout)
        self.display = ConsoleLcd()
        self.buzzer = ConsoleBuzzer() if args.console_hardware else self._make_buzzer()
        self.authenticators: list[Any] = []
        self.buttons: ButtonController | None = None
        self.controller: SessionController | None = None
        self._queue: asyncio.Queue[EventType | None] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None

    def _make_buzzer(self) -> Any:
        try:
            return GpioBuzzer(self.args.buzzer_pin, self.args.buzzer_frequency)
        except Exception:
            logging.exception("buzzer unavailable; using log output")
            return ConsoleBuzzer()

    def _load_authenticators(self) -> None:
        if not self.args.disable_card:
            try:
                from card.reader import CardAuthenticator

                self.authenticators.append(CardAuthenticator())
            except Exception:
                logging.exception("card authentication unavailable")
        if not self.args.disable_face:
            try:
                from face.authenticator import FaceAuthenticator

                self.authenticators.append(
                    FaceAuthenticator(
                        Path(self.args.face_db),
                        model_name=self.args.face_model,
                        threshold=self.args.face_threshold,
                    )
                )
            except Exception:
                logging.exception("face authentication unavailable")
        if not self.authenticators:
            raise RuntimeError("no authentication device is available")

    def _button_pressed(self, event_type: EventType) -> None:
        # GPIO callbacks execute on a driver thread. Ignore input while a session
        # is active instead of leaving a future session queued.
        if self._loop is not None and self.controller is not None and self.controller.is_idle:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, event_type)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._load_authenticators()
        self.controller = SessionController(
            authenticators=self.authenticators,
            api=self.api,
            display=self.display,
            buzzer=self.buzzer,
            device_id=self.args.device_id,
            authentication_timeout=self.args.auth_timeout,
            result_display_seconds=self.args.result_seconds,
        )
        if self.args.console_buttons:
            asyncio.create_task(self._console_input())
        else:
            self.buttons = ButtonController(
                self._button_pressed,
                check_in_pin=self.args.check_in_pin,
                check_out_pin=self.args.check_out_pin,
                debounce_ms=self.args.debounce_ms,
            )
        await self.controller._idle()
        while True:
            event_type = await self._queue.get()
            if event_type is None:
                break
            await self.controller.run_session(event_type)

    async def _console_input(self) -> None:
        while True:
            value = (await asyncio.to_thread(input, "[i]入室 [o]退出 [q]終了 > ")).strip().lower()
            if value == "q":
                await self._queue.put(None)
                return
            if value in ("i", "o") and self.controller is not None and self.controller.is_idle:
                await self._queue.put("check_in" if value == "i" else "check_out")

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)

    def close(self) -> None:
        if self.controller is not None:
            self.controller.close()
        for authenticator in reversed(self.authenticators):
            try:
                authenticator.close()
            except Exception:
                logging.exception("failed to close authenticator")
        if self.buttons is not None:
            self.buttons.close()
        self.display.close()
        self.buzzer.close()
        self.api.close()


def parse_args() -> argparse.Namespace:
    base_url = os.getenv("AUTH_API_BASE_URL", "http://localhost:3000")
    endpoint = os.getenv("AUTH_API_ATTENDANCE_ENDPOINT", "/api/v1/attendance-events")
    parser = argparse.ArgumentParser(description="Smart Gate integrated authentication terminal")
    parser.add_argument("--api-url", default=f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}")
    parser.add_argument("--token", default=os.getenv("AUTH_APP_BEARER_TOKEN", ""))
    parser.add_argument("--device-id", default=os.getenv("AUTH_DEVICE_ID", "smart-gate-terminal-01"))
    parser.add_argument("--api-timeout", type=float, default=3.0)
    parser.add_argument("--auth-timeout", type=float, default=8.0)
    parser.add_argument("--result-seconds", type=float, default=2.0)
    parser.add_argument("--face-db", default=os.getenv("FACE_AUTH_DB_PATH", "face/face.db"))
    parser.add_argument("--face-model", default=os.getenv("FACE_AUTH_MODEL_NAME", "buffalo_sc"))
    parser.add_argument("--face-threshold", type=float, default=float(os.getenv("FACE_AUTH_THRESHOLD", "0.5")))
    parser.add_argument("--check-in-pin", type=int, default=17)
    parser.add_argument("--check-out-pin", type=int, default=27)
    parser.add_argument("--buzzer-pin", type=int, default=18)
    parser.add_argument("--buzzer-frequency", type=int, default=4000)
    parser.add_argument("--debounce-ms", type=int, default=250)
    parser.add_argument("--disable-card", action="store_true")
    parser.add_argument("--disable-face", action="store_true")
    parser.add_argument("--console-buttons", action="store_true")
    parser.add_argument("--console-hardware", action="store_true")
    args = parser.parse_args()
    if not args.token:
        parser.error("AUTH_APP_BEARER_TOKEN or --token is required")
    if not args.device_id.strip() or args.api_timeout <= 0 or args.auth_timeout <= 0:
        parser.error("device-id must be non-empty and timeouts must be positive")
    if args.result_seconds < 0 or args.debounce_ms < 0:
        parser.error("result-seconds and debounce-ms must not be negative")
    if args.buzzer_frequency <= 0:
        parser.error("buzzer-frequency must be positive")
    return args


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = TerminalApp(parse_args())
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, app.stop)
    try:
        await app.run()
    finally:
        app.close()


if __name__ == "__main__":
    asyncio.run(_main())
