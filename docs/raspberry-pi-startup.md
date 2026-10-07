# Raspberry Piで認証Appを自動起動する

Raspberry Pi 5（8GB）/ Raspberry Pi OS 64-bitで、統合認証端末と顔登録APIをsystemdで自動起動する。既存のPythonエントリーポイントを使用し、認証・イベント送信の仕様は変更しない。

| サービス | 実行内容 | 接続先・保存先 |
| --- | --- | --- |
| `smart-gate-face-register` | `uvicorn face.register:app` | `127.0.0.1:8001`、顔埋め込みDB |
| `smart-gate-terminal` | `python -m terminal.app` | GPIO・カメラ・カード・Web API |

Webアプリと同じPiへ配置する構成。顔登録APIはLANへ公開せず、Web APIから呼び出す。別のPiで動かす場合はAPIの待受・アクセス制御・接続先を別途調整する。

設定ファイルは `/etc/smart-gate-auth/auth.env`、顔埋め込みDBとInsightFaceモデルは `/var/lib/smart-gate-auth`、コードと仮想環境は `/opt/smart-gate-auth` に配置する。以下のコマンドは **Raspberry Pi上で実行**する。

## 1. 機器とPython環境

[統合端末](../terminal/README.md#ソフトウェアのセットアップ)、[顔認証](../face/README.md#セットアップ)、[カード認証](../card/README.md#ccid-escape-commandの設定)の手順で、カメラ・GPIO・I2C・RC-S300を手動起動で確認する。Pi 5に対応するGPIOライブラリを使用し、既に実機で動作している依存を不用意に置き換えない。

Picamera2はOSのパッケージを使い、仮想環境には `--system-site-packages` を付ける。PCの `.venv` をPiへコピーしない。I2Cを `raspi-config` で有効にし、カードのCCID Escape Commandも既存手順で設定する。

```bash
sudo apt update
sudo apt install python3-venv python3-dev python3-picamera2 pcscd pcsc-tools libccid libpcsclite-dev swig i2c-tools build-essential rsync sqlite3 curl
sudo systemctl enable --now pcscd.socket
```

## 2. 専用ユーザーと配置

リポジトリを `~/projects/smart-gate-auth` に配置した例。既に `smartgate` ユーザーがある場合、`useradd` は省略し、そのホームが `/var/lib/smart-gate-auth` になっているか `getent passwd smartgate` で確認する。InsightFaceはこのユーザーのホーム下の `.insightface` にモデルを置く。

```bash
sudo useradd --system --user-group --home-dir /var/lib/smart-gate-auth --no-create-home --shell /usr/sbin/nologin smartgate
sudo install -d -m 0755 /opt/smart-gate-auth
sudo install -d -o smartgate -g smartgate -m 0700 /var/lib/smart-gate-auth
sudo install -d -m 0700 /etc/smart-gate-auth
cd ~/projects/smart-gate-auth
sudo rsync -a --delete --exclude='.git/' --exclude='.venv/' --exclude='.env' --exclude='.env.local' --exclude='__pycache__/' --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' ./ /opt/smart-gate-auth/
sudo python3 -m venv --system-site-packages /opt/smart-gate-auth/.venv
sudo /opt/smart-gate-auth/.venv/bin/python -m pip install -r /opt/smart-gate-auth/requirements.txt
sudo chown -R root:root /opt/smart-gate-auth
sudo chmod -R a+rX /opt/smart-gate-auth
```

`rsync --delete` の宛先はこのアプリ専用とする。DB・秘密情報をコードの配置先へ置かない。既存 `smartgate` のホームが異なる場合は、他の用途に使っていないことを確認して `sudo usermod --home /var/lib/smart-gate-auth smartgate` を実行し、既存モデルも新しいホームへ移す。

Raspberry Pi OSの機器グループへ専用ユーザーを追加する。存在するグループだけを追加する。

```bash
for PI_AUTH_GROUP in video render gpio i2c; do
  if getent group "$PI_AUTH_GROUP" >/dev/null; then
    sudo usermod -aG "$PI_AUTH_GROUP" smartgate
  fi
done
id smartgate
ls -l /dev/gpiomem* /dev/i2c-* /dev/video* /dev/media*
```

PC/SCは `pcscd` 経由でアクセスする。専用ユーザーでも `pcsc_scan` が使えるか確認し、拒否される場合はPi側のPC/SC・polkit設定を調査する。全サービスをrootに変更して回避しない。サービスには `PrivateDevices` を設定しないため、機器グループの権限でGPIO・カメラへアクセスできる。

## 3. 環境変数と既存顔DB

```bash
sudo install -m 0600 deploy/raspberry-pi/auth.env.example /etc/smart-gate-auth/auth.env
sudoedit /etc/smart-gate-auth/auth.env
```

初回のみ設定例をコピーする。再設定時は既存の環境ファイルを編集し、トークンを上書きしない。

- `AUTH_APP_BEARER_TOKEN`: Web側の同名の値と一致させる。
- `FACE_AUTH_APP_BEARER_TOKEN`: Web側の同名の値と一致させる。イベント用とは異なる値を使う。
- `AUTH_API_BASE_URL=http://127.0.0.1:3000`: Web APIを同じPiで動かす場合。別ホストならnginxのLAN内URL（例: `http://192.168.1.100`）にする。
- `FACE_AUTH_DB_PATH=/var/lib/smart-gate-auth/face.db`: 登録API・統合端末で必ず同じDBを使う。
- `FACE_AUTH_MODEL_NAME`: 登録済み顔DBに使用したモデルと一致させる。

Web側の `FACE_AUTH_APP_URL` は `http://127.0.0.1:8001/` にする。systemdの環境ファイルは `KEY=value` 形式で、`export` や変数展開を使わない。サービスに設定した値は開発用 `.env` より優先される。本番へ `.env` をコピーしない。

既存の顔DBがある場合は、旧顔登録APIと旧認証端末を止めてバックアップし、初回起動前に移行する。新DBに登録済みの顔情報を上書きしない。

```bash
# 実際の旧DBのパスへ置き換える。sqlite3が新規DBを作らないよう存在も確認する。
test -f /absolute/path/to/old-face.db
sqlite3 /absolute/path/to/old-face.db ".backup '/tmp/smart-gate-face-initial.db'"
sudo install -o smartgate -g smartgate -m 0600 /tmp/smart-gate-face-initial.db /var/lib/smart-gate-auth/face.db
```

顔埋め込みは生体情報のため、コピー元・バックアップもアクセス制限する。顔画像自体を保存する処理は追加しない。

## 4. モデルを事前取得する

既存モデルがある場合は `.insightface` を `/var/lib/smart-gate-auth/.insightface` へコピーし、所有者を `smartgate:smartgate` にする。ない場合は、ネットワーク接続のある初期セットアップ時に専用ユーザーでモデルを取得する。モデル名は設定と一致させる。

```bash
cd /opt/smart-gate-auth
sudo -u smartgate -H .venv/bin/python -c 'from insightface.app import FaceAnalysis; app = FaceAnalysis(name="buffalo_sc", providers=["CPUExecutionProvider"]); app.prepare(ctx_id=-1, det_size=(320, 320))'
```

この処理が成功してからサービスを有効化する。モデルを事前配置することで、通常の起動時にダウンロードを必要としない。

## 5. サービスを有効化する

```bash
cd ~/projects/smart-gate-auth
sudo install -m 0644 face/smart-gate-face-register.service /etc/systemd/system/smart-gate-face-register.service
sudo install -m 0644 terminal/smart-gate-terminal.service /etc/systemd/system/smart-gate-terminal.service
sudo systemd-analyze verify /etc/systemd/system/smart-gate-face-register.service /etc/systemd/system/smart-gate-terminal.service
sudo systemctl daemon-reload
sudo systemctl enable --now smart-gate-face-register
curl --fail http://127.0.0.1:8001/health
```

モデル初期化が終わると `{"status":"ok"}` が返る。準備中ならログを確認して待つ。

**新規DBでは顔が未登録のため、顔認証は初期化できない。** Web管理画面から最初の利用者の顔を登録してから、統合端末を起動する。カードが使える場合、未登録状態でもカードだけで動作するが、その後顔を登録したら端末の再起動が必要になる。

```bash
sudo systemctl enable --now smart-gate-terminal
systemctl status smart-gate-face-register smart-gate-terminal --no-pager
sudo journalctl -u smart-gate-face-register -u smart-gate-terminal -b --no-pager -n 100
```

統合端末は登録APIのヘルスチェックを最大120秒待つ。タイムアウトや異常終了時は5秒後に再試行する。`systemctl stop` では再起動しない。顔登録APIは1 workerで起動し、モデルの不要な多重ロードを避ける。

既存の顔認証単体アプリや旧端末を同時に動かすとカメラ・GPIOが競合するため、今回のサービスを開始する前に旧プロセスを停止する。今回のサービスでカメラを使うのは統合端末だけで、登録APIは画像処理のみを行う。

## 6. 再起動確認

Web側の `smart-gate-api` と `nginx` も有効化されていることを確認して再起動する。

```bash
sudo reboot
```

再接続後:

```bash
systemctl is-enabled smart-gate-face-register smart-gate-terminal
systemctl is-active smart-gate-face-register smart-gate-terminal
curl --fail http://127.0.0.1:8001/health
curl --fail http://127.0.0.1:3000/api/v1/health
sudo journalctl -u smart-gate-face-register -u smart-gate-terminal -b --no-pager -n 100
```

LCD、ボタン、顔認証、カード認証、Web履歴への記録を実機で確認する。`active` だけでは顔とカードの両方が使えることは保証されない。ログの `face authentication unavailable` / `card authentication unavailable` を確認する。

## 更新・ログ・バックアップ

```bash
sudo journalctl -u smart-gate-terminal -f
sudo journalctl -u smart-gate-face-register -f
```

更新時は両サービスを停止し、顔DBをSQLiteの `.backup` でバックアップしてからソースと依存を更新する。DBとモデルは `/var/lib/smart-gate-auth` に残す。`/opt` の更新時に仮想環境を削除しない。設定例は再コピーせず、既存 `/etc/smart-gate-auth/auth.env` を維持する。

```bash
sudo systemctl stop smart-gate-terminal smart-gate-face-register
sudo install -d -m 0700 /var/backups/smart-gate-auth
PI_AUTH_BACKUP="/var/backups/smart-gate-auth/face-$(date +%Y%m%d-%H%M%S).db"
sudo sqlite3 /var/lib/smart-gate-auth/face.db ".backup '$PI_AUTH_BACKUP'"
sudo chmod 0600 "$PI_AUTH_BACKUP"
```

バックアップ成功後に配置手順のrsyncと依存インストールを行う。サービス定義を変更したら再配置と `daemon-reload` を行い、`sudo systemctl start smart-gate-face-register smart-gate-terminal` で再開する。バックアップは別媒体にも保管する。

現在の統合端末はイベントを保存・自動再送しない。Web APIの停止中や通信失敗時には `API ERROR` が表示されるため、利用者が再操作する必要がある。今回の自動起動設定では、このイベント送信仕様は変更していない。

### 起動しない場合

- `status=203/EXEC`: `/opt/smart-gate-auth/.venv/bin/python` の配置・実行権限を確認する。
- 環境ファイル読み込みエラー: 新しいパスは `/etc/smart-gate-auth/auth.env`。旧雛形の `/etc/smart-gate-terminal.env` から設定を移す。
- `Permission denied`: 専用ユーザーの機器グループ、PC/SC権限、DB・モデルの所有者を確認する。
- 登録API待機のタイムアウト: 登録APIのモデル初期化ログ、トークン、モデル取得、DB権限を確認する。
- `no face embeddings are registered`: 顔データ登録後に端末を再起動する。
- `no authentication device is available`: 顔とカードの初期化ログを調べる。

systemdの仕様は [公式serviceドキュメント](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml) を参照。
