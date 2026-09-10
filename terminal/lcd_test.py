from __future__ import annotations

import argparse
import time

from terminal.hardware.lcd import I2cLcd


def main() -> None:
    parser = argparse.ArgumentParser(description="Display a test message on the LCD2004")
    parser.add_argument("--bus", type=int, default=1)
    parser.add_argument("--address", type=lambda value: int(value, 0), default=0x27)
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--line1", default="SMART GATE")
    parser.add_argument("--line2", default="LCD TEST OK")
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error("seconds must be positive")

    lcd = I2cLcd(bus_number=args.bus, address=args.address)
    try:
        lcd.show(args.line1, args.line2)
        time.sleep(args.seconds)
    finally:
        lcd.close()


if __name__ == "__main__":
    main()
