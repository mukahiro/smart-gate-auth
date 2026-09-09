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
    uvicorn face_auth_app:app --host 127.0.0.1 --port 8001
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


MAX_IMAGES = 10
MAX_IMAGE_BYTES = 10 * 1024 * 1024
STUDENT_NUMBER_PATTERN = re.compile(r"^[0-9]{10}$")

DB_PATH = Path(os.getenv("FACE_AUTH_DB_PATH", "./face.db"))
MODEL_NAME = os.getenv("FACE_AUTH_MODEL_NAME", "buffalo_sc")
DET_SIZE = int(os.getenv("FACE_AUTH_DET_SIZE", "320"))
BEARER_TOKEN = os.getenv("FACE_AUTH_APP_BEARER_TOKEN", "")

# FaceAnalysisは起動時に一度だけ読み込み、リクエスト間で再利用する。
# 同時リクエストからモデルが並行実行されないようロックで保護する。
face_analyzer: FaceAnalysis | None = None
model_lock = threading.Lock()


def init_database() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as conn:
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
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
                idx_face_embeddings_student_number
            ON face_embeddings(student_number)
            """
        )


def build_face_analyzer() -> FaceAnalysis:
    app = FaceAnalysis(
        name=MODEL_NAME,
        providers=["CPUExecutionProvider"],
    )
    app.prepare(ctx_id=-1, det_size=(DET_SIZE, DET_SIZE))
    return app


@asynccontextmanager
async def lifespan(_: FastAPI):
    global face_analyzer

    if not BEARER_TOKEN:
        raise RuntimeError(
            "FACE_AUTH_APP_BEARER_TOKEN is not set. "
            "Refusing to start without authentication."
        )

    init_database()
    face_analyzer = build_face_analyzer()

    yield

    face_analyzer = None


app = FastAPI(
    title="Smart Gate Face Auth App",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def authenticate_registration_request(request: Request, call_next):
    # multipartのフォーム項目を処理する前に認証する。
    if request.method == "PUT" and request.url.path == "/":
        authorization = request.headers.get("authorization", "")
        expected = f"Bearer {BEARER_TOKEN}"

        if not secrets.compare_digest(authorization, expected):
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized"},
                headers={"WWW-Authenticate": "Bearer"},
            )

    return await call_next(request)


def l2_normalize(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))

    if norm <= 1e-12:
        raise ValueError("Face model returned a zero-length embedding")

    return vector / norm


def decode_image(data: bytes) -> np.ndarray:
    encoded = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)

    if image is None:
        raise ValueError("Image could not be decoded")

    return image


def extract_embedding(image_bgr: np.ndarray) -> np.ndarray:
    if face_analyzer is None:
        raise RuntimeError("Face model is not initialized")

    with model_lock:
        faces = face_analyzer.get(image_bgr)

    if len(faces) == 0:
        raise ValueError("No face detected")

    if len(faces) > 1:
        raise ValueError("Multiple faces detected")

    face = faces[0]
    embedding = getattr(face, "normed_embedding", None)

    if embedding is None:
        embedding = face.embedding

    return l2_normalize(embedding)


async def read_and_validate_image(upload: UploadFile) -> bytes:
    content_type = upload.content_type or ""

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
    """1人の学生に登録された全埋め込みを原子的に置き換える。

    DELETEと全INSERTを同一トランザクションで実行するため、失敗時に
    一部の埋め込みだけが置き換わった状態にはならない。
    """
    blobs = [
        np.asarray(embedding, dtype=np.float32).tobytes()
        for embedding in embeddings
    ]

    with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")

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
            conn.rollback()
            raise


@app.put("/", status_code=204)
async def replace_face_images(
    student_number: Annotated[str, Form(alias="studentNumber")],
    images: Annotated[list[UploadFile], File()],
) -> Response:
    if not STUDENT_NUMBER_PATTERN.fullmatch(student_number):
        raise HTTPException(
            status_code=422,
            detail="studentNumber must be exactly 10 digits",
        )

    if not 1 <= len(images) <= MAX_IMAGES:
        raise HTTPException(
            status_code=422,
            detail="images must contain between 1 and 10 files",
        )

    # 全画像から正常な埋め込みを抽出してから、置換対象をまとめてDBへ反映する。
    embeddings: list[np.ndarray] = []

    for index, upload in enumerate(images, start=1):
        try:
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
            await upload.close()

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
    return {"status": "ok"}
