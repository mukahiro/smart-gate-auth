from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from terminal.hardware.buzzer import GpioBuzzer


class BuzzerTests(unittest.TestCase):
    def test_passive_buzzer_uses_four_kilohertz_pwm(self) -> None:
        gpio = types.ModuleType("RPi.GPIO")
        gpio.BCM = 11
        gpio.OUT = 1
        gpio.LOW = 0
        gpio.setmode = MagicMock()
        gpio.setup = MagicMock()
        gpio.output = MagicMock()
        gpio.cleanup = MagicMock()
        pwm = MagicMock()
        gpio.PWM = MagicMock(return_value=pwm)
        rpi = types.ModuleType("RPi")
        rpi.GPIO = gpio

        with patch.dict(sys.modules, {"RPi": rpi, "RPi.GPIO": gpio}):
            buzzer = GpioBuzzer(18)
            with patch("terminal.hardware.buzzer.time.sleep"):
                buzzer.play("success")
            buzzer.close()

        gpio.PWM.assert_called_once_with(18, 4000)
        pwm.start.assert_called_once_with(0.0)
        self.assertEqual(pwm.ChangeDutyCycle.call_args_list[0].args, (50.0,))
        self.assertIn((0.0,), [call.args for call in pwm.ChangeDutyCycle.call_args_list])
        self.assertGreaterEqual(pwm.stop.call_count, 2)


if __name__ == "__main__":
    unittest.main()

