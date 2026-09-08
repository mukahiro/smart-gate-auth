import time

from smartcard.System import readers
from smartcard.scard import SCARD_SHARE_DIRECT
from smartcard.pcsc.PCSCPart10 import (
    getFeatureRequest,
    hasFeature,
    FEATURE_CCID_ESC_COMMAND,
)


SYSTEM_CODE = 0xFE00
SERVICE_CODE = 0x1A8B

POLL_INTERVAL = 0.2


START_SESSION = [
    0xFF, 0xC2, 0x00, 0x00,
    0x02,
    0x81, 0x00,
    0x00,
]

END_SESSION = [
    0xFF, 0xC2, 0x00, 0x00,
    0x02,
    0x82, 0x00,
    0x00,
]

SWITCH_TO_FELICA = [
    0xFF, 0xC2, 0x00, 0x02,
    0x04,
    0x8F, 0x02, 0x03, 0x00,
    0x00,
]


def find_tlv(data, target):
    if len(data) >= 2 and data[-2:] == [0x90, 0x00]:
        data = data[:-2]

    i = 0

    while i + 2 <= len(data):
        tag = data[i]
        length = data[i + 1]

        if i + 2 + length > len(data):
            return None

        value = data[i + 2:i + 2 + length]

        if tag == target:
            return value

        i += 2 + length

    return None


def get_reader():
    for reader in readers():
        if "RC-S300" in str(reader):
            return reader

    raise RuntimeError("RC-S300が見つかりません")


def polling(conn, escape_code):
    """
    FCF System Code FE00をPollingする。
    カードが存在すればIDmを返し、存在しなければNone。
    """

    command = [
        0xFF, 0xC2, 0x00, 0x01,
        0x08,

        0x95, 0x06,

        0x06,       # FeliCa packet length
        0x00,       # Polling command
        0xFE, 0x00, # System Code
        0x00,       # Request Code
        0x00,       # Time Slot
    ]

    try:
        response = conn.control(
            escape_code,
            command,
        )
    except Exception:
        return None

    felica_response = find_tlv(
        response,
        0x97,
    )

    if felica_response is None:
        return None

    # Polling Response
    if len(felica_response) < 10:
        return None

    if felica_response[1] != 0x01:
        return None

    return felica_response[2:10]


def read_student_id(conn, escape_code, idm):
    """
    FCF Service 1A8B / Block 0を読み、
    個人ID領域から学籍番号を取得する。
    """

    payload = [
        0x06,       # Read Without Encryption
        *idm,

        0x01,       # Number of Services
        0x8B, 0x1A, # Service Code 0x1A8B (Little Endian)

        0x01,       # Number of Blocks
        0x80, 0x00, # Block 0
    ]

    felica_packet = [
        len(payload) + 1,
        *payload,
    ]

    command = [
        0xFF, 0xC2, 0x00, 0x01,
        len(felica_packet) + 2,

        0x95,
        len(felica_packet),

        *felica_packet,
    ]

    response = conn.control(
        escape_code,
        command,
    )

    felica_response = find_tlv(
        response,
        0x97,
    )

    if felica_response is None:
        raise RuntimeError(
            "FCF基本情報の応答がありません"
        )

    if len(felica_response) < 29:
        raise RuntimeError(
            "FCF基本情報の応答が短すぎます"
        )

    # Read Without Encryption Response
    if felica_response[1] != 0x07:
        raise RuntimeError(
            "Read Without Encryptionの"
            "レスポンスではありません"
        )

    status1 = felica_response[10]
    status2 = felica_response[11]

    if status1 != 0x00 or status2 != 0x00:
        raise RuntimeError(
            f"FeliCa Read Error: "
            f"{status1:02X} {status2:02X}"
        )

    number_of_blocks = felica_response[12]

    if number_of_blocks < 1:
        raise RuntimeError(
            "Block 0が返されませんでした"
        )

    block0 = bytes(
        felica_response[13:29]
    )

    # FCF個人ID:
    # Block 0 の offset 2、12 bytes
    personal_id = (
        block0[2:14]
        .decode("ascii", errors="ignore")
        .rstrip("\x00 ")
    )

    if not personal_id:
        raise RuntimeError(
            "個人IDが空です"
        )

    return personal_id


def main():
    reader = get_reader()

    print(f"Reader: {reader}")

    conn = reader.createConnection()

    conn.connect(
        mode=SCARD_SHARE_DIRECT
    )

    features = getFeatureRequest(conn)

    escape_code = hasFeature(
        features,
        FEATURE_CCID_ESC_COMMAND,
    )

    if escape_code is None:
        raise RuntimeError(
            "FEATURE_CCID_ESC_COMMAND "
            "が利用できません"
        )

    print("カードをかざしてください")
    print("Ctrl+Cで終了します")
    print()

    session_started = False

    try:
        # Transparent Session開始
        conn.control(
            escape_code,
            START_SESSION,
        )

        session_started = True

        # NFC-F / FeliCaへ切り替え
        conn.control(
            escape_code,
            SWITCH_TO_FELICA,
        )

        card_present = False

        while True:
            idm = polling(
                conn,
                escape_code,
            )

            #
            # カードなし
            #
            if idm is None:
                if card_present:
                    print("カードが離れました")
                    print()

                card_present = False

                time.sleep(
                    POLL_INTERVAL
                )

                continue

            #
            # 同じカードを置きっぱなしの場合
            #
            if card_present:
                time.sleep(
                    POLL_INTERVAL
                )

                continue

            #
            # 新しくカードが置かれた
            #
            card_present = True

            try:
                student_id = read_student_id(
                    conn,
                    escape_code,
                    idm,
                )

                print(
                    f"学籍番号: {student_id}"
                )

            except Exception as e:
                print(
                    f"読み取りエラー: {e}"
                )

            time.sleep(
                POLL_INTERVAL
            )

    except KeyboardInterrupt:
        print()
        print("終了します")

    finally:
        if session_started:
            try:
                conn.control(
                    escape_code,
                    END_SESSION,
                )
            except Exception:
                pass


if __name__ == "__main__":
    main()