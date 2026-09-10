from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from terminal.hardware.lcd import ConsoleLcd, I2cLcd, ResilientDisplay


class LcdTests(unittest.TestCase):
    def make_lcd(self) -> tuple[I2cLcd, MagicMock]:
        bus = MagicMock()
        with patch("terminal.hardware.lcd.time.sleep"):
            lcd = I2cLcd(bus=bus, address=0x27, columns=20, rows=4)
        return lcd, bus

    def test_ascii_and_half_width_katakana_encoding(self) -> None:
        self.assertEqual(I2cLcd._encode_text("ABC", 5), b"ABC  ")
        self.assertEqual(I2cLcd._encode_text("ﾃｽﾄ", 3), bytes((0xC3, 0xBD, 0xC4)))

    def test_known_japanese_prompts_are_rendered_as_ascii(self) -> None:
        self.assertEqual(
            I2cLcd._encode_text("入室/退出を選択", 20),
            b"IN / OUT WO SENTAKU ",
        )

    def test_show_writes_all_rows_to_configured_address(self) -> None:
        lcd, bus = self.make_lcd()
        bus.reset_mock()
        with patch("terminal.hardware.lcd.time.sleep"):
            lcd.show("HELLO", "ﾃｽﾄ")
        self.assertGreater(bus.write_byte.call_count, 4 * 20)
        self.assertTrue(
            all(call.args[0] == 0x27 for call in bus.write_byte.call_args_list)
        )

    def test_runtime_i2c_failure_uses_fallback(self) -> None:
        primary = MagicMock()
        primary.show.side_effect = OSError("I2C disconnected")
        fallback = MagicMock(spec=ConsoleLcd)
        display = ResilientDisplay(primary, fallback)
        with self.assertLogs(level="ERROR"):
            display.show("MESSAGE")
        fallback.show.assert_called_once_with("MESSAGE", "")


if __name__ == "__main__":
    unittest.main()

