# 統合認証端末

`terminal/` は、入室・退出ボタン、顔認証、学生証認証、LCD、ブザー、Smart Gate APIへの送信を1つにまとめるRaspberry Pi向けアプリです。

ユーザーは最初に「入室」または「退出」ボタンを押し、顔をカメラへ向けるか学生証をかざします。顔とカードは同時に受け付け、最初に成功した認証結果だけをAPIへ送信します。

状態機械、非同期処理、GPIO、I2C、PWM、HTTP通信などの背景は [統合認証端末の技術解説](TECHNICAL_GUIDE.md) にまとめています。

## 利用者の操作

1. LCDに待機画面が表示されていることを確認する。
2. 「入室」または「退出」ボタンを押す。
3. カメラに顔を向けるか、RC-S300に学生証をかざす。
4. LCDとブザーで結果を確認する。
5. カードを使った場合は、「カードを離してください」と表示されたら取り外す。

1回のボタン操作につき、入退室イベントは最大1件だけ生成されます。

## 処理の流れ

```text
入室・退出ボタン
          |
          v
     認証受付開始
       /       \
      v         v
  顔認証     カード認証
       \       /
        v     v
   最初の有効な結果
          |
          v
  入退室イベントを生成
          |
          v
    Smart Gate API
          |
          v
    LCD・ブザーで通知
```

端末はイベントをローカル保存しず、API送信に失敗しても自動再送しません。通信エラーが表示された場合は、ユーザーがもう一度操作します。

## 必要なもの

- Raspberry Pi 5 / Raspberry Pi OS
- Python 3.10以降
- Raspberry Pi Camera（顔認証を使う場合）
- SONY PaSoRi RC-S300（カード認証を使う場合）
- HD44780互換LCD2004 + PCF8574 I2Cバックパック
- パッシブ圧電ブザー
- 入室・退出用のモーメンタリボタン2個
- Smart Gate APIのURLとBearer token

顔とカードのどちらか一方だけでも起動できます。両方の初期化に失敗すると端末は起動を中止します。

## ハードウェア設定

| 機器 | 既定値 | 接続・動作 |
| --- | --- | --- |
| 入室ボタン | BCM GPIO 17 | GPIOとGND間に接続するアクティブLow |
| 退出ボタン | BCM GPIO 27 | GPIOとGND間に接続するアクティブLow |
| ブザー | BCM GPIO 18 / 4000 Hz | PWMで駆動 |
| LCD | I2C bus 1 / `0x27` | 20文字 × 4行 |

GPIO番号は物理ピン番号ではなくBCM番号です。電源電圧、ブザーの駆動回路、I2Cの電圧レベルは使用する機器の仕様を確認してください。

I2Cが有効かは `raspi-config`、LCDのアドレスは `i2cdetect -y 1` で確認できます。

## ソフトウェアのセットアップ

リポジトリ直下で仮想環境を作成します。Picamera2を使うため、Raspberry Pi OSのシステムパッケージを参照できるようにします。

```bash
sudo apt update
sudo apt install python3-venv python3-picamera2 python3-rpi.gpio \
  pcscd pcsc-tools libccid libpcsclite-dev swig python3-dev i2c-tools

python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

カード認証に必要なCCID Escape Commandの設定は [カード認証](../card/README.md) を参照してください。顔データの登録方法は [顔認証](../face/README.md) にあります。

## 設定

初回だけ、リポジトリ直下で設定ファイルを作成します。

```bash
cp .env.example .env
```

`.env` を開き、少なくとも `AUTH_APP_BEARER_TOKEN` にSmart Gate APIのBearer tokenを設定してください。以後は起動時に自動で読み込まれるため、毎回の `export` は不要です。

| 環境変数 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- |
| `AUTH_APP_BEARER_TOKEN` | 必須 | なし | Smart Gate API用Bearer token |
| `AUTH_API_BASE_URL` | 任意 | `http://localhost:3000` | APIサーバーのOrigin |
| `AUTH_API_ATTENDANCE_ENDPOINT` | 任意 | `/api/v1/attendance-events` | 入退室イベントのパス |
| `AUTH_DEVICE_ID` | 任意 | `smart-gate-terminal-01` | 端末を識別するID |
| `FACE_AUTH_DB_PATH` | 任意 | `face/face.db` | 顔埋め込みDB |
| `FACE_AUTH_MODEL_NAME` | 任意 | `buffalo_sc` | 顔認証モデル |
| `FACE_AUTH_THRESHOLD` | 任意 | `0.5` | 顔認証の類似度閾値 |
| `AUTH_LCD_BUS` | 任意 | `1` | LCDのI2C bus |
| `AUTH_LCD_ADDRESS` | 任意 | `0x27` | LCDのI2Cアドレス |

`.env` の記入例:

```dotenv
AUTH_APP_BEARER_TOKEN=replace-with-a-long-random-secret
AUTH_API_BASE_URL=http://192.168.1.100:3000
AUTH_DEVICE_ID=entrance-terminal-01
FACE_AUTH_DB_PATH=face/face.db
```

## 起動

