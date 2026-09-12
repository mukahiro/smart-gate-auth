#!/usr/bin/env python3
"""RC-S300でFCF学生証を読み取り、入退室イベントをAPIへ送信する。"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import requests

try:
    from smartcard.System import readers
    from smartcard.pcsc.PCSCPart10 import (
        FEATURE_CCID_ESC_COMMAND,
        getFeatureRequest,
        hasFeature,
    )
    from smartcard.scard import SCARD_SHARE_DIRECT
except ImportError as exc:  # --helpは依存パッケージの導入前でも表示可能にする。
    PYSCARD_IMPORT_ERROR: ImportError | None = exc
else:
    PYSCARD_IMPORT_ERROR = None

SYSTEM_CODE = 0xFE00
SERVICE_CODE = 0x1A8B
POLL_INTERVAL_SEC = 0.2

START_SESSION = [0xFF, 0xC2, 0x00, 0x00, 0x02, 0x81, 0x00, 0x00]
END_SESSION = [0xFF, 0xC2, 0x00, 0x00, 0x02, 0x82, 0x00, 0x00]
SWITCH_TO_FELICA = [
    0xFF, 0xC2, 0x00, 0x02, 0x04, 0x8F, 0x02, 0x03, 0x00, 0x00
]


class CardReadError(RuntimeError):
    """カード応答が不正、またはFCF個人IDを取得できない。"""


class ApiError(RuntimeError):
    """入退室APIへの送信に失敗した。"""


@dataclass(frozen=True)
class Config:
    api_url: str
    bearer_token: str
    device_id: str
    db_path: Path
    timeout_sec: float
    max_retries: int


@dataclass(frozen=True)
class AttendanceEvent:
    event_id: str
    student_number: str
    device_id: str
    event_type: str
    authenticated_at: str
    idm: str

    def payload(self) -> dict[str, str]:
        return {
            "eventId": self.event_id,
            "studentNumber": self.student_number,
            "deviceId": self.device_id,
            "method": "card",
            "eventType": self.event_type,
            "authenticatedAt": self.authenticated_at,
        }


def load_config(args: argparse.Namespace) -> Config:
    base_url = os.environ.get("AUTH_API_BASE_URL", "http://localhost:3000")
    endpoint = os.environ.get(
        "AUTH_API_ATTENDANCE_ENDPOINT", "/api/v1/attendance-events"
    )
    api_url = args.api_url or f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    bearer_token = args.token or os.environ.get("AUTH_APP_BEARER_TOKEN", "")
    device_id = args.device_id or os.environ.get(
        "AUTH_DEVICE_ID", "smart-gate-card-01"
    )
    db_path = Path(
        args.db_path
        or os.environ.get(
            "AUTH_CARD_DB_PATH", str(Path(__file__).with_name("card_auth.db"))
        )
    )
    if not bearer_token:
        raise ValueError(
            "AUTH_APP_BEARER_TOKENが未設定です。環境変数または--tokenで指定してください"
        )
    if not device_id.strip():
        raise ValueError("deviceIdは空にできません")
    return Config(
        api_url=api_url,
        bearer_token=bearer_token,
        device_id=device_id,
        db_path=db_path,
        timeout_sec=args.timeout,
        max_retries=args.retries,
    )


def find_tlv(data: Sequence[int], target: int) -> list[int] | None:
    """PC/SC応答から指定タグの単純TLVを取り出す。"""
    values = list(data)
    if len(values) >= 2 and values[-2:] == [0x90, 0x00]:
        values = values[:-2]
    offset = 0
    while offset + 2 <= len(values):
        tag = values[offset]
        length = values[offset + 1]
        end = offset + 2 + length
        if end > len(values):
            return None
        if tag == target:
            return values[offset + 2 : end]
        offset = end
    return None


def get_reader():
    if PYSCARD_IMPORT_ERROR is not None:
        raise RuntimeError(
            "pyscardがインストールされていません（pip install pyscard）"
        ) from PYSCARD_IMPORT_ERROR
    available = list(readers())
    for reader in available:
        if "RC-S300" in str(reader):
            return reader
    names = ", ".join(str(reader) for reader in available) or "なし"
    raise RuntimeError(f"RC-S300が見つかりません（検出済み: {names}）")


def poll_fcf(conn: Any, escape_code: int) -> list[int] | None:
    """System Code FE00をPollingし、FCF SystemのIDmを返す。"""
    packet = [
        0x06, 0x00, (SYSTEM_CODE >> 8) & 0xFF, SYSTEM_CODE & 0xFF, 0x00, 0x00
    ]
    command = [
        0xFF, 0xC2, 0x00, 0x01, len(packet) + 2, 0x95, len(packet), *packet
    ]
    try:
        response = conn.control(escape_code, command)
    except Exception:
        # カードがない間、ドライバによっては例外で通知される。
        return None
    felica_response = find_tlv(response, 0x97)
    if (
        felica_response is None
        or len(felica_response) < 18
        or felica_response[0] != len(felica_response)
        or felica_response[1] != 0x01
    ):
        return None
    return felica_response[2:10]


def read_student_number(conn: Any, escape_code: int, idm: Sequence[int]) -> str:
    """FCF基本情報 Service 1A8B / Block 0から学籍番号を読む。"""
    if len(idm) != 8:
        raise CardReadError("IDmの長さが不正です")
    payload = [
        0x06,
        *idm,
        0x01,
        SERVICE_CODE & 0xFF,
        (SERVICE_CODE >> 8) & 0xFF,
        0x01,
        0x80,
        0x00,
    ]
    felica_packet = [len(payload) + 1, *payload]
    command = [
        0xFF, 0xC2, 0x00, 0x01, len(felica_packet) + 2,
        0x95, len(felica_packet), *felica_packet,
    ]
    response = conn.control(escape_code, command)
    felica_response = find_tlv(response, 0x97)
    if felica_response is None:
        raise CardReadError("FCF基本情報の応答がありません")
    if len(felica_response) < 29:
        raise CardReadError("FCF基本情報の応答が短すぎます")
    if felica_response[0] != len(felica_response):
        raise CardReadError("FeliCa応答のLengthが不正です")
    if felica_response[1] != 0x07:
        raise CardReadError("Read Without Encryptionの応答ではありません")
    if felica_response[2:10] != list(idm):
        raise CardReadError("応答のIDmがPolling結果と一致しません")
    status1, status2 = felica_response[10:12]
    if (status1, status2) != (0x00, 0x00):
        raise CardReadError(f"FeliCa Read Error: {status1:02X} {status2:02X}")
    if felica_response[12] != 1:
        raise CardReadError("Block 0が1ブロック返されませんでした")

    # Block 0 offset 2から12 bytesがFCF個人ID。
    try:
        personal_id = bytes(felica_response[15:27]).decode("ascii").rstrip("\x00 ")
    except UnicodeDecodeError as exc:
        raise CardReadError("FCF個人IDがASCIIではありません") from exc
    if not re.fullmatch(r"[0-9]{10}", personal_id):
        raise CardReadError(
            f"FCF個人IDが10桁の学籍番号ではありません: {personal_id!r}"
        )
    return personal_id


class EventStore:
    """入退室状態と、API応答待ちのイベントをSQLiteで保持する。"""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS card_status (
                student_number TEXT PRIMARY KEY,
                current_status TEXT NOT NULL CHECK(current_status IN ('in', 'out'))
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_attendance_events (
                event_id TEXT PRIMARY KEY,
                student_number TEXT NOT NULL UNIQUE,
                device_id TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN ('check_in', 'check_out')),
                authenticated_at TEXT NOT NULL,
                idm TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def prepare_event(
        self, student_number: str, device_id: str, idm: str
    ) -> tuple[AttendanceEvent, bool]:
        row = self.conn.execute(
            "SELECT * FROM pending_attendance_events WHERE student_number = ?",
            (student_number,),
        ).fetchone()
        if row:
            return self._to_event(row), False
        status_row = self.conn.execute(
            "SELECT current_status FROM card_status WHERE student_number = ?",
            (student_number,),
        ).fetchone()
        current_status = status_row[0] if status_row else "out"
        event = AttendanceEvent(
            event_id=str(uuid.uuid4()),
            student_number=student_number,
            device_id=device_id,
            event_type="check_in" if current_status == "out" else "check_out",
            authenticated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            idm=idm,
        )
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO pending_attendance_events
                    (event_id, student_number, device_id, event_type, authenticated_at, idm)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id, event.student_number, event.device_id,
                    event.event_type, event.authenticated_at, event.idm,
                ),
            )
        return event, True

    def pending_events(self) -> list[AttendanceEvent]:
        rows = self.conn.execute(
            "SELECT * FROM pending_attendance_events ORDER BY authenticated_at"
        ).fetchall()
        return [self._to_event(row) for row in rows]

    def complete(self, event: AttendanceEvent) -> None:
        new_status = "in" if event.event_type == "check_in" else "out"
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO card_status (student_number, current_status) VALUES (?, ?)
                ON CONFLICT(student_number) DO UPDATE SET current_status = excluded.current_status
                """,
                (event.student_number, new_status),
            )
            self.conn.execute(
                "DELETE FROM pending_attendance_events WHERE event_id = ?",
                (event.event_id,),
            )

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _to_event(row: sqlite3.Row) -> AttendanceEvent:
        return AttendanceEvent(
            event_id=row["event_id"],
            student_number=row["student_number"],
            device_id=row["device_id"],
            event_type=row["event_type"],
            authenticated_at=row["authenticated_at"],
            idm=row["idm"],
        )


