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

SONY PaSoRi RC-S300を使い、FCF準拠学生証のFCF基本情報から10桁の学籍番号を読み取ります。既存の単体アプリはローカルの入退室状態を切り替えます。統合認証Appはボタンで選択された種別を使い、イベントを保存・再送せずSmart Gate APIへ直接送信します。

- FCF System Code `0xFE00`を明示的にPolling
- Service Code `0x1A8B`のBlock 0から学籍番号を取得
- 入室・退出イベントの生成
- API障害時のローカル保存と冪等な再送
- 同じカードを置いたままにした場合の重複読み取り防止

セットアップ、環境変数、実行方法、カード通信仕様、トラブルシューティングは[カード認証アプリのドキュメント](card/card-auth.md)を参照してください。

## API連携

顔認証とカード認証で使用する共通の入退室イベントAPI仕様を定義しています。統合認証Appでは、どちらの認証結果も共通イベントへ変換して送信します。既存の顔認証単体アプリは標準出力へJSONを出力します。

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
学籍番号・類似度              FCF個人ID読み取り
  |                               |
  |                               v
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
| 顔認証 | Raspberry Pi Camera向け単体アプリと統合用コンポーネントを実装済み | `face/face_recognition_app.py`, `face/authenticator.py` |
| カード認証 | RC-S300向け常駐アプリを実装済み | `card/card-auth.py` |
| 統合認証端末 | ボタン、顔・カード認証、通知、API送信を実装済み | `terminal/app.py` |
| 入退室API仕様 | 顔・カード共通仕様を定義済み | `api-endpoints.md` |

## 統合認証端末

GPIOボタンで「入室」または「退出」を選択し、顔認証とカード認証を並行受付します。最初の有効な認証結果だけを共通イベントへ変換して送信します。構成、配線、起動方法は[統合認証Appの設計・運用ドキュメント](terminal/README.md)を参照してください。

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
├── terminal/
│   ├── app.py                # 統合認証Appのエントリーポイント
│   ├── state_machine.py      # 認証セッションと状態遷移
│   ├── attendance_api.py     # 再送しないAPIクライアント
│   ├── hardware/             # ボタン、LCD、ブザーのアダプター
│   └── README.md             # 統合認証Appの設計・運用方法
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
- 統合認証Appの入退室種別はGPIOボタンの選択結果を使用します。既存のカード単体アプリのローカル状態切替は互換用の暫定実装です。
