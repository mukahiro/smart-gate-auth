# RC-S300を用いたFCF準拠学生証の学籍番号読み取り

## 1. 概要

SONY RC-S300を用いて、FCF（FeliCa Common-use Format）に準拠した学生証から学籍番号を取得する。

RC-S300はLinux環境ではPC/SCデバイスとして扱えるため、Pythonからは `pyscard` を利用する。

FCF領域を読み取る際には、通常のPC/SCによるカード接続だけでは目的のFeliCa Systemが選択されない場合がある。そのため、CCID Transparent Exchangeを使用してFCFのSystem Codeを明示的にPollingし、その後 `Read Without Encryption` コマンドによってFCF基本情報を取得する。

---

## 2. 使用環境

* カードリーダー: SONY RC-S300
* OS: Ubuntu / Linux
* Python: Python 3
* Pythonライブラリ: `pyscard`
* PC/SC: `pcscd`
* CCIDドライバ: `libccid`

---

## 3. 必要パッケージ

Ubuntuでは以下をインストールする。

```bash
sudo apt update
sudo apt install python3-pyscard pcscd libccid
```

仮想環境を使用する場合は、必要に応じて `pyscard` を仮想環境へインストールする。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pyscard
```

---

## 4. RC-S300の確認

Pythonからカードリーダーを列挙する。

```python
from smartcard.System import readers

for reader in readers():
    print(reader)
```

RC-S300が正常に認識されていれば、以下のような名称が表示される。

```text
SONY FeliCa RC-S300/P (...) 00 00
```

---

## 5. CCID Escape Commandの有効化

FCF領域を明示的にPollingするには、PC/SCの通常のAPDUだけではなく、RC-S300のTransparent Exchangeを利用する。

Linuxの `libccid` では、CCID Escape Commandがデフォルトで無効になっている場合がある。

その場合、

```text
FEATURE_CCID_ESC_COMMAND
```

が取得できない。

### 5.1 設定確認

例えば以下を確認する。

```bash
grep -A1 ifdDriverOptions /etc/libccid_Info.plist
```

設定が

```xml
<key>ifdDriverOptions</key>
<string>0x0000</string>
```

となっている場合、CCID Escape Commandを利用できるようにする。

```xml
<key>ifdDriverOptions</key>
<string>0x0001</string>
```

既に別のフラグが設定されている場合は、既存値に `0x0001` をORする。

設定後、

```bash
sudo systemctl restart pcscd
```

を実行し、必要に応じてRC-S300をUSBから抜き差しする。

---

## 6. FCF領域について

FCF準拠カードでは、基本情報を以下の領域から取得できる。

| 項目                   | 値                       |
| -------------------- | ----------------------- |
| FeliCa System Code   | `0xFE00`                |
| FCF基本情報 Service Code | `0x1A8B`                |
| 読み取り方式               | Read Without Encryption |
| 1ブロック                | 16 bytes                |

Service CodeはFeliCaコマンド内ではLittle Endianで指定するため、

```text
0x1A8B
```

は

```text
8B 1A
```

として送信する。

---

## 7. 通常のPC/SCアクセスだけでは読めない場合

RC-S300では通常、

```text
FF CA 00 00 00
```

によってIDmを取得できる。

しかし、複数のFeliCa Systemを持つ学生証では、ここで自動的に選択されるSystemがFCFの `FE00` とは限らない。

その状態で、

```text
Service Code 1A8B
```

を指定すると、カードからサービスコード不正を示すエラーが返る場合がある。

この場合、FCF領域が存在しないのではなく、**異なるFeliCa Systemを選択している可能性がある**。

そのため、Transparent Exchangeを利用して

```text
System Code FE00
```

を明示的にPollingする。

---

## 8. Transparent Session

RC-S300にDirect Modeで接続する。

```python
from smartcard.scard import SCARD_SHARE_DIRECT

conn = reader.createConnection()
conn.connect(mode=SCARD_SHARE_DIRECT)
```

CCID Escape Command用のControl Codeを取得する。

```python
from smartcard.pcsc.PCSCPart10 import (
    getFeatureRequest,
    hasFeature,
    FEATURE_CCID_ESC_COMMAND,
)

features = getFeatureRequest(conn)

escape_code = hasFeature(
    features,
    FEATURE_CCID_ESC_COMMAND
)

