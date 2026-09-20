# Experiments

このディレクトリには、Smart Gate Authの実装本体から独立した実験・検証用ファイルを格納します。
本番アプリの実行には使用しません。

- `card/`: カード認証の初期・単体プロトタイプとFCF参考実装
- `face/`: 顔認証の単体アプリ、方式設計、精度検証スクリプトとノートブック
- `terminal/`: LCDとブザーの手動動作確認スクリプト
- `sandbox/`: 用途を限定しない簡易な試行用ファイル

## 本番条件での顔認証評価

`face/production_condition_face_test.py` は、統合端末と同じ方式で少人数の
顔画像データを交差検証します。

- InsightFace `buffalo_sc`
- 顔検出サイズ `320 x 320`
- 登録画像ごとのEmbeddingを個別に保持
- 全登録Embeddingとのコサイン類似度から最大値を選択
- 顔が0人または複数人の画像は照合しない
- 1人を未登録者とし、全員が未登録者になるよう交代して評価

データセットは次のように配置します。各フォルダ名は結果CSV上の人物IDに
使用されます。

```text
face_dataset/
├── person01/
│   ├── 01.jpg
│   ├── 02.jpg
│   ├── 03.jpg
│   ├── 04.jpg
│   └── 05.jpg
├── person02/
│   └── ...
└── person06/
    └── ...
```

リポジトリ直下から実行します。既定では1人3枚を登録し、残りをテスト
として、登録画像の組合せを入れ替えます。

```bash
python -m experiments.face.production_condition_face_test face_dataset
```

主なオプション:

```bash
python -m experiments.face.production_condition_face_test face_dataset \
  --enroll-images 3 \
  --thresholds 0.40 0.45 0.50 0.55 0.60 0.65 \
  --output-dir production_condition_face_output
```

出力先には、全試行の詳細CSV、閾値ごとの集計CSV、実験条件のJSONを
保存します。登録用に選ばれた画像から顔を1人だけ検出できなかった場合、
その登録組合せは本番の登録APIと同様に不成立とし、別CSVへ記録します。

顔画像と出力CSVは個人情報としてアクセスを制限し、Gitにコミットしないで
ください。
