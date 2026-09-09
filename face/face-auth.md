# 顔認証アプリ

顔画像から顔埋め込み（Embedding）を登録するHTTPサーバーと、Raspberry Pi Cameraで登録済みの顔を照合するアプリのセットアップ・運用方法です。

## 構成

| ファイル | 役割 |
| --- | --- |
| `face_auth_app.py` | 顔画像を受け取り、顔埋め込みをSQLiteへ登録するHTTPサーバー |
| `face_recognition_app.py` | Raspberry Pi Cameraの映像を登録済み埋め込みと照合する顔認証アプリ |
| `requirements.txt` | 共通のPython依存パッケージ |
| `face.db` | 顔埋め込みを保存するSQLite DB（登録サーバー起動時に作成、Git管理外） |

両アプリはInsightFaceの同じモデルを使用し、L2正規化した埋め込みを`face_embeddings`テーブルで共有します。登録サーバーと認証アプリを別のマシンで動かす場合は、同じ内容の`face.db`を認証端末へ安全に配布する仕組みが別途必要です。

```text
顔画像（1～10枚）
  -> 登録サーバー
  -> 顔検出・埋め込み抽出
  -> face.db
  -> Raspberry Pi顔認証アプリ
  -> カメラ映像とのコサイン類似度を計算
  -> JSON形式の認証結果
```

## 動作仕様

### 顔画像・埋め込み登録サーバー

- `PUT /`をBearer認証で保護
- 学籍番号は半角数字10桁
- 1回に1～10枚、各10 MiB以下の`image/*`を受付
- 各画像に顔が1人だけ写っている場合に登録
- 全画像の検証と抽出が成功した場合だけ、対象学生の既存埋め込みを一括置換
- 受信した元画像は保存しない
- `GET /health`で稼働確認が可能（認証不要）

### 顔認証アプリ

- 起動時に`face.db`の全埋め込みを読み込み
- Picamera2/libcamera経由でRaspberry Pi Cameraを使用
- 顔が1人だけ写っているフレームを一定間隔で照合
- 全登録埋め込みとのコサイン類似度から最大値を選択
- 最大類似度が閾値以上なら、学籍番号と類似度を標準出力へJSONで出力
- 認証されないまま指定時間が過ぎるとタイムアウト結果を出力
- カメラ画像は保存しない

顔認証アプリは認証結果のJSONを出力するところまでを担当します。現時点では、入退室イベントAPIへの送信は行いません。

## 必要環境

### 登録サーバー

- Python 3.10以降
- InsightFaceモデルを初回取得できるネットワーク接続
- CPU実行に必要な空きメモリとストレージ

### 顔認証端末

- Raspberry Pi 5
- Raspberry Pi OS
- Raspberry Pi Camera
- Python 3.10以降
- Picamera2/libcamera
- 登録済みの`face.db`
- InsightFaceモデルを初回取得できるネットワーク接続

## セットアップ

Raspberry Piで登録サーバーと顔認証アプリの両方を動かす例です。Picamera2はRaspberry Pi OSのパッケージを使用するため、仮想環境からシステムパッケージを参照できるようにします。

```bash
sudo apt update
sudo apt install python3-picamera2 python3-venv

cd face
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
```

初回起動時はInsightFaceが指定モデルをダウンロードするため、通常より時間がかかります。

登録サーバーだけをカメラのないLinuxマシンで動かす場合は、通常の仮想環境でも構いません。

```bash
cd face
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 顔画像の登録

### 1. 設定

十分に長いランダムなBearer tokenを環境変数へ設定します。

```bash
export FACE_AUTH_APP_BEARER_TOKEN="replace-with-a-long-random-secret"
```

| 環境変数 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- |
| `FACE_AUTH_APP_BEARER_TOKEN` | 必須 | なし | `PUT /`を保護するBearer token |
| `FACE_AUTH_DB_PATH` | 任意 | `./face.db` | 顔埋め込み用SQLite DB |
| `FACE_AUTH_MODEL_NAME` | 任意 | `buffalo_sc` | InsightFaceモデル名 |
| `FACE_AUTH_DET_SIZE` | 任意 | `320` | 顔検出器の入力サイズ |

相対パスは起動時のカレントディレクトリを基準に解決されます。以下の例では`face/face.db`が作成されます。

### 2. 起動

```bash
cd face
source .venv/bin/activate
uvicorn face_auth_app:app --host 127.0.0.1 --port 8001
```

別のマシンから接続させる場合は、ネットワーク構成とファイアウォールを確認したうえで`--host 0.0.0.0`を指定してください。Bearer tokenだけに依存せず、信頼できるネットワークまたはTLS終端の背後で公開してください。

### 3. 稼働確認

```bash
curl http://127.0.0.1:8001/health
```

```json
{"status":"ok"}
```

### 4. 顔画像を登録

同じ人物について、正面、左右への軽い顔向き、眼鏡の有無など条件を少し変えた鮮明な画像を3～5枚用意することを推奨します。各画像には登録対象者だけが写るようにしてください。

```bash
curl -i -X PUT http://127.0.0.1:8001/ \
  -H "Authorization: Bearer ${FACE_AUTH_APP_BEARER_TOKEN}" \
  -F "studentNumber=1234567890" \
  -F "images=@front.jpg;type=image/jpeg" \
  -F "images=@left.jpg;type=image/jpeg" \
  -F "images=@right.jpg;type=image/jpeg"
