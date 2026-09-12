# カード認証

`card/` は、SONY PaSoRi RC-S300でFCF準拠学生証を読み取り、10桁の学籍番号を取得するプログラムです。

`CardAuthenticator` は統合端末から利用できるほか、カード単体で起動し、読み取った学籍番号をコンソールで確認できます。

NFC、FeliCa、FCF、PC/SCなどの背景は [カード認証の技術解説](TECHNICAL_GUIDE.md) にまとめています。

## できること

- RC-S300をPC/SCのDirect Modeで開く
- FCF System Code `0xFE00` を指定して学生証を検出する
- Service Code `0x1A8B` のBlock 0から学籍番号を読み取る
- 読み取った値が半角数字10桁か検証する
- 同じカードを置いたままにした場合、取り外しを待って重複認証を防ぐ

入室・退出の選択、API送信、LCD表示、ブザー制御は `terminal/` が担当します。カード側で入退室状態やイベントを保存することはありません。

## 読み取りの流れ

```text
学生証をかざす
  → RC-S300がカードを検出
  → FCF基本情報を読み取る
  → 10桁の学籍番号を検証
  → 統合端末へ認証結果を返す
  → 統合端末が入退室APIへ送信
```

## 必要なもの

- Raspberry Pi 5、またはLinuxマシン
- SONY PaSoRi RC-S300
- FCF準拠学生証
- Python 3.10以降
- `pcscd`、`libccid`、`pyscard`

Ubuntu / Raspberry Pi OSでのセットアップ例です。

```bash
sudo apt update
sudo apt install pcscd pcsc-tools libccid libpcsclite-dev swig python3-dev
sudo systemctl enable --now pcscd
python -m pip install -r requirements.txt
```

`pcsc_scan` を実行し、`SONY FeliCa RC-S300/P` が表示されることを確認してください。

## CCID Escape Commandの設定

FCF領域の読み取りには、`libccid` のCCID Escape Commandが必要です。

```bash
grep -A1 ifdDriverOptions /etc/libccid_Info.plist
```

`ifdDriverOptions` が `0x0000` の場合は `0x0001` へ変更します。別のフラグが設定済みなら、現在値に `0x0001` をORしてください。変更後は `pcscd` を再起動し、必要に応じてRC-S300を抜き差しします。

```bash
sudo systemctl restart pcscd
```

通信手順の参考資料は [FCF読み取り仕様](../experiments/card/fcf-card-reading.md) を参照してください。

## 起動方法

### カード単体で起動する

リポジトリ直下から次のコマンドを実行します。

```bash
python -m card.authenticator
```

起動後は学生証の読み取りを待ち、成功すると `学籍番号: 1234567890` の形式で表示します。カードを取り外すと次の読み取りに戻り、`Ctrl+C` で終了します。

単体実行では、入室・退出の判定、API送信、履歴保存は行いません。

### 統合端末で使う

カード認証を含む統合端末の起動方法です。初回にリポジトリ直下の `.env.example` を `.env` へコピーし、`AUTH_APP_BEARER_TOKEN` を設定してください。

```bash
cp .env.example .env  # 初回だけ
python -m terminal.app
```

顔認証を使わず、カード認証だけで起動する場合は次のようにします。

```bash
python -m terminal.app --disable-face
```

その他の設定と操作方法は [統合認証端末](../terminal/README.md) を参照してください。

## トラブルシューティング

### RC-S300が見つからない

- USB接続を確認する
- `pcsc_scan` でリーダーが表示されるか確認する
- `systemctl status pcscd` でPC/SCサービスを確認する
- RC-S300を抜き差しする

### `FEATURE_CCID_ESC_COMMAND is unavailable`

`ifdDriverOptions` の設定と `pcscd` の再起動を確認してください。

### カードをかざしても認証されない

- FCF準拠の学生証か確認する
- FCF個人IDが半角数字10桁で格納されているか発行元に確認する
- 端末ログの `card authentication unavailable` や `authentication device failed` を確認する

## ファイル

| ファイル | 役割 |
| --- | --- |
| `authenticator.py` | RC-S300の初期化、学生証の読み取り、単体起動用の `main()` |
| `TECHNICAL_GUIDE.md` | カード認証で使う技術の解説 |

Python依存パッケージはリポジトリ直下の `requirements.txt` で一括管理しています。

旧単体アプリと参考実装は `experiments/card/` に分離されています。

## 関連資料

- [カード認証のセットアップと運用](README.md)
- [RC-S300によるFCF読み取りの詳細](../experiments/card/fcf-card-reading.md)
- [統合認証端末の技術解説](../terminal/TECHNICAL_GUIDE.md)
