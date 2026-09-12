# 顔認証機能 設計書

## 1. 概要

本機能は、研究室入退室記録システムにおける本人確認手段の一つとして、顔認証を提供する。

顔認証には、事前学習済みの顔特徴抽出モデルを使用する。  
新しい利用者を追加するたびにモデルを再学習する方式は採用せず、登録時に顔画像から特徴ベクトル（Embedding）を抽出し、顔認証専用SQLiteへ保存する。

認証時には、カメラ画像から同様にEmbeddingを抽出し、登録済みEmbeddingとのコサイン類似度を計算する。  
最も高い類似度が所定の閾値以上であれば、対応する学籍番号を認証結果として返す。

---

## 2. 基本方針

- 顔認証モデルは事前学習済みモデルを使用し、運用中は原則固定する
- 利用者追加時にモデルの再学習を行わない
- 顔画像そのものは、Embedding生成後は原則保存しない
- 1人につき複数のEmbeddingを登録可能とする
- 顔認証用データは、学籍・出欠情報を保存する中央DBとは分離する
- 顔認証用SQLiteには、原則として学籍番号とEmbeddingのみを保存する
- 顔認証サービスは、認証成功時に学籍番号を返す
- 学籍情報、氏名、所属、入退室履歴等の管理は中央API側の責務とする

---

## 3. システム構成

```text
                         ┌─────────────────────┐
                         │       Web UI        │
                         │ 顔登録・学生情報管理 │
                         └──────────┬──────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │     Central API     │
                         │ 学籍情報・出欠管理   │
                         └──────────┬──────────┘
                                    │
                         顔登録要求 / 認証結果
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │ Face Auth Service   │
                         │      Python         │
                         ├─────────────────────┤
                         │ 顔検出              │
                         │ Embedding抽出       │
                         │ 類似度計算          │
                         └──────────┬──────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │       face.db       │
                         │ 顔認証専用SQLite    │
                         └─────────────────────┘
```

中央APIはシステム全体のデータ管理を担当する。

顔認証サービスは、顔画像を入力として受け取り、認証結果として学籍番号を返すことを主な責務とする。

---

## 4. 顔認証方式

### 4.1 Embedding方式

顔画像を直接人物クラスへ分類するニューラルネットワークは使用しない。

代わりに、事前学習済み顔認証モデルを使用し、顔画像を固定長の特徴ベクトルへ変換する。

```text
顔画像
  ↓
顔検出
  ↓
顔位置・向きの補正
  ↓
顔特徴抽出モデル
  ↓
Embedding
```

Embeddingは、同一人物であれば互いに近く、別人物であれば離れるような特徴空間上のベクトルである。

---

### 4.2 類似度計算

認証時には、入力画像から得られたEmbeddingと登録済みEmbeddingとのコサイン類似度を計算する。

```text
入力Embedding
     │
     ├── 登録Embedding A → similarity = 0.88
     ├── 登録Embedding B → similarity = 0.19
     ├── 登録Embedding C → similarity = 0.25
     └── ...
```

最大類似度を持つEmbeddingの学籍番号を候補とする。

最大類似度が閾値以上であれば認証成功とし、閾値未満であれば未登録人物として扱う。

---

## 5. 利用者登録

### 5.1 登録フロー

Web UIから、利用者の顔写真を複数枚登録する。

推奨枚数は3〜5枚程度とする。

```text
Web UI
  ↓
顔写真を複数枚送信
  ↓
Central API
  ↓
Face Auth Service
  ↓
各画像からEmbedding抽出
  ↓
品質・整合性確認
  ↓
face.dbへ保存
  ↓
登録完了
```

登録時にモデルの学習処理は行わない。

---

### 5.2 登録画像の例

同一人物について、完全に同じ条件の写真だけではなく、多少条件を変えた画像を登録することが望ましい。

例:

- 正面
- 少し右向き
- 少し左向き
- 表情違い
- 眼鏡あり・なし

ただし、極端な横顔や顔が小さすぎる画像等は登録対象から除外する。

---

### 5.3 登録時チェック

登録時には最低限、以下を確認する。

