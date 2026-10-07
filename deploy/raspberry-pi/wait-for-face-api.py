"""Wait for the local face API to initialize its database and model."""

import argparse
import json
import time
from urllib.request import ProxyHandler, build_opener


def wait_for_face_api(url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    # ローカルAPIの確認を、運用環境のHTTPプロキシへ送らない。
    opener = build_opener(ProxyHandler({}))
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            with opener.open(url, timeout=min(2.0, remaining)) as response:
                if response.status == 200 and json.load(response) == {"status": "ok"}:
                    return True
        except (OSError, ValueError):
            pass
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8001/health")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    if not 0 < args.timeout < float("inf"):
        parser.error("timeout must be a finite positive number")
    print("Waiting for face registration API", flush=True)
    if not wait_for_face_api(args.url, args.timeout):
        print("Face registration API was not ready before timeout", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
