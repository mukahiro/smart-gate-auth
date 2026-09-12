from __future__ import annotations

"""
顔画像から顔埋め込みを登録するHTTPサービス。

インターフェース:
    PUT /
    Authorization: Bearer <FACE_AUTH_APP_BEARER_TOKEN>
    Content-Type: multipart/form-data

フォーム項目:
    studentNumber: 半角数字10桁の文字列
    images:        1～10個のimage/*ファイル（空でなく、各10 MiB以下）

動作:
    - 各画像から正規化済みの顔埋め込みを1件抽出する。
    - 画像が1件でも不正、または埋め込み抽出に失敗した場合はSQLiteを更新しない。
    - 全画像の処理に成功した場合、studentNumberの全埋め込みを原子的に置き換える。
    - 元画像は本アプリの永続DBやアプリ管理下のファイル領域に保存しない。
    - 成功時はHTTP 204 No Contentを返す。

SQLiteスキーマ:
    face_embeddings(student_number TEXT, embedding BLOB)

起動例:
    export FACE_AUTH_APP_BEARER_TOKEN='replace-with-a-long-random-secret'
    uvicorn register:app --host 127.0.0.1 --port 8001
"""

import os
import re
import secrets
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from insightface.app import FaceAnalysis


# 過大なリクエストがメモリを消費しないよう、枚数と1枚あたりのサイズを制限する。
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# \dは全角数字なども許可するため、0〜9を明示して半角数字10桁だけに限定する。
STUDENT_NUMBER_PATTERN = re.compile(r"^[0-9]{10}$")

# パスやモデルは実行環境ごとに変えられるよう、モジュール読み込み時に環境変数から取得する。
DB_PATH = Path(os.getenv("FACE_AUTH_DB_PATH", "./face.db"))
MODEL_NAME = os.getenv("FACE_AUTH_MODEL_NAME", "buffalo_sc")
DET_SIZE = int(os.getenv("FACE_AUTH_DET_SIZE", "320"))
BEARER_TOKEN = os.getenv("FACE_AUTH_APP_BEARER_TOKEN", "")

# FaceAnalysisは起動時に一度だけ読み込み、リクエスト間で再利用する。
# 同時リクエストからモデルが並行実行されないようロックで保護する。
face_analyzer: FaceAnalysis | None = None
model_lock = threading.Lock()


def init_database() -> None:
    """顔埋め込みの保存先テーブルと検索用インデックスを作成する。"""
    # DBファイルを別ディレクトリに置く設定でも、初回起動で作成できるようにする。
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as conn:
        # DB側にも学籍番号の制約を持たせ、API以外からの不正な書き込みを防ぐ。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS face_embeddings (
                student_number TEXT NOT NULL
                    CHECK (
                        length(student_number) = 10
                        AND student_number NOT GLOB '*[^0-9]*'
                    ),
                embedding BLOB NOT NULL
            )
            """
        )
        # 学籍番号単位の検索や置換を効率化する。
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
                idx_face_embeddings_student_number
            ON face_embeddings(student_number)
            """
        )


def build_face_analyzer() -> FaceAnalysis:
    """InsightFaceモデルをCPU推論用に初期化する。"""
    # Raspberry PiやGPUのない管理サーバーでも動くよう、CPU実行を指定する。
    app = FaceAnalysis(
        name=MODEL_NAME,
        providers=["CPUExecutionProvider"],
    )
    app.prepare(ctx_id=-1, det_size=(DET_SIZE, DET_SIZE))
    return app


@asynccontextmanager
async def lifespan(_: FastAPI):
    """API起動時にDBとモデルを準備し、終了時に解放する。"""
    global face_analyzer

    # 無認証で顔データを更新できる状態を避けるため、token未設定では起動しない。
    if not BEARER_TOKEN:
        raise RuntimeError(
            "FACE_AUTH_APP_BEARER_TOKEN is not set. "
            "Refusing to start without authentication."
        )

    # HTTPリクエストを受ける前に、DBと推論モデルの準備を完了させる。
    init_database()
    face_analyzer = build_face_analyzer()

    # yield中だけFastAPIがリクエストを受け付ける。
    yield

    # 停止時にモデルへの参照を外し、Pythonがメモリを解放できるようにする。
    face_analyzer = None


app = FastAPI(
    title="Smart Gate Face Auth App",
    # 顔登録APIの不要な情報公開を避けるため、自動生成ドキュメントは無効にする。
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def authenticate_registration_request(request: Request, call_next):
    """顔登録リクエストのBearer tokenをフォーム解析前に検証する。"""
    # multipartのフォーム項目を処理する前に認証する。
    if request.method == "PUT" and request.url.path == "/":
        authorization = request.headers.get("authorization", "")
        expected = f"Bearer {BEARER_TOKEN}"

        # compare_digestは文字が一致した位置による処理時間の差を抑え、token推測の手がかりを減らす。
        if not secrets.compare_digest(authorization, expected):
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized"},
                headers={"WWW-Authenticate": "Bearer"},
            )

    return await call_next(request)


def l2_normalize(vector: np.ndarray) -> np.ndarray:
    """ベクトルのL2ノルムを1にし、類似度計算に使える形にする。"""
    # 型と形を固定し、DB保存時と認証時で同じ表現を使う。
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))

    # ほぼ0のベクトルは割り算できず、顔特徴としても有効でない。
    if norm <= 1e-12:
        raise ValueError("Face model returned a zero-length embedding")

    return vector / norm