class AttendanceApi:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {config.bearer_token}",
                "Accept": "application/json",
            }
        )

    def send(self, event: AttendanceEvent) -> dict[str, Any]:
        last_error = "不明なエラー"
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self.session.post(
                    self.config.api_url,
                    json=event.payload(),
                    timeout=self.config.timeout_sec,
                )
                if response.status_code in (200, 201):
                    body = response.json()
                    if not isinstance(body, dict):
                        raise ApiError("APIの成功応答がJSON objectではありません")
                    if body.get("eventId") != event.event_id:
                        raise ApiError("API応答のeventIdが送信値と一致しません")
                    if body.get("status") not in ("recorded", "duplicate"):
                        raise ApiError("API応答のstatusが不正です")
                    return body
                try:
                    error_body = response.json()
                    last_error = error_body.get("error", {}).get(
                        "message", response.text[:200]
                    )
                except (ValueError, AttributeError):
                    last_error = response.text[:200]
                last_error = f"HTTP {response.status_code}: {last_error}"
                if response.status_code < 500 and response.status_code not in (408, 429):
                    break
            except ApiError:
                raise
            except requests.RequestException as exc:
                last_error = str(exc)
            except ValueError:
                last_error = "APIからJSONではない成功応答が返されました"
            if attempt < self.config.max_retries:
                time.sleep(min(2**attempt, 4))
        raise ApiError(last_error)

    def close(self) -> None:
        self.session.close()


