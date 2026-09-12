from __future__ import annotations

"""RC-S300を使い、FCF学生証から学籍番号を読み取る。"""

import re
import threading
import time
from typing import Any, Sequence

from terminal.models import AuthenticationResult

# 開発PCでも他のモジュールを読めるよう、pyscardの不足はクラス初期化時に通知する。
try:
    from smartcard.System import readers
    from smartcard.pcsc.PCSCPart10 import FEATURE_CCID_ESC_COMMAND, getFeatureRequest, hasFeature
    from smartcard.scard import SCARD_SHARE_DIRECT
except ImportError as exc:
    _IMPORT_ERROR: ImportError | None = exc
else:
    _IMPORT_ERROR = None

# FCF基本情報を選択するためのSystem CodeとService Code。
SYSTEM_CODE = 0xFE00
SERVICE_CODE = 0x1A8B

# RC-S300でFeliCaコマンドを送る前後に使う制御コマンド。
START_SESSION = [0xFF, 0xC2, 0x00, 0x00, 0x02, 0x81, 0x00, 0x00]
END_SESSION = [0xFF, 0xC2, 0x00, 0x00, 0x02, 0x82, 0x00, 0x00]
SWITCH_TO_FELICA = [0xFF, 0xC2, 0x00, 0x02, 0x04, 0x8F, 0x02, 0x03, 0x00, 0x00]


class CardReadError(RuntimeError):
    """カードの応答や学籍番号が想定する形式でない場合のエラー。"""
    pass


def find_tlv(data: Sequence[int], target: int) -> list[int] | None:
    """TLV形式の応答から指定タグの値を取り出す。"""
    values = list(data)

    # 末尾の90 00は「コマンド成功」を表すステータスで、TLV本体には含めない。
    if len(values) >= 2 and values[-2:] == [0x90, 0x00]:
        values = values[:-2]

    # Tagと1 byteのLengthを読み、Length分のValueを順番にたどる。
    offset = 0
    while offset + 2 <= len(values):
        tag, length = values[offset : offset + 2]
        end = offset + 2 + length
        # 宣言された長さが実データを超える応答は壊れている。
        if end > len(values):
            return None
        if tag == target:
            return values[offset + 2 : end]
        offset = end
    return None


def poll_fcf(connection: Any, escape_code: int) -> list[int] | None:
    """FCFカードをPollingし、検出時はIDmを返す。"""
    # FeliCa PollingコマンドでFCFのSystem Code FE00を指定する。
    packet = [0x06, 0x00, 0xFE, 0x00, 0x00, 0x00]
    # RC-S300のTransparent Exchange用コマンドでFeliCaパケットを包む。
    command = [0xFF, 0xC2, 0x00, 0x01, len(packet) + 2, 0x95, len(packet), *packet]
    try:
        response = connection.control(escape_code, command)
    except Exception:
        # カードがない場合もドライバが例外を返すことがあるため、未検出として扱う。
        return None
    value = find_tlv(response, 0x97)
    # 長さ、パケット内の長さフィールド、Polling応答コードを検査する。
    if value is None or len(value) < 18 or value[0] != len(value) or value[1] != 0x01:
        return None
    # Polling応答の3バイト目から8 bytesがカードのIDm。
    return value[2:10]


