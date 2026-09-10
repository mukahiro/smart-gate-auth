from __future__ import annotations

import argparse
import time

from terminal.hardware.buzzer import GpioBuzzer, Sound


SOUNDS: tuple[Sound, ...] = (
    "accepted",
    "success",
    "auth_failed",
    "api_failed",
    "device_error",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Play Smart Gate buzzer melodies")
    parser.add_argument("sound", choices=(*SOUNDS, "all"), nargs="?", default="all")
    parser.add_argument("--pin", type=int, default=18)
    parser.add_argument("--frequency", type=int, default=4000)
    args = parser.parse_args()

    buzzer = GpioBuzzer(args.pin, args.frequency)
    try:
        selected = SOUNDS if args.sound == "all" else (args.sound,)
        for sound in selected:
            print(sound, flush=True)
            buzzer.play(sound)
            time.sleep(0.5)
    finally:
        buzzer.close()


if __name__ == "__main__":
    main()