def notify(message: str, *, is_error: bool = False) -> None:
    prefix = "[ERROR]" if is_error else "[OK]"
    print(f"{prefix} {message}")


def send_and_complete(
    event: AttendanceEvent, store: EventStore, api: AttendanceApi
) -> bool:
    try:
        result = api.send(event)
    except ApiError as exc:
        notify(
            f"API送信失敗（eventId={event.event_id}、後で再送します）: {exc}",
            is_error=True,
        )
        return False
    store.complete(event)
    action = "入室" if event.event_type == "check_in" else "退出"
    display_name = result.get("lcdDisplayName")
    result_code = result.get("resultCode", "")
    if display_name:
        notify(f"{display_name} さん、{action}しました（{result_code}）")
    else:
        notify(f"学籍番号 {event.student_number}: {action}しました（{result_code}）")
    return True


def retry_pending(store: EventStore, api: AttendanceApi) -> None:
    pending = store.pending_events()
    if not pending:
        return
    print(f"未送信イベント {len(pending)} 件を再送します")
    for event in pending:
        send_and_complete(event, store, api)


def run_reader(config: Config) -> None:
    if PYSCARD_IMPORT_ERROR is not None:
        raise RuntimeError(
            "pyscardがインストールされていません（pip install pyscard）"
        ) from PYSCARD_IMPORT_ERROR
    store = EventStore(config.db_path)
    api = AttendanceApi(config)
    connection = None
    session_started = False
    escape_code = None
    try:
        retry_pending(store, api)
        reader = get_reader()
        print(f"Reader: {reader}")
        connection = reader.createConnection()
        connection.connect(mode=SCARD_SHARE_DIRECT)
        features = getFeatureRequest(connection)
        escape_code = hasFeature(features, FEATURE_CCID_ESC_COMMAND)
        if escape_code is None:
            raise RuntimeError(
                "FEATURE_CCID_ESC_COMMANDが利用できません。"
                "libccidのifdDriverOptionsを確認してください"
            )
        connection.control(escape_code, START_SESSION)
        session_started = True
        connection.control(escape_code, SWITCH_TO_FELICA)
        print("学生証をかざしてください（Ctrl+Cで終了）")
        card_present = False
        while True:
            idm = poll_fcf(connection, escape_code)
            if idm is None:
                if card_present:
                    print("カードが離れました")
                card_present = False
                time.sleep(POLL_INTERVAL_SEC)
                continue
            if card_present:
                time.sleep(POLL_INTERVAL_SEC)
                continue
            card_present = True
            try:
                student_number = read_student_number(connection, escape_code, idm)
                idm_hex = "".join(f"{value:02X}" for value in idm)
                print(f"学籍番号: {student_number}")
                event, created = store.prepare_event(
                    student_number, config.device_id, idm_hex
                )
                if not created:
                    print(f"未送信イベントを再送します: {event.event_id}")
                send_and_complete(event, store, api)
            except CardReadError as exc:
                notify(f"カード読み取りエラー: {exc}", is_error=True)
            except Exception as exc:
                # 1枚の処理失敗で常駐プロセスを終了しない。
                notify(f"カード処理エラー: {exc}", is_error=True)
            time.sleep(POLL_INTERVAL_SEC)
    finally:
        if session_started and connection is not None and escape_code is not None:
            try:
                connection.control(escape_code, END_SESSION)
            except Exception:
                pass
        if connection is not None:
            try:
                connection.disconnect()
            except Exception:
                pass
        api.close()
        store.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="RC-S300でFCF学生証を読み取り、入退室APIへ送信します"
    )
    parser.add_argument("--api-url", help="attendance-eventsの完全なURL")
    parser.add_argument("--token", help="Bearer token（通常は環境変数を推奨）")
    parser.add_argument("--device-id", help="認証端末ID")
    parser.add_argument("--db-path", help="状態・未送信イベント用SQLite DB")
    parser.add_argument("--timeout", type=float, default=3.0, help="API timeout秒")
    parser.add_argument("--retries", type=int, default=2, help="API再試行回数")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeoutは0より大きい値にしてください")
    if args.retries < 0:
        parser.error("--retriesは0以上にしてください")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = load_config(parse_args(argv))
        run_reader(config)
    except KeyboardInterrupt:
        print("\n終了します")
        return 0
    except (RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
