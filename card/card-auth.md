# カード認証アプリ

SONY PaSoRi RC-S300でFCF準拠の学生証を読み取り、学籍番号と入退室イベントをSmart Gate APIへ送信するPythonアプリケーションです。

## 機能

- FCF System Code `0xFE00`を明示的にPolling
- FCF基本情報 Service Code `0x1A8B`のBlock 0から個人IDを取得
- 個人IDをハイフンなしのASCII数字10桁の学籍番号として検証
- 学生証をかざすたびに入室・退出を切り替え（現在の暫定仕様）
- Bearer認証を使用して`POST /api/v1/attendance-events`へ送信
- UUIDで一意な`eventId`を生成
- API送信に失敗したイベントをSQLiteへ保存し、同じ`eventId`で再送
- カードを置いたままにした場合の重複読み取りを防止

## 必要環境

- Raspberry Pi 5、またはUbuntu/Linuxマシン
- Python 3.10以降
- SONY PaSoRi RC-S300
- FCF準拠学生証
- `pcscd`
- `libccid`
- Pythonパッケージ`pyscard`、`requests`

## セットアップ

### 1. PC/SC関連パッケージのインストール

```bash
sudo apt update
sudo apt install pcscd pcsc-tools libccid libpcsclite-dev swig python3-dev python3-venv
sudo systemctl enable --now pcscd
```

### 2. Python環境の作成

リポジトリのルートから実行します。

```bash
cd card
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd ..
```

### 3. RC-S300の確認

RC-S300を接続し、PC/SCから認識されていることを確認します。

```bash
pcsc_scan
```

`SONY FeliCa RC-S300/P`が表示されれば認識されています。

## CCID Escape Commandの有効化

FCF領域を選択するTransparent Exchangeには、libccidのCCID Escape Commandが必要です。`/etc/libccid_Info.plist`の設定を確認してください。

```bash
grep -A1 ifdDriverOptions /etc/libccid_Info.plist
```

値が`0x0000`の場合は、管理者権限で次のように変更します。

```xml
<key>ifdDriverOptions</key>
<string>0x0001</string>
```

既に別のフラグが設定されている場合は、現在値に`0x0001`をORしてください。変更後、サービスとリーダーを再初期化します。

```bash
sudo systemctl restart pcscd
```

必要に応じてRC-S300をUSBから抜き差ししてください。FeliCaコマンドやFCF領域の詳細は[fcf-card-reading.md](fcf-card-reading.md)を参照してください。

## 設定

実行前にBearer tokenを設定します。

```bash
export AUTH_APP_BEARER_TOKEN="your-secret-token"
```

利用可能な環境変数は次のとおりです。

| 環境変数 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- |
| `AUTH_APP_BEARER_TOKEN` | 必須 | なし | APIのBearer token |
| `AUTH_API_BASE_URL` | 任意 | `http://localhost:3000` | APIサーバーのOrigin |
| `AUTH_API_ATTENDANCE_ENDPOINT` | 任意 | `/api/v1/attendance-events` | 入退室イベントのパス |
| `AUTH_DEVICE_ID` | 任意 | `smart-gate-card-01` | 認証端末を識別するID |
| `AUTH_CARD_DB_PATH` | 任意 | `card/card_auth.db` | 状態・未送信イベント用SQLite DB |

設定例：

```bash
export AUTH_API_BASE_URL="http://192.168.1.100:3000"
export AUTH_APP_BEARER_TOKEN="your-secret-token"
export AUTH_DEVICE_ID="entrance-card-reader-01"
```

APIサーバー側にも同じ`AUTH_APP_BEARER_TOKEN`を設定してください。tokenはソースコードやGitへコミットしないでください。

## 実行

リポジトリのルートから実行します。

```bash
source card/.venv/bin/activate
python3 card/card-auth.py
```

起動後、次の表示が出たら学生証をかざします。

```text
Reader: SONY FeliCa RC-S300/P ...
学生証をかざしてください（Ctrl+Cで終了）
```

終了するには`Ctrl+C`を押してください。

### コマンドラインオプション

環境変数の代わりに一部の設定を引数で指定できます。