def read_student_number(connection: Any, escape_code: int, idm: Sequence[int]) -> str:
    """FCF基本情報を読み、10桁の学籍番号を返す。"""
    # IDmはFeliCa仕様で8 bytesと決められている。
    if len(idm) != 8:
        raise CardReadError("invalid IDm length")
    # Read Without EncryptionでService Code 1A8BのBlock 0を1ブロック読む。
    payload = [0x06, *idm, 0x01, SERVICE_CODE & 0xFF, SERVICE_CODE >> 8, 0x01, 0x80, 0x00]
    packet = [len(payload) + 1, *payload]
    command = [0xFF, 0xC2, 0x00, 0x01, len(packet) + 2, 0x95, len(packet), *packet]
    value = find_tlv(connection.control(escape_code, command), 0x97)
    # Read Without Encryption応答のコードは07。必要フィールドが揃う最低長も確認する。
    if value is None or len(value) < 29 or value[0] != len(value) or value[1] != 0x07:
        raise CardReadError("invalid FCF response")
    # 応答元IDm、2つのステータス、ブロック数を確認し、別カードや読み取り失敗を除外する。
    if value[2:10] != list(idm) or value[10:12] != [0, 0] or value[12] != 1:
        raise CardReadError("FCF read failed")
    try:
        # 個人ID領域の12 bytesをASCIIとして読み、末尾の空白とNUL文字を除く。
        student_number = bytes(value[15:27]).decode("ascii").rstrip("\x00 ")
    except UnicodeDecodeError as exc:
        raise CardReadError("student number is not ASCII") from exc
    if not re.fullmatch(r"[0-9]{10}", student_number):
        raise CardReadError("student number is not 10 digits")
    return student_number


class CardAuthenticator:
    """RC-S300の接続を維持し、カード認証結果を統合端末へ返す。"""

    def __init__(self, poll_interval: float = 0.2):
        """RC-S300とTransparent Sessionを開始し、読み取りを準備する。"""
        # pyscardなしではハードウェアを扱えないため、理由を保持したまま起動を中止する。
        if _IMPORT_ERROR is not None:
            raise RuntimeError("pyscard is not installed") from _IMPORT_ERROR
        # PC/SCが認識した機器のうち、RC-S300と判別できる最初の1台を使う。
        available = list(readers())
        reader = next((item for item in available if "RC-S300" in str(item)), None)
        if reader is None:
            raise RuntimeError("RC-S300 was not found")
        # Direct Modeはカードではなく、リーダー本体へ制御コマンドを送るために使う。
        self._connection = reader.createConnection()
        self._connection.connect(mode=SCARD_SHARE_DIRECT)
        feature = hasFeature(getFeatureRequest(self._connection), FEATURE_CCID_ESC_COMMAND)
        # Escape Commandが無効な環境ではFeliCaのTransparent Exchangeを実行できない。
        if feature is None:
            self._connection.disconnect()
            raise RuntimeError("FEATURE_CCID_ESC_COMMAND is unavailable")
        self._escape_code = feature
        # Transparent Sessionを開始し、通信対象をFeliCa（NFC-F）へ切り替える。
        self._connection.control(feature, START_SESSION)
        self._connection.control(feature, SWITCH_TO_FELICA)
        self._poll_interval = poll_interval
        self._closed = False

    def authenticate(self, timeout: float, cancel: threading.Event) -> AuthenticationResult | None:
        """制限時間内にカードを読み、認証結果を返す。"""
        # monotonic時計はOS時刻の変更の影響を受けないため、制限時間の計測に適している。
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not cancel.is_set():
            idm = poll_fcf(self._connection, self._escape_code)
            if idm is not None:
                # 統合端末が共通処理できるよう、学籍番号と認証方式を共通型にする。
                return AuthenticationResult(read_student_number(self._connection, self._escape_code, idm), "card")
            # waitを使うと、待機中でも統合端末からのキャンセルをすぐ受け取れる。
            cancel.wait(self._poll_interval)
        return None

    def wait_for_removal(self, timeout: float = 10.0) -> None:
        """カードが取り外されるか、制限時間に達するまで待つ。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if poll_fcf(self._connection, self._escape_code) is None:
                return
            time.sleep(self._poll_interval)

    def close(self) -> None:
        """Transparent Sessionを終了し、RC-S300との接続を閉じる。"""
        # 終了処理が複数回呼ばれても、機器への終了コマンドは1回だけ送る。
        if self._closed:
            return
        self._closed = True
        try:
            self._connection.control(self._escape_code, END_SESSION)
        finally:
            self._connection.disconnect()