def decode_image(data: bytes) -> np.ndarray:
    """受信した画像バイトをOpenCVのBGR配列へ変換する。"""
    # HTTPで届いたbytesをNumPy配列として参照し、OpenCVに画像形式を解析させる。
    encoded = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)

    if image is None:
        raise ValueError("Image could not be decoded")

    return image


def extract_embedding(image_bgr: np.ndarray) -> np.ndarray:
    """画像内の1人分の顔から正規化済み埋め込みを取得する。"""
    if face_analyzer is None:
        raise RuntimeError("Face model is not initialized")

    # FaceAnalysisを複数リクエストから同時実行しないよう、推論中はロックする。
    with model_lock:
        faces = face_analyzer.get(image_bgr)

    # 未検出と複数人画像を拒否し、学籍番号と別人の顔を結び付けることを防ぐ。
    if len(faces) == 0:
        raise ValueError("No face detected")

    if len(faces) > 1:
        raise ValueError("Multiple faces detected")

    face = faces[0]
    # InsightFaceが正規化済み値を提供すれば優先し、なければ元の埋め込みを使う。
    embedding = getattr(face, "normed_embedding", None)

    if embedding is None:
        embedding = face.embedding

    return l2_normalize(embedding)


async def read_and_validate_image(upload: UploadFile) -> bytes:
    """アップロード画像の形式とサイズを検証して読み込む。"""
    content_type = upload.content_type or ""

    # 画像と宣言されたファイルだけを後続の画像デコーダへ渡す。
    if not content_type.startswith("image/"):
        raise HTTPException(
            status_code=422,
            detail="Every images file must have an image/* Content-Type",
        )

    # 上限より1バイトだけ多く読み、無制限にメモリへ載せずに超過を検出する。
    data = await upload.read(MAX_IMAGE_BYTES + 1)

    if len(data) == 0:
        raise HTTPException(
            status_code=422,
            detail="Empty image files are not allowed",
        )

    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Each image must be 10 MiB or smaller",
        )

    return data


def replace_embeddings(student_number: str, embeddings: list[np.ndarray]) -> None:
    """1人分の全埋め込みを1つのトランザクションで置き換える。"""
    # NumPy配列を型情報のそろったbytesにし、SQLiteのBLOBとして保存する。
    blobs = [
        np.asarray(embedding, dtype=np.float32).tobytes()
        for embedding in embeddings
    ]

    with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
        try:
            # 書き込みロックを先に取得し、同時更新との競合を避ける。
            conn.execute("BEGIN IMMEDIATE")

            # 旧データの削除と新データの登録を同じトランザクションで行う。
            conn.execute(
                "DELETE FROM face_embeddings WHERE student_number = ?",
                (student_number,),
            )

            conn.executemany(
                """
                INSERT INTO face_embeddings (student_number, embedding)
                VALUES (?, ?)
                """,
                [(student_number, blob) for blob in blobs],
            )

            conn.commit()
        except Exception:
            # 途中で1件でも失敗したら全更新を取り消し、旧データを保つ。
            conn.rollback()
            raise


@app.put("/", status_code=204)
async def replace_face_images(
    student_number: Annotated[str, Form(alias="studentNumber")],
    images: Annotated[list[UploadFile], File()],
) -> Response:
    """顔画像群を検証し、対象学生の埋め込みを一括置換する。"""
    # アプリとDBの両方で同じ学籍番号形式を保証する。
    if not STUDENT_NUMBER_PATTERN.fullmatch(student_number):
        raise HTTPException(
            status_code=422,
            detail="studentNumber must be exactly 10 digits",
        )

    # 顔画像なしの登録と、枚数過多による長時間の推論を拒否する。
    if not 1 <= len(images) <= MAX_IMAGES:
        raise HTTPException(
            status_code=422,
            detail="images must contain between 1 and 10 files",
        )

    # 全画像から正常な埋め込みを抽出してから、置換対象をまとめてDBへ反映する。
    embeddings: list[np.ndarray] = []

    for index, upload in enumerate(images, start=1):
        try:
            # 各画像に対し、読み込み、復号、顔検出、特徴抽出を順番に行う。
            data = await read_and_validate_image(upload)
            image_bgr = decode_image(data)
            embedding = extract_embedding(image_bgr)
            embeddings.append(embedding)
        except HTTPException:
            raise
        except ValueError as exc:
            # ファイル名、学籍番号、画像データなどの機微情報を応答やログに含めない。
            raise HTTPException(
                status_code=422,
                detail=f"Image {index} was rejected: {exc}",
            ) from exc
        finally:
            # 成功・失敗にかかわらず一時ファイルを閉じ、リソースを残さない。
            await upload.close()

    # 全画像の埋め込み生成に成功した後だけ、DB更新を始める。
    try:
        replace_embeddings(student_number, embeddings)
    except sqlite3.Error as exc:
        # DB内部情報や学籍番号をAPI応答に含めない。
        raise HTTPException(
            status_code=500,
            detail="Failed to update face enrollment",
        ) from exc

    return Response(status_code=204)


@app.get("/health")
def health() -> dict[str, str]:
    """APIプロセスが稼働中であることを返す。"""
    return {"status": "ok"}