```bash
python3 card/card-auth.py \
  --api-url http://localhost:3000/api/v1/attendance-events \
  --device-id entrance-card-reader-01 \
  --db-path card/card_auth.db \
  --timeout 3 \
  --retries 2
```

```bash
python3 card/card-auth.py --help
```

セキュリティ上、tokenは`--token`より`AUTH_APP_BEARER_TOKEN`での指定を推奨します。

## APIへ送信するデータ

読み取りに成功すると、次の形式でイベントを送信します。

```json
{
  "eventId": "550e8400-e29b-41d4-a716-446655440000",
  "studentNumber": "1234567890",
  "deviceId": "entrance-card-reader-01",
  "method": "card",
  "eventType": "check_in",
  "authenticatedAt": "2026-09-09T08:45:12+09:00"
}
```

API仕様の詳細は[../api-endpoints.md](../api-endpoints.md)を参照してください。

## 入退室状態と再送

最初の読み取りは`check_in`、次の読み取りは`check_out`として扱います。状態はSQLiteの`card_status`テーブルに保存されるため、アプリを再起動しても維持されます。

イベントはAPIから正常応答を受け取るまで`pending_attendance_events`テーブルに保存されます。通信断などで送信に失敗した場合は、次回起動時または同じ学生証を再度読み取ったときに同一の`eventId`で再送します。API側では同じ`eventId`が二重登録されません。

この自動切替は現在の暫定仕様です。将来はユーザーが認証端末のGPIOボタンで「入室」または「退出」を選択し、その選択をAPIの`eventType`へ設定する方式に置き換える予定です。

## GPIOボタン対応（予定）

本番端末では、GPIOに接続したボタンをユーザーが操作して入室・退出を選択する予定です。選択結果を`check_in`または`check_out`へ変換し、カードから取得した学籍番号とともにSmart Gate APIへ送信します。GPIO対応後は、現在のローカル状態による自動切替を使用しません。

ボタンの個数、操作とカード読み取りの順序、GPIOのピン番号、プルアップ・プルダウン方式、チャタリング対策、操作タイムアウトは、端末の配線・操作仕様確定後に実装します。

## 処理の流れ

```text
RC-S300をDirect Modeで接続
  -> Transparent Sessionを開始
  -> NFC-Fへ切り替え
  -> System Code FE00をPolling
  -> Service Code 1A8B / Block 0を読み取り
  -> 10桁の学籍番号を検証
  -> 入室・退出イベントを作成
  -> Smart Gate APIへ送信
```

## トラブルシューティング

### `pyscardがインストールされていません`

仮想環境を有効にして依存パッケージをインストールしてください。

```bash
source card/.venv/bin/activate
pip install -r card/requirements.txt
```

### `RC-S300が見つかりません`

- USB接続を確認する
- `pcsc_scan`でリーダーが表示されるか確認する
- `systemctl status pcscd`でサービスの状態を確認する
- RC-S300を抜き差しする

### `FEATURE_CCID_ESC_COMMANDが利用できません`

「CCID Escape Commandの有効化」の手順に従い、`ifdDriverOptions`と`pcscd`を確認してください。

### `FCF個人IDが10桁の学籍番号ではありません`

読み取ったカードのFCF個人IDが、このシステムの想定する10桁の学籍番号と一致していません。カード発行元のFCFデータ仕様を確認してください。

### APIが`401 Unauthorized`を返す

- 認証アプリとAPIサーバーで同じ`AUTH_APP_BEARER_TOKEN`を設定する
- tokenの前後に意図しない空白や引用符が含まれていないか確認する
- APIサーバーを環境変数設定後に再起動する

## 関連ファイル

| ファイル | 内容 |
| --- | --- |
| `card-auth.py` | FCF学生証読み取り・API送信アプリ |
| `requirements.txt` | Python依存パッケージ |
| `fcf-card-reading.md` | RC-S300とFCFの通信仕様 |
| `fcf-card-reading.py` | FCF読み取りの参考実装 |

## 注意事項

- 本アプリはRC-S300とLinuxの組み合わせを前提としています。
- FCF個人IDの格納形式はカード発行元によって異なる場合があります。
- SQLite DBには学籍番号とカードのIDmが保存されるため、適切なファイル権限で管理してください。