```

成功時は`204 No Content`です。同じ学籍番号を再登録すると、その学生の既存埋め込みは今回送信した画像の埋め込みへすべて置き換わります。画像が1枚でも不正な場合は、その学生の登録内容を変更しません。

## Raspberry Piで顔認証を実行

### 1. カメラ確認

Raspberry Pi Cameraが認識され、他のプロセスに使用されていないことを確認します。

```bash
rpicam-hello --list-cameras
```

### 2. 設定

| 環境変数 | デフォルト | 対応する引数 | 説明 |
| --- | --- | --- | --- |
| `FACE_AUTH_DB_PATH` | `./face.db` | `--db` | 顔埋め込み用SQLite DB |
| `FACE_AUTH_MODEL_NAME` | `buffalo_sc` | `--model` | InsightFaceモデル名 |
| `FACE_AUTH_DET_SIZE` | `320` | `--det-size` | 顔検出器の入力サイズ |
| `FACE_AUTH_THRESHOLD` | `0.5` | `--threshold` | 認証成功とするコサイン類似度の閾値 |
| `FACE_AUTH_DURATION_SECONDS` | `8` | `--duration` | 1回の認証受付時間（秒） |
| `FACE_AUTH_INFERENCE_INTERVAL_SECONDS` | `0.3` | `--interval` | 推論間隔の最小値（秒） |
| `FACE_AUTH_CAMERA_INDEX` | `0` | `--camera` | Picamera2のカメラインデックス |
| `FACE_AUTH_CAMERA_WIDTH` | `640` | `--width` | 取得画像の幅 |
| `FACE_AUTH_CAMERA_HEIGHT` | `480` | `--height` | 取得画像の高さ |

登録時と認証時は`FACE_AUTH_MODEL_NAME`を必ず同じ値にしてください。モデルが異なると埋め込みの比較が成立しません。顔の検出条件もそろえる場合は`FACE_AUTH_DET_SIZE`も同じ値にします。

### 3. 実行

登録サーバーと同じ`face/face.db`を使う場合は、次のように実行します。

```bash
cd face
source .venv/bin/activate
python3 face_recognition_app.py
```

引数で設定を変更する例です。

```bash
python3 face_recognition_app.py \
  --db ./face.db \
  --duration 8 \
  --threshold 0.5 \
  --camera 0 \
  --width 640 \
  --height 480
```

機械可読な最終結果は標準出力へ1行で出力されます。進捗とエラーは標準エラー出力へ出力されます。

認証成功:

```json
{"authenticated":true,"studentNumber":"1234567890","similarity":0.812}
```

タイムアウト:

```json
{"authenticated":false,"reason":"timeout"}
```

`Ctrl+C`による中断は`cancelled`、設定・DB・カメラなどのエラーは`application_error`です。認証成功、タイムアウト、中断の終了コードは`0`、アプリケーションエラーは`1`です。

## データベース

登録サーバーが次のテーブルとインデックスを自動作成します。

```sql
CREATE TABLE face_embeddings (
    student_number TEXT NOT NULL
        CHECK (
            length(student_number) = 10
            AND student_number NOT GLOB '*[^0-9]*'
        ),
    embedding BLOB NOT NULL
);

CREATE INDEX idx_face_embeddings_student_number
ON face_embeddings(student_number);
```

埋め込みはL2正規化済みの`float32`配列をBLOBとして保存します。SQLite DBには学籍番号と生体情報に相当する顔埋め込みが含まれるため、アクセス権限、バックアップ、転送、廃棄を適切に管理してください。

## トラブルシューティング

### `FACE_AUTH_APP_BEARER_TOKEN is not set`

登録サーバーの起動前に`FACE_AUTH_APP_BEARER_TOKEN`を設定してください。空のtokenでは起動しません。

### `Picamera2 is not available`

`python3-picamera2`をインストールし、`--system-site-packages`付きで作成した仮想環境を使用してください。

### `No Raspberry Pi camera was detected`

- カメラの接続とケーブルの向きを確認する
- `rpicam-hello --list-cameras`で認識状態を確認する
- カメラを使用中の別プロセスを終了する
- 必要に応じてRaspberry Piを再起動する

### `No face embeddings are registered`

認証アプリが参照している`face.db`に登録データがありません。登録サーバーで顔画像を登録し、両アプリの`FACE_AUTH_DB_PATH`が同じDBを指していることを確認してください。

### 顔が認識されない

- 画面内に1人だけが写っているか確認する
- 顔をカメラへ向け、照明を明るく均一にする
- 登録時と認証時のモデル設定を一致させる
- 実機環境で評価したうえで閾値を調整する
- 顔向きや眼鏡などの条件を変えた登録画像を追加する

閾値を下げると本人を受け入れやすくなる一方、他人を誤って受け入れる危険も高まります。運用環境の照明、距離、カメラで本人受入率と他人受入率を確認してから決定してください。

## セキュリティ上の注意

- Bearer tokenをソースコードやGitへコミットしない
- 登録APIをインターネットへ直接公開しない
- `face.db`を必要なプロセスと管理者だけが読める権限にする
- 顔画像をクライアントやリバースプロキシのログへ残さない
- 不要になった埋め込みを確実に削除できる運用を用意する
- 写真によるなりすましを防ぐ必要がある場合は、別途ライブネス検知や追加認証を導入する
