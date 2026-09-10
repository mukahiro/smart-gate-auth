from __future__ import annotations

import asyncio
import threading
import unittest

from terminal.attendance_api import AttendanceApiError
from terminal.models import AuthenticationResult, TerminalState
from terminal.state_machine import SessionController


class FakeAuthenticator:
    def __init__(self, result: AuthenticationResult | None, delay: float = 0):
        self.result = result
        self.delay = delay

    def authenticate(self, timeout: float, cancel: threading.Event):
        cancel.wait(self.delay)
        return None if cancel.is_set() else self.result

    def close(self) -> None:
        pass


class FakeApi:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.events = []

    def send(self, event):
        self.events.append(event)
        if self.fail:
            raise AttendanceApiError("offline")
        return {"eventId": event.event_id, "status": "recorded", "lcdDisplayName": "ﾃｽﾄ"}

    def close(self) -> None:
        pass


class FakeDisplay:
    def __init__(self):
        self.messages = []

    def show(
        self,
        line1: str,
        line2: str = "",
        line3: str = "",
        line4: str = "",
    ) -> None:
        self.messages.append((line1, line2, line3, line4))

    def close(self) -> None:
        pass


class FakeBuzzer:
    def __init__(self):
        self.sounds = []

    def play(self, sound) -> None:
        self.sounds.append(sound)

    def close(self) -> None:
        pass


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def make_controller(self, authenticators, api):
        self.display = FakeDisplay()
        self.buzzer = FakeBuzzer()
        controller = SessionController(
            authenticators=authenticators,
            api=api,
            display=self.display,
            buzzer=self.buzzer,
            device_id="gate-1",
            authentication_timeout=0.2,
            result_display_seconds=0,
        )
        # The fakes are non-blocking; inline execution keeps transition tests
        # deterministic without involving OS worker scheduling.
        async def run_inline(function, *args):
            return function(*args)

        controller._run_sync = run_inline
        return controller

    async def test_first_result_wins_and_button_selects_event_type(self) -> None:
        api = FakeApi()
        controller = self.make_controller(
            [
                FakeAuthenticator(AuthenticationResult("2222222222", "card")),
                FakeAuthenticator(AuthenticationResult("1111111111", "face", 0.9)),
            ],
            api,
        )
        await controller.run_session("check_out")
        self.assertEqual(len(api.events), 1)
        self.assertEqual(api.events[0].student_number, "2222222222")
        self.assertEqual(api.events[0].event_type, "check_out")
        self.assertEqual(controller.state, TerminalState.IDLE)
        controller.close()

    async def test_api_failure_is_not_retried_and_reports_unrecorded(self) -> None:
        api = FakeApi(fail=True)
        controller = self.make_controller(
            [FakeAuthenticator(AuthenticationResult("1234567890", "card"))], api
        )
        with self.assertLogs(level="ERROR"):
            await controller.run_session("check_in")
        self.assertEqual(len(api.events), 1)
        self.assertIn(
            ("::ERROR::", "ﾂｳｼﾝ ｴﾗｰ", "ﾓｳｲﾁﾄﾞ ｵﾀﾒｼｸﾀﾞｻｲ", ""),
            self.display.messages,
        )
        self.assertIn("api_failed", self.buzzer.sounds)
        controller.close()

    async def test_one_failed_device_allows_other_device(self) -> None:
        class Broken(FakeAuthenticator):
            def authenticate(self, timeout, cancel):
                raise RuntimeError("camera disconnected")

        api = FakeApi()
        controller = self.make_controller(
            [Broken(None), FakeAuthenticator(AuthenticationResult("1234567890", "card"))], api
        )
        with self.assertLogs(level="ERROR"):
            await controller.run_session("check_in")
        self.assertEqual(len(api.events), 1)
        controller.close()


if __name__ == "__main__":
    unittest.main()
