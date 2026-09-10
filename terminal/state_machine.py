from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Protocol

from .attendance_api import AttendanceApiError
from .hardware.buzzer import Buzzer
from .hardware.lcd import Display
from .models import AuthenticationResult, AttendanceEvent, EventType, TerminalState


class Authenticator(Protocol):
    def authenticate(self, timeout: float, cancel: threading.Event) -> AuthenticationResult | None: ...
    def close(self) -> None: ...


class Api(Protocol):
    def send(self, event: AttendanceEvent) -> dict[str, Any]: ...
    def close(self) -> None: ...


class SessionController:
    """Runs exactly one button-to-result authentication session at a time."""

    def __init__(
        self,
        *,
        authenticators: Sequence[Authenticator],
        api: Api,
        display: Display,
        buzzer: Buzzer,
        device_id: str,
        authentication_timeout: float = 8.0,
        result_display_seconds: float = 2.0,
    ):
        if not authenticators:
            raise ValueError("at least one authenticator is required")
        self.authenticators = list(authenticators)
        self.api = api
        self.display = display
        self.buzzer = buzzer
        self.device_id = device_id
        self.authentication_timeout = authentication_timeout
        self.result_display_seconds = result_display_seconds
        self.state = TerminalState.IDLE
        self._executor = ThreadPoolExecutor(
            max_workers=max(4, len(self.authenticators) + 2),
            thread_name_prefix="terminal-worker",
        )

    async def _run_sync(self, function: Any, *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, partial(function, *args)
        )

    @property
    def is_idle(self) -> bool:
        return self.state is TerminalState.IDLE

    async def _notify(
        self,
        state: TerminalState,
        line1: str,
        line2: str = "",
        line3: str = "",
        line4: str = "",
        sound: str | None = None,
    ) -> None:
        self.state = state
        await self._run_sync(self.display.show, line1, line2, line3, line4)
        if sound is not None:
            await self._run_sync(self.buzzer.play, sound)

    async def _first_valid_result(self) -> tuple[AuthenticationResult | None, int]:
        cancel = threading.Event()
        tasks = [
            asyncio.create_task(
                self._run_sync(authenticator.authenticate, self.authentication_timeout, cancel)
            )
            for authenticator in self.authenticators
        ]
        errors = 0
        result: AuthenticationResult | None = None
        try:
            for completed in asyncio.as_completed(tasks):
                try:
                    candidate = await completed
                except Exception:
                    errors += 1
                    logging.exception("authentication device failed")
                    continue
                if candidate is not None:
                    result = candidate
                    break
        finally:
            # Workers are cooperative: setting this prevents a late result from a
            # previous generation being accepted by a later session.
            cancel.set()
            await asyncio.gather(*tasks, return_exceptions=True)
        return result, errors

    async def run_session(self, event_type: EventType) -> None:
        if not self.is_idle:
            return
        action = "[ﾆｭｳｼﾂ]" if event_type == "check_in" else "[ﾀｲｼﾂ]"
        await self._notify(
            TerminalState.AUTHENTICATING,
            ":: AUTHENTICATING ::",
            "ﾆﾝｼｮｳ ｳｹﾂｹﾁｭｳ...",
            action,
            sound="accepted",
        )
        result, errors = await self._first_valid_result()
        if result is None:
            if errors == len(self.authenticators):
                await self._notify(
                    TerminalState.DEVICE_ERROR,
                    ":: DEVICE ERROR ::",
                    "ﾀﾝﾏﾂ ｴﾗｰ",
                    "ｶﾝﾘｼｬﾆ ﾚﾝﾗｸ",
                    sound="device_error",
                )
            else:
                await self._notify(
                    TerminalState.AUTH_FAILED,
                    ":: AUTH FAILED ::",
                    "ﾆﾝｼｮｳ ｼｯﾊﾟｲ",
                    "ﾓｳｲﾁﾄﾞ ｵﾀﾒｼｸﾀﾞｻｲ",
                    sound="auth_failed",
                )
            await asyncio.sleep(self.result_display_seconds)
            await self._idle()
            return

        await self._notify(
            TerminalState.PROCESSING,
            ":: PROCESSING ::",
            "ﾆﾝｼｮｳﾁｭｳ...",
            action,
        )
        event = AttendanceEvent.from_authentication(
            result, device_id=self.device_id, event_type=event_type
        )
        try:
            response = await self._run_sync(self.api.send, event)
        except AttendanceApiError:
            logging.exception("attendance event was not recorded")
            await self._notify(
                TerminalState.API_FAILED,
                ":: API ERROR ::",
                "ﾂｳｼﾝ ｴﾗｰ",
                "ﾓｳｲﾁﾄﾞ ｵﾀﾒｼｸﾀﾞｻｲ",
                sound="api_failed",
            )
        else:
            name = response.get("lcdDisplayName") or "ﾒｲｼｮｳ ﾐｾｯﾃｲ"
            await self._notify(
                TerminalState.SUCCESS,
                ":: AUTH SUCCESS ::",
                "ｷﾛｸ ｼﾏｼﾀ",
                action,
                str(name),
                sound="success",
            )
        await asyncio.sleep(self.result_display_seconds)

        card = next((item for item in self.authenticators if hasattr(item, "wait_for_removal")), None)
        if card is not None:
            await self._notify(
                TerminalState.COOLDOWN,
                ":: MESSAGE ::",
                "ｶｰﾄﾞｦ ﾊﾅｼﾃｸﾀﾞｻｲ",
            )
            await self._run_sync(card.wait_for_removal)
        await self._idle()

    async def _idle(self) -> None:
        await self._notify(
            TerminalState.IDLE,
            ":: smart gate ::",
            "ﾖｳｺｿ!",
            "[ﾆｭｳｼﾂ]  [ﾀｲｼﾂ]",
            "ﾎﾞﾀﾝｦ ｵｼﾃｸﾀﾞｻｲ",
        )

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)