- 顔が検出できること
- 画像内の主要な顔が1人であること
- 顔領域が小さすぎないこと
- 著しいブレがないこと
- 登録画像同士のEmbeddingが大きく乖離していないこと

例えば3枚のうち1枚のみ他2枚との類似度が極端に低い場合、その画像の再撮影を要求する。

---

## 6. Embeddingの保存方法

### 6.1 1学生に対して複数Embeddingを保存

登録画像が複数存在する場合、それぞれの画像から生成したEmbeddingを個別に保存する。

```text
student_id = 12345678
  ├ Embedding 1
  ├ Embedding 2
  ├ Embedding 3
  └ Embedding 4
```

各Embeddingを平均して1本のPrototypeへ統合する方式も可能だが、本システムでは複数Embeddingをそのまま保持する方式を基本とする。

理由:

- 顔向きの違いを保持できる
- 表情差を保持できる
- 登録枚数が少なく、保存容量・計算量が問題にならない
- 後から個別Embeddingを削除・更新しやすい

---

## 7. 顔認証用SQLite

### 7.1 DB分離

学籍情報・入退室情報等を保存する中央DBとは別に、顔認証専用SQLiteを配置する。

```text
app.db
├ users
├ attendance_events
└ その他業務データ

face.db
└ face_embeddings
```

顔認証サービスが必要とする情報を最小限に限定する。

---

### 7.2 テーブル

```sql
CREATE TABLE face_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT NOT NULL,
    embedding BLOB NOT NULL
);

CREATE INDEX idx_face_embeddings_student_id
ON face_embeddings(student_id);
```

基本構成では、以下のみを保持する。

- `id`
- `student_id`
- `embedding`

必要になった場合のみ、以下の追加を検討する。

- `model_name`
- `model_version`
- `created_at`
- `quality_score`

---

### 7.3 Embedding形式

EmbeddingはJSON文字列ではなく、バイナリ形式（BLOB）での保存を推奨する。

例:

```python
embedding.astype("float32").tobytes()
```

読み込み時:

```python
embedding = np.frombuffer(blob, dtype=np.float32)
```

512次元の `float32` の場合、1Embeddingあたり約2KBである。

---

## 8. 認証処理

### 8.1 認証フロー

```text
カメラ画像
  ↓
顔検出
  ↓
Embedding抽出
  ↓
登録Embedding一覧と比較
  ↓
最大類似度を取得
  ↓
閾値判定
  ├ 閾値以上 → student_idを返す
  └ 閾値未満 → unknown
```

---

### 8.2 複数Embeddingとの比較

例:

```text
入力顔

student_id = 12345678
  embedding 1 → 0.82
  embedding 2 → 0.91
  embedding 3 → 0.76

student_id = 87654321
  embedding 1 → 0.18
  embedding 2 → 0.22
  embedding 3 → 0.20
```

この場合、

```text
best_student_id = 12345678
best_similarity = 0.91
```

となる。

`best_similarity` が閾値以上であれば `12345678` として認証する。

---

## 9. 類似度閾値

初期実験では、LFWを使用して以下の結果を確認した。

```text
Registered identification accuracy : 96.000%
Unknown reject rate                : 100.000%
Unknown false accept rate          : 0.000%
Overall open-set accuracy          : 98.000%

Genuine best similarity:
median = 0.763
p05    = 0.525

Unknown best similarity:
median = 0.111
p95    = 0.159
max    = 0.201
```

また、自前画像による本人テストでは以下の類似度を確認した。

```text
0.886
0.892
0.922
```

現時点では閾値 `0.50` を暫定値として使用する。

ただし最終的な閾値は、実際に使用するカメラ、照明、距離、人物構成で収集した検証データを使用して決定する。

特に、誤って他人を本人として認証するFalse Acceptを低く抑えることを優先する。

---

## 10. 顔画像の保存

顔画像そのものは認証処理には不要である。

登録処理は以下とする。

```text
顔画像受信
  ↓
Embedding生成
  ↓
Embedding保存
  ↓
元画像破棄
```