リポジトリ直下で実行します。

```bash
source .venv/bin/activate
python -m terminal.app
```

主な起動オプション:

| オプション | 説明 |
| --- | --- |
| `--disable-card` | カード認証を使わない |
| `--disable-face` | 顔認証を使わない |
| `--console-buttons` | GPIOボタンの代わりに `i` / `o` / `q` をキーボード入力する |
| `--console-hardware` | LCDとブザーの代わりにログを使う |
| `--auth-timeout 8` | 1回の認証受付時間 |
| `--result-seconds 2` | 結果表示時間 |
| `--lcd-address 0x27` | LCDのI2Cアドレス |

ハードウェアのない開発PCで入力と表示だけを確認するには、次のように実行します。顔とカードの少なくとも一方は初期化できる必要があります。

```bash
python -m terminal.app --console-buttons --console-hardware
```

## LCD表示と対処

| 表示 | 意味 | 対処 |
| --- | --- | --- |
| `AUTHENTICATING` | 顔またはカードを受付中 | 顔を向けるか学生証をかざす |
| `AUTH SUCCESS` | 認証とAPIへの記録が成功 | 操作完了 |
| `AUTH FAILED` | 制限時間内に認証できなかった | 顔の向きやカードを確認し、再操作する |
| `API ERROR` | APIへ記録できなかった | ネットワークとAPIを確認し、再操作する |
| `DEVICE ERROR` | 使用中の認証機器がすべて失敗 | 端末ログを確認し管理者へ連絡する |
| `MESSAGE` / カード取り外し | 重複読み取り防止中 | 学生証をRC-S300から離す |

LCDの初期化または動作中のI2C通信に失敗した場合、表示はログへ切り替わり、認証処理は継続します。

## 手動で機器を確認する

手動確認スクリプトは本実装から分離し、`experiments/terminal/` にあります。

```bash
python -m experiments.terminal.buzzer_test all
python -m experiments.terminal.lcd_test --address 0x27 --seconds 30
```

## systemdで常駐起動する

`smart-gate-terminal.service` は、リポジトリを `/opt/smart-gate-auth` へ配置する想定の雛形です。

```bash
sudo cp terminal/smart-gate-terminal.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smart-gate-terminal
sudo systemctl status smart-gate-terminal
```

`/etc/systemd/system/smart-gate-terminal.service` の配置先、実行ユーザー、仮想環境のパスを実環境に合わせてください。また、`/etc/smart-gate-terminal.env` に必要な環境変数を設定し、実行ユーザーにカメラ、GPIO、I2C、PC/SCを利用する権限を与えます。

ログは次のコマンドで確認できます。

```bash
journalctl -u smart-gate-terminal -f
```

## トラブルシューティング

### `AUTH_APP_BEARER_TOKEN or --token is required`

リポジトリ直下の `.env` に `AUTH_APP_BEARER_TOKEN` を設定してから起動してください。

### `no authentication device is available`

顔とカードの初期化がどちらも失敗しています。直前のログに表示される `face authentication unavailable` または `card authentication unavailable` の詳細を確認してください。

### LCDが表示されない

- `i2cdetect -y 1` でアドレスを確認する
- アドレスが `0x27` でなければ `AUTH_LCD_ADDRESS` または `--lcd-address` で指定する
- LCD背面のコントラスト調整ねじを確認する
- SDA、SCL、電源、GNDの配線を確認する

### API通信エラー

- `AUTH_API_BASE_URL` と `AUTH_API_ATTENDANCE_ENDPOINT` を確認する
- APIサーバーと同じBearer tokenが設定されているか確認する
- Raspberry PiからAPIサーバーへ接続できるか確認する
- APIレスポンスに送信した `eventId`、`recorded` または `duplicate` の `status` が含まれるか確認する

## ファイル

| ファイル | 役割 |
| --- | --- |
| `app.py` | 起動・終了、機器の初期化、ボタン入力の受付 |
| `state_machine.py` | 1回の認証セッションと画面遷移の制御 |
| `models.py` | 認証結果と入退室イベントのデータ型 |
| `attendance_client.py` | Smart Gate APIへのHTTP送信 |
| `hardware/buttons.py` | 入室・退出ボタンのGPIO制御 |
| `hardware/lcd.py` | LCD表示、半角カタカナ変換、スクロール、ログへのフォールバック |
| `hardware/buzzer.py` | ブザーの鳴動パターン |
| `smart-gate-terminal.service` | systemd常駐起動の雛形 |
| `TECHNICAL_GUIDE.md` | 統合端末で使う技術の解説 |

全コンポーネントのPython依存パッケージはリポジトリ直下の `requirements.txt` で一括管理しています。

送信ペイロードは `terminal/models.py` の `AttendanceEvent.payload()`、必要なAPIレスポンスは `terminal/attendance_client.py` で確認できます。

## 関連資料

- [統合認証端末の設置と運用](README.md)
- [顔認証の技術解説](../face/TECHNICAL_GUIDE.md)
- [カード認証の技術解説](../card/TECHNICAL_GUIDE.md)