if escape_code is None:
    raise RuntimeError(
        "FEATURE_CCID_ESC_COMMAND が利用できません"
    )
```

---

## 9. Transparent Sessionの開始

Transparent Sessionを開始する。

```python
START_SESSION = [
    0xFF, 0xC2, 0x00, 0x00,
    0x02,
    0x81, 0x00,
    0x00
]

conn.control(
    escape_code,
    START_SESSION
)
```

続いて、通信方式をFeliCa（NFC-F）へ切り替える。

```python
SWITCH_TO_FELICA = [
    0xFF, 0xC2, 0x00, 0x02,
    0x04,
    0x8F, 0x02, 0x03, 0x00,
    0x00
]

conn.control(
    escape_code,
    SWITCH_TO_FELICA
)
```

---

## 10. FCF System Code `FE00` のPolling

FCF Systemを明示的にPollingする。

```python
polling = [
    0xFF, 0xC2, 0x00, 0x01,
    0x08,

    0x95, 0x06,

    0x06,       # FeliCa packet length
    0x00,       # Polling command
    0xFE, 0x00, # System Code
    0x00,       # Request Code
    0x00,       # Time Slot
]

resp = conn.control(
    escape_code,
    polling
)
```

正常に応答した場合、Transparent Exchangeのレスポンス中に `0x97` TLVとしてFeliCa Polling Responseが含まれる。

Polling Responseの構造は概ね以下となる。

```text
Length
01
IDm (8 bytes)
PMm (8 bytes)
```

ここで取得したIDmを、以降の `Read Without Encryption` に使用する。

重要なのは、通常の `FF CA` で取得したIDmではなく、**`FE00` をPollingして取得したIDmを使用すること**である。

---

## 11. TLVレスポンスの取得

Transparent Exchangeでは、レスポンス中からFeliCa Responseを取り出す必要がある。

例：

```python
def find_tlv(data, target):
    if len(data) >= 2 and data[-2:] == [0x90, 0x00]:
        data = data[:-2]

    i = 0

    while i + 2 <= len(data):
        tag = data[i]
        length = data[i + 1]
        value = data[i + 2:i + 2 + length]

        if tag == target:
            return value

        i += 2 + length

    return None
```

FeliCaからのレスポンスは、

```python
felica_response = find_tlv(resp, 0x97)
```

で取得する。

---

## 12. FCF基本情報の読み取り

FCF基本情報はService Code `0x1A8B` から取得する。

学籍番号のみ必要な場合、Block 0だけを読めばよい。

### 12.1 Read Without Encryption

FeliCaコマンドを生成する。

```python
payload = [
    0x06,       # Read Without Encryption
    *idm,

    0x01,       # Number of Services
    0x8B, 0x1A, # Service Code 0x1A8B

    0x01,       # Number of Blocks
    0x80, 0x00, # Block 0
]
```

FeliCaパケットには先頭にPacket Lengthを付ける。

```python
felica_packet = [
    len(payload) + 1,
    *payload
]
```

Transparent Exchange用のコマンドを作成する。

```python
apdu = [
    0xFF, 0xC2, 0x00, 0x01,
    len(felica_packet) + 2,

    0x95,
    len(felica_packet),

    *felica_packet
]
```

送信する。

```python
resp = conn.control(
    escape_code,
    apdu
)
```

---

## 13. Read Without Encryption Response

FeliCa Responseを取得する。

```python
felica_response = find_tlv(
    resp,
    0x97
)

if felica_response is None:
    raise RuntimeError(
        "FCF基本情報の読み取りに失敗しました"
    )
```

Read Without Encryption Responseの主な構造は以下。

```text
Length
07
IDm               8 bytes
Status Flag 1      1 byte
Status Flag 2      1 byte
Number of Blocks   1 byte
Block Data         16 bytes × N
```

正常終了の場合、

```text
Status Flag 1 = 00
Status Flag 2 = 00
```

となる。

```python
status1 = felica_response[10]
status2 = felica_response[11]

if status1 != 0x00 or status2 != 0x00:
    raise RuntimeError(
        f"FeliCa Read Error: "
        f"{status1:02X} {status2:02X}"
    )
