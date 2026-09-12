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

顔画像・埋め込み登録サーバーと統合認証App用の顔認証コンポーネントは実装済みで、Raspberry Pi実機で動作確認済みです。セットアップ、登録API、設定、トラブルシューティングは[顔認証のドキュメント](face/README.md)を参照してください。

### カード認証

SONY PaSoRi RC-S300を使い、FCF準拠学生証のFCF基本情報から10桁の学籍番号を読み取ります。統合認証Appはボタンで選択された種別を使い、イベントを保存・再送せずSmart Gate APIへ直接送信します。

- FCF System Code `0xFE00`を明示的にPolling
- Service Code `0x1A8B`のBlock 0から学籍番号を取得
- 入室・退出イベントの生成
- 同じカードを置いたままにした場合の重複読み取り防止

端末のセットアップと実行方法は[統合認証Appのドキュメント](terminal/README.md)、カード通信仕様は[FCF読み取り仕様](card/fcf-card-reading.md)を参照してください。

## API連携

顔認証とカード認証で使用する共通の入退室イベントAPI仕様を定義しています。統合認証Appでは、どちらの認証結果も共通イベントへ変換して送信します。

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
| 顔画像・埋め込み登録 | HTTPサーバーを実装済み | `face/register.py` |
| 顔認証 | Raspberry Pi Camera向け統合コンポーネントを実装済み | `face/authenticator.py` |
| カード認証 | RC-S300向け統合コンポーネントを実装済み | `card/authenticator.py` |
| 統合認証端末 | ボタン、顔・カード認証、通知、API送信を実装済み | `terminal/app.py` |
| 入退室API仕様 | 顔・カード共通仕様を定義済み | `api-endpoints.md` |

## 統合認証端末

GPIOボタンで「入室」または「退出」を選択し、顔認証とカード認証を並行受付します。最初の有効な認証結果だけを共通イベントへ変換して送信します。構成、配線、起動方法は[統合認証Appの設計・運用ドキュメント](terminal/README.md)を参照してください。

## ファイル構成

```text
smart-gate-auth/
├── README.md
├── face/
│   ├── register.py           # 顔画像・埋め込み登録API
│   ├── authenticator.py      # 統合端末用の顔認証コンポーネント
│   ├── README.md             # 顔認証のセットアップ・運用方法
│   └── requirements.txt      # 顔認証のPython依存関係
├── terminal/
│   ├── app.py                # 統合認証Appのエントリーポイント
│   ├── state_machine.py      # 認証セッションと状態遷移
│   ├── attendance_client.py  # 再送しない入退室APIクライアント
│   ├── hardware/             # ボタン、LCD、ブザーのアダプター
│   └── README.md             # 統合認証Appの設計・運用方法
├── card/
│   ├── authenticator.py      # 統合端末用のカード認証コンポーネント
│   └── requirements.txt      # カード認証のPython依存関係
└── experiments/             # 実験・手動検証用（実装本体には不使用）
```

## セキュリティと運用上の注意

- 顔画像、顔埋め込み、学籍番号、カードIDmは個人情報として適切に管理してください。顔埋め込みを保存する`face.db`も生体情報としてアクセス制御してください。
- APIのBearer tokenをソースコードやGitへコミットしないでください。
- 顔認証は、実運用環境の照明・角度・カメラを使って本人受入率と他人受入率を評価してから導入してください。
- 統合認証Appの入退室種別はGPIOボタンの選択結果を使用します。