特別な用途がない限り、登録時の顔画像を永続保存しない。

---

## 11. Embeddingの保護

Embeddingは元画像そのものではないが、生体情報に由来するデータであるため保護対象とする。

将来的には、Embeddingを暗号化して保存することを検討する。

例:

```text
Embedding
  ↓
AES-GCM
  ↓
暗号化Embedding
  ↓
face.db
```

暗号鍵は `face.db` と同じ場所には保存せず、OSのアクセス権限を利用して別管理する。

例:

```text
/etc/smart-gate/face.key
```

DB単体が漏洩した場合に、生のEmbeddingが取得されることを防ぐ。

---

## 12. モデル管理

Embeddingは生成したモデルに依存する。

そのため、顔認証モデルを変更した場合、既存Embeddingとの互換性が失われる可能性がある。

モデル変更時には、原則として全利用者のEmbeddingを再登録する。

必要に応じて以下の情報をDBまたはシステム設定として管理する。

```text
model_name
model_version
embedding_dimension
```

---

## 13. Pythonサービス起動時の処理

研究室規模では登録人数が少ないため、認証ごとにSQLiteをSELECTしても性能上大きな問題はない。

ただし、より単純かつ高速にする場合は、Pythonサービス起動時に全Embeddingをメモリへロードする。

```text
face.db
  ↓ 起動時
SELECT student_id, embedding
  ↓
メモリ
  ↓
認証時はNumPy上で比較
```

登録・削除が行われた場合のみキャッシュを更新する。

---

## 14. 利用者追加・削除

### 14.1 追加

```text
Web UI
  ↓
顔写真登録
  ↓
Embedding生成
  ↓
face_embeddingsへINSERT
  ↓
登録完了
```

モデルの再学習は不要。

### 14.2 削除

```sql
DELETE FROM face_embeddings
WHERE student_id = ?;
```

これにより、その利用者の顔認証情報を完全に削除できる。

---

## 15. APIとの責務分離

### Central API

担当:

- 学生情報管理
- 学籍番号管理
- 出欠・入退室イベント管理
- Web UIとの通信
- 顔登録要求の受付
- 顔認証結果の受信

### Face Auth Service

担当:

- 顔検出
- 顔位置補正
- Embedding生成
- Embedding登録
- Embedding削除
- 類似度計算
- Unknown判定

顔認証サービスから中央APIへ返す主要な情報は以下とする。

```json
{
  "student_id": "12345678",
  "similarity": 0.91
}
```

認証失敗時:

```json
{
  "student_id": null,
  "similarity": 0.32
}
```

---

## 16. 想定する認証モデル

実験段階ではInsightFace系モデルを使用する。

Raspberry Pi 5で動作させる場合は、軽量モデルの利用も検討する。

候補:

- `buffalo_l`: 精度重視
- `buffalo_sc`: 軽量・Raspberry Pi向け候補

最終的なモデルは、実機上での認証速度と精度を比較して決定する。

---

## 17. 今後の検討事項

- 実際の研究室メンバーによる認証精度評価
- Raspberry Pi実機カメラでの評価
- 類似度閾値の決定
- 顔画像品質判定
- 顔登録時のUI
- Embedding暗号化
- モデル更新時の移行方法
- 顔認証サービスと中央API間の通信方式
- 顔認証キャッシュ更新方式
- なりすまし対策（写真・スマートフォン画面への対策）
- 顔認証失敗時のカード認証へのフォールバック

---

## 18. 設計概要

本システムの顔認証機能は、以下を基本設計とする。

```text
Web UIから顔画像を複数登録
           ↓
PythonでEmbedding抽出
           ↓
顔画像は破棄
           ↓
face.dbへ
student_id + Embeddingを保存
           ↓

認証時
カメラ画像
           ↓
Embedding抽出
           ↓
登録済みEmbeddingと
コサイン類似度比較
           ↓
閾値以上
           ↓
student_idをCentral APIへ返す
```

この方式により、新しい利用者を追加した場合でもニューラルネットワークの再学習を必要とせず、Embeddingの追加のみで即時に顔認証対象へ追加できる。