```

Block 0を取り出す。

```python
block0 = bytes(
    felica_response[13:29]
)
```

---

## 14. Block 0から個人IDを取得

FCF基本情報のBlock 0は以下のような構造を持つ。

| Offset |   Length | 内容      |
| -----: | -------: | ------- |
|      0 |  2 bytes | 利用者区分   |
|      2 | 12 bytes | 個人ID    |
|     14 |   1 byte | 再発行関連情報 |
|     15 |   1 byte | 属性情報    |

学籍番号がFCFの個人IDとして登録されているカードでは、

```python
personal_id = (
    block0[2:14]
    .decode("ascii", errors="ignore")
    .rstrip("\x00 ")
)
```

によって取得できる。

大学側の運用によって個人IDの内容は異なる可能性があるため、システムとしては「FCF個人ID」として扱い、その大学で個人IDと学籍番号が一致することを確認した上で利用することが望ましい。

---

## 15. Transparent Sessionの終了

読み取りが終了したらTransparent Sessionを終了する。

```python
END_SESSION = [
    0xFF, 0xC2, 0x00, 0x00,
    0x02,
    0x82, 0x00,
    0x00
]

conn.control(
    escape_code,
    END_SESSION
)
```

Session終了後はFeliCaコマンドを送信できないため、

```text
Session開始
↓
FeliCaへ切り替え
↓
FE00 Polling
↓
FCF基本情報読み取り
↓
Session終了
```

の順序を守る必要がある。

---

## 16. 処理フロー

全体の処理は以下となる。

```text
SONY RC-S300
      │
      ▼
PC/SC / pyscard
      │
      ▼
CCID Direct Mode
      │
      ▼
Transparent Session開始
      │
      ▼
NFC-F / FeliCaへ切り替え
      │
      ▼
System Code FE00をPolling
      │
      ▼
FCF SystemのIDm取得
      │
      ▼
Read Without Encryption
Service Code 1A8B
Block 0
      │
      ▼
FCF個人ID取得
      │
      ▼
学籍番号として利用
      │
      ▼
Transparent Session終了
```

---

## 17. 本システムでの利用方針

入退室記録システムでは、カード固有のIDmを直接ユーザーIDとして利用するのではなく、FCF領域に格納された個人IDを利用する。

### IDmを使用しない理由

IDmは物理カードに紐づく識別子であり、カード再発行時に変更される可能性がある。

一方、FCFの個人IDに学籍番号が登録されている場合、カードが再発行されても同一人物について同じ学籍番号を取得できる可能性が高い。

そのため、

```text
物理カード
    ↓
FCF個人ID
    ↓
学籍番号
    ↓
システム上のユーザー
```

という対応関係とする。

---

## 18. 注意事項

### FCF準拠カードすべてで個人ID＝学籍番号とは限らない

FCFには共通フォーマットが存在するが、個人IDとして何を格納するかは発行組織の運用によって異なる可能性がある。

導入対象となる学生証について、FCF個人IDと学籍番号の対応を事前に確認する。

### System Codeを明示する

複数Systemを持つFeliCaカードでは、通常のPC/SC接続時にFCF Systemが選択されるとは限らない。

必ず、

```text
System Code = FE00
```

を明示してPollingする。

### 個人情報をログへ出力しない

開発時の確認を除き、以下の情報を不用意にログへ記録しない。

* FCF個人ID
* 学籍番号
* 氏名
* IDm
* その他カード内の個人情報

本番環境では、必要最小限の情報のみをアプリケーションへ渡す。

### 必要なブロックのみ読む

学籍番号のみを認証に利用する場合は、FCF基本情報4ブロックすべてを取得せず、Block 0のみを読む。

これにより、氏名など認証に不要な個人情報を取得しない設計とする。

---

## 19. まとめ

RC-S300をLinux上で使用してFCF準拠学生証の個人IDを取得するには、通常のPC/SC APDUだけでは不十分な場合がある。

RC-S300のCCID Transparent Exchangeを利用し、

1. Transparent Sessionを開始する
2. NFC-F/FeliCaモードへ切り替える
3. FCF System Code `FE00` を明示してPollingする
4. 取得したIDmを使用する
5. Service Code `1A8B` のBlock 0をRead Without Encryptionで読む
6. Block 0の個人ID領域を取得する
7. Transparent Sessionを終了する

という流れで読み取る。

入退室記録システムでは、認証に不要な個人情報を取得しないため、**Block 0のみを読み、FCF個人IDだけをアプリケーションに渡す構成**とする。
