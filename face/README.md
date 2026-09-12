# 顔認証

`face/` は、利用者の顔情報を登録するHTTP APIと、Raspberry Pi Cameraの映像から利用者を識別する認証コンポーネントを提供します。

顔画像そのものは保存しません。顔の特徴を数値化した「顔埋め込み」と学籍番号をSQLiteの `face.db` に保存します。このデータも生体情報として厳重に管理してください。

## 2つの役割

| ファイル | 役割 | 起動方法 |
| --- | --- | --- |
| `register.py` | 顔画像から埋め込みを作成し、`face.db` へ登録するHTTP API | Uvicornで別プロセスとして起動 |
| `authenticator.py` | カメラ映像と `face.db` を照合する | `terminal.app` が自動で読み込む |

```text
管理者が顔画像を送信
  → register.py
  → 顔検出・埋め込み生成
  → face.db
  → authenticator.pyが読み込む
  → Raspberry Pi Cameraの映像と照合
  → 学籍番号と類似度を統合端末へ返す
```

## 必要なもの

### 顔登録API

- Python 3.10以降
- InsightFace / ONNX Runtimeが動作するLinuxマシン
- InsightFaceモデルの初回取得時のネットワーク接続

### 顔認証端末

- Raspberry Pi 5 / Raspberry Pi OS
- Raspberry Pi Camera
- Picamera2 / libcamera
- 登録済みの `face.db`

## セットアップ

Raspberry Pi上で顔登録APIと顔認証の両方を使う例です。Picamera2はOSのパッケージを使うため、仮想環境からシステムパッケージを参照できるようにします。

```bash
sudo apt update
sudo apt install python3-picamera2 python3-venv

python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python -m pip install -r face/requirements.txt
python -m pip install -r terminal/requirements.txt
```

初回起動時はInsightFaceがモデルを取得するため、時間がかかることがあります。

## 顔を登録する

### 1. 設定

```bash
export FACE_AUTH_APP_BEARER_TOKEN='replace-with-a-long-random-secret'
export FACE_AUTH_DB_PATH='face/face.db'
export FACE_AUTH_MODEL_NAME='buffalo_sc'
export FACE_AUTH_DET_SIZE='320'
```

| 環境変数 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- |
| `FACE_AUTH_APP_BEARER_TOKEN` | 必須 | なし | 登録APIを保護するBearer token |
| `FACE_AUTH_DB_PATH` | 任意 | `./face.db` | 顔埋め込みDBの保存先 |
| `FACE_AUTH_MODEL_NAME` | 任意 | `buffalo_sc` | InsightFaceのモデル名 |
| `FACE_AUTH_DET_SIZE` | 任意 | `320` | 顔検出器の入力サイズ |

登録と認証では必ず同じ `FACE_AUTH_MODEL_NAME` を使ってください。異なるモデルで作った埋め込みは比較できません。

### 2. APIを起動

リポジトリ直下から起動します。上の例のように `FACE_AUTH_DB_PATH=face/face.db` を設定すると、統合端末と同じDBを使用できます。未設定時の登録APIは、カレントディレクトリの `face.db` を使用します。

```bash
uvicorn face.register:app --host 127.0.0.1 --port 8001
```

別のマシンから接続する場合は、ファイアウォールやTLS終端を用意したうえで `--host 0.0.0.0` を指定します。登録APIをインターネットへ直接公開しないでください。

### 3. 稼働確認

```bash
curl http://127.0.0.1:8001/health
```

```json
{"status":"ok"}
```

### 4. 画像を登録

同じ人物の正面、左右への軽い顔向き、眼鏡の有無など、条件を少し変えた鮮明な画像を3～5枚用意することを推奨します。各画像には登録対象者だけが写るようにしてください。

```bash
curl -i -X PUT http://127.0.0.1:8001/ \
  -H "Authorization: Bearer ${FACE_AUTH_APP_BEARER_TOKEN}" \
  -F "studentNumber=1234567890" \
  -F "images=@front.jpg;type=image/jpeg" \
  -F "images=@left.jpg;type=image/jpeg" \
  -F "images=@right.jpg;type=image/jpeg"
```

- `studentNumber` は半角数字10桁
- `images` は1～10ファイル
- 1ファイルの上限は10 MiB
- 画像内の顔は1人だけ
- 成功時は `204 No Content`
- 1枚でも不正ならDBは更新されない
- 同じ学籍番号を再登録すると、従来の埋め込みをすべて置き換える

## 統合端末で顔認証を使う

`face.db` に1人以上を登録したうえで、リポジトリ直下から統合端末を起動します。

```bash
export AUTH_APP_BEARER_TOKEN='replace-with-a-long-random-secret'
export FACE_AUTH_DB_PATH='face/face.db'
export FACE_AUTH_MODEL_NAME='buffalo_sc'
export FACE_AUTH_THRESHOLD='0.5'
python -m terminal.app
```

カード認証を使わず、顔認証だけで起動する場合は `--disable-card` を付けます。

`FaceAuthenticator` は起動時に埋め込みDBを読み、認証受付中だけカメラを開きます。画面内に顔が1人だけあり、登録済み埋め込みとの類似度が閾値以上になると認証成功です。

端末の設定と操作方法は [統合認証端末](../terminal/README.md) を参照してください。

## データベースの扱い

`face.db` には学籍番号と顔埋め込みが保存されます。登録APIと認証端末を別のマシンで実行する場合は、更新済みDBを安全に端末へ配布する仕組みが必要です。

認証端末は各認証の開始時にDBの更新時刻を確認し、変更されていれば再読み込みします。

## トラブルシューティング

### 登録APIが起動しない

- `FACE_AUTH_APP_BEARER_TOKEN` が空でないか確認する
- 初回モデル取得用のネットワーク接続を確認する
- `FACE_AUTH_DB_PATH` の親ディレクトリへ書き込めるか確認する

### カメラが見つからない

```bash
rpicam-hello --list-cameras
```

カメラの接続、ケーブルの向き、別プロセスが使用中でないかを確認してください。

### 顔が認証されない

- カメラに顔を向け、画面内に1人だけが写るようにする
- 顔全体が映り、照明が明るく均一になるようにする
- 登録時と認証時のモデル名を一致させる
- 登録DBのパスと登録件数を確認する
- 実機で評価したうえで類似度閾値を調整する

閾値を下げると本人を受け入れやすくなる一方、他人を誤って受け入れる危険も高まります。運用環境で評価してから決定してください。

## セキュリティ

- Bearer tokenをソースコードやGitへ保存しない
- 登録APIをインターネットへ直接公開しない
- `face.db` は必要なプロセスと管理者だけが読み書きできるようにする
- 顔画像をクライアントやリバースプロキシのログへ残さない
- 写真によるなりすまし対策が必要な場合は、ライブネス検知または追加認証を導入する

## ファイル

| ファイル | 役割 |
| --- | --- |
| `register.py` | 顔埋め込み登録HTTP API |
| `authenticator.py` | 統合端末用の顔認証コンポーネント |
| `requirements.txt` | Python依存パッケージ |
| `face.db` | 学籍番号と顔埋め込みを保存するDB（Git管理外） |

単体顔認証アプリと評価スクリプトは `experiments/face/` に分離されています。
