# Smart Gate Auth

Smart Gate Authは、顔認証と学生証によるカード認証を使って利用者を識別し、入退室記録へ連携するための認証アプリケーション群です。

## 認証方式

### 顔認証

顔画像から抽出した顔埋め込みをSQLiteへ登録し、Raspberry Pi Cameraで取得した顔を登録データと照合して利用者を識別します。

- Bearer認証付きHTTP APIによる顔埋め込みの登録・一括置換
- Picamera2/libcameraを使ったカメラ画像からの顔検出
- 登録済み利用者とのコサイン類似度による顔照合
- 学籍番号と類似度を含むJSON認証結果の生成
- 元の登録画像とカメラ画像を保存しないインメモリ処理

顔画像・埋め込み登録サーバーと顔認証アプリは実装済みで、Raspberry Pi実機で動作確認済みです。現在の顔認証アプリは認証結果のJSON出力までを担当し、入退室イベントAPIへの送信は行いません。セットアップ、登録API、実行方法、設定、トラブルシューティングは[顔認証アプリのドキュメント](face/face-auth.md)を参照してください。

### カード認証

SONY PaSoRi RC-S300を使い、FCF準拠学生証のFCF基本情報から10桁の学籍番号を読み取ります。現在は読み取り後にローカルの入退室状態を切り替え、Smart Gate APIへイベントを送信します。

- FCF System Code `0xFE00`を明示的にPolling
- Service Code `0x1A8B`のBlock 0から学籍番号を取得
- 入室・退出イベントの生成
- API障害時のローカル保存と冪等な再送
- 同じカードを置いたままにした場合の重複読み取り防止

セットアップ、環境変数、実行方法、カード通信仕様、トラブルシューティングは[カード認証アプリのドキュメント](card/card-auth.md)を参照してください。

## API連携

顔認証とカード認証で使用する共通の入退室イベントAPI仕様を定義しています。カード認証アプリからの送信は実装済みです。顔認証アプリへの送信処理は未実装で、現在は標準出力のJSONを後続処理へ渡す構成です。

```http
POST /api/v1/attendance-events
Authorization: Bearer <AUTH_APP_BEARER_TOKEN>
Content-Type: application/json
```

認証方式はリクエストの`method`で区別します。

| 認証方式 | `method` | 追加情報 |
| --- | --- | --- |
| 顔認証 | `face` | `confidence`が必須 |
| カード認証 | `card` | FCFから取得した学籍番号を使用 |

APIへ連携する際は、一意な`eventId`、学籍番号、端末ID、入退室種別、認証日時を送信します。同じ`eventId`を再送してもAPI側では二重登録されません。詳細は[API仕様](api-endpoints.md)を参照してください。

## 全体構成

```text
顔画像                         FCF学生証
  |                               |
  v                               v
顔検出・照合                   RC-S300 / PC/SC
  |                               |
  v                               v
学籍番号・類似度のJSON出力     FCF個人ID読み取り
  |                               |
  |（API送信は今後実装）           v
  |                         入退室イベント生成
  |                               |
  +-------------------------------+
                  |
                  v
      POST /api/v1/attendance-events
```

## 実装状況

| 項目 | 状態 | 主なファイル |
| --- | --- | --- |
| 顔画像・埋め込み登録 | HTTPサーバーを実装済み | `face/face_auth_app.py` |
| 顔認証 | Raspberry Pi Camera向けアプリを実装・実機確認済み | `face/face_recognition_app.py` |
| カード認証 | RC-S300向け常駐アプリを実装済み | `card/card-auth.py` |
| 入退室API仕様 | 顔・カード共通仕様を定義済み | `api-endpoints.md` |

## 今後の予定

入室・退出の選択には、認証端末へ接続したGPIOボタンを使用する予定です。ユーザーがGPIOボタンを操作して「入室」または「退出」を選択し、その結果を`eventType`として顔認証・カード認証のイベントに反映します。

現在のカード認証アプリが行うローカル状態の自動切替は暫定実装であり、GPIOボタン対応後に置き換える予定です。

## ファイル構成

```text
smart-gate-auth/
├── README.md
├── api-endpoints.md          # 共通API仕様
├── face/
│   ├── face_auth_app.py      # 顔画像・埋め込み登録サーバー
│   ├── face_recognition_app.py # Raspberry Pi向け顔認証アプリ
│   ├── face-auth.md          # 顔認証のセットアップ・運用方法
│   └── requirements.txt      # 顔認証のPython依存関係
└── card/
    ├── card-auth.py          # カード認証アプリ
    ├── card-auth.md          # カード認証のセットアップ・運用方法
    ├── requirements.txt      # カード認証のPython依存関係
    ├── fcf-card-reading.md   # RC-S300・FCF通信仕様
    └── fcf-card-reading.py   # FCF読み取り参考実装
```

## セキュリティと運用上の注意

- 顔画像、顔埋め込み、学籍番号、カードIDmは個人情報として適切に管理してください。顔埋め込みを保存する`face.db`も生体情報としてアクセス制御してください。
- APIのBearer tokenをソースコードやGitへコミットしないでください。
- 顔認証は、実運用環境の照明・角度・カメラを使って本人受入率と他人受入率を評価してから導入してください。
- カード認証の入退室判定は現在ローカル状態を基準にした暫定実装です。将来はGPIOボタンでユーザーが選択した入室・退出を使用します。
