from __future__ import annotations

"""
Raspberry Pi Cameraを使用するSmart Gate顔認証アプリ。

カメラ入力にはcv2.VideoCaptureではなくPicamera2/libcameraを使用する。

動作:
    1. face.dbから登録済みの顔埋め込みを読み込む。
    2. Picamera2でRaspberry Pi Cameraを開く。
    3. 指定時間だけフレームを取得する。
    4. 設定した間隔で顔認証を実行する。
    5. 登録顔との類似度が閾値以上になった時点で正常終了する。
    6. 認証されなければタイムアウト時に終了する。

取得した画像はディスクへ保存しない。

環境変数のデフォルト値:
    FACE_AUTH_DB_PATH=./face.db
    FACE_AUTH_MODEL_NAME=buffalo_sc
    FACE_AUTH_DET_SIZE=320
    FACE_AUTH_THRESHOLD=0.5
    FACE_AUTH_DURATION_SECONDS=8
    FACE_AUTH_INFERENCE_INTERVAL_SECONDS=0.3
    FACE_AUTH_CAMERA_INDEX=0
    FACE_AUTH_CAMERA_WIDTH=640
    FACE_AUTH_CAMERA_HEIGHT=480

実行例:
    python face_recognition_app.py

認証成功時の出力:
    {"authenticated":true,"studentNumber":"1234567890","similarity":0.812}

タイムアウト時の出力:
    {"authenticated":false,"reason":"timeout"}
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from insightface.app import FaceAnalysis

try:
    from picamera2 import Picamera2
except ImportError as exc:
    raise RuntimeError(
        "Picamera2 is not available. On Raspberry Pi OS, install it with "
        "'sudo apt install -y python3-picamera2'. If using a venv, create it "
        "with '--system-site-packages' so the apt-installed Picamera2/libcamera "
        "packages are visible."
    ) from exc


DB_PATH_DEFAULT = os.getenv("FACE_AUTH_DB_PATH", "./face.db")
MODEL_NAME_DEFAULT = os.getenv("FACE_AUTH_MODEL_NAME", "buffalo_sc")
DET_SIZE_DEFAULT = int(os.getenv("FACE_AUTH_DET_SIZE", "320"))
THRESHOLD_DEFAULT = float(os.getenv("FACE_AUTH_THRESHOLD", "0.5"))
DURATION_DEFAULT = float(os.getenv("FACE_AUTH_DURATION_SECONDS", "8"))
INTERVAL_DEFAULT = float(
    os.getenv("FACE_AUTH_INFERENCE_INTERVAL_SECONDS", "0.3")
)
CAMERA_INDEX_DEFAULT = int(os.getenv("FACE_AUTH_CAMERA_INDEX", "0"))
CAMERA_WIDTH_DEFAULT = int(os.getenv("FACE_AUTH_CAMERA_WIDTH", "640"))
CAMERA_HEIGHT_DEFAULT = int(os.getenv("FACE_AUTH_CAMERA_HEIGHT", "480"))

warnings.filterwarnings(
    "ignore",
    message="`estimate` is deprecated",
    category=FutureWarning,
)


@dataclass(frozen=True)
class FaceDatabase:
    student_numbers: list[str]
    embeddings: np.ndarray


@dataclass(frozen=True)
class MatchResult:
    student_number: str
    similarity: float


def emit(payload: dict) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def l2_normalize(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-12:
        raise ValueError("zero-length embedding")
    return vector / norm


def load_face_database(db_path: Path) -> FaceDatabase:
    if not db_path.exists():
        raise RuntimeError(f"Face database does not exist: {db_path}")

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT student_number, embedding
            FROM face_embeddings
            ORDER BY student_number
            """
        ).fetchall()

    if not rows:
        raise RuntimeError("No face embeddings are registered")

    student_numbers: list[str] = []
    vectors: list[np.ndarray] = []
    expected_dim: int | None = None

    for student_number, blob in rows:
        vector = np.frombuffer(blob, dtype=np.float32).copy()

        if vector.size == 0:
            raise RuntimeError("Face database contains an empty embedding")

        if expected_dim is None:
            expected_dim = vector.size
        elif vector.size != expected_dim:
            raise RuntimeError(
                "Face database contains embeddings with inconsistent dimensions"
            )

        student_numbers.append(str(student_number))
        vectors.append(l2_normalize(vector))

    return FaceDatabase(
        student_numbers=student_numbers,
        embeddings=np.vstack(vectors).astype(np.float32, copy=False),
    )


def build_face_analyzer(model_name: str, det_size: int) -> FaceAnalysis:
    analyzer = FaceAnalysis(
        name=model_name,
        providers=["CPUExecutionProvider"],
    )
    analyzer.prepare(
        ctx_id=-1,
        det_size=(det_size, det_size),
    )
    return analyzer


def extract_embedding(
    analyzer: FaceAnalysis,
    frame_bgr: np.ndarray,
) -> np.ndarray | None:
    faces = analyzer.get(frame_bgr)

    # 入退室端末では、顔がちょうど1人分だけ写ったフレームを受け付ける。
    if len(faces) != 1:
        return None

    face = faces[0]
    embedding = getattr(face, "normed_embedding", None)
    if embedding is None:
        embedding = face.embedding

    return l2_normalize(embedding)


def find_best_match(
    query_embedding: np.ndarray,
    database: FaceDatabase,
) -> MatchResult:
    if query_embedding.size != database.embeddings.shape[1]:
        raise RuntimeError(
            "Query embedding dimension does not match stored embeddings. "
            "Enrollment and recognition must use the same model."
        )

    # 全ベクトルはL2正規化済みなので、内積がコサイン類似度になる。
    similarities = database.embeddings @ query_embedding
    best_index = int(np.argmax(similarities))

    return MatchResult(
        student_number=database.student_numbers[best_index],
        similarity=float(similarities[best_index]),
    )


def open_camera(
    camera_index: int,
    width: int,
    height: int,
) -> Picamera2:
    cameras = Picamera2.global_camera_info()
    if not cameras:
        raise RuntimeError("No Raspberry Pi camera was detected")

    if camera_index < 0 or camera_index >= len(cameras):
        raise RuntimeError(
            f"Camera index {camera_index} is unavailable "
            f"(detected cameras: {len(cameras)})"
        )

    camera = Picamera2(camera_index)

    # Picamera2のRGB888をcapture_arrayで取得するとB、G、R順になるため、
    # OpenCVとInsightFaceへそのまま渡せる。
    config = camera.create_preview_configuration(
        main={
            "size": (width, height),
            "format": "RGB888",
        },
        buffer_count=4,
    )
    camera.configure(config)
    camera.start()

    return camera


def recognize_for_period(
    *,
    analyzer: FaceAnalysis,
    database: FaceDatabase,
    camera_index: int,
    camera_width: int,
    camera_height: int,
    duration: float,
    interval: float,
    threshold: float,
) -> MatchResult | None:
    camera = open_camera(
        camera_index=camera_index,
        width=camera_width,
        height=camera_height,
    )

    try:
        started_at = time.monotonic()
        deadline = started_at + duration
        last_inference_at = -float("inf")

        while time.monotonic() < deadline:
            # 取得したフレームはメモリ内だけで処理する。
            frame_bgr = camera.capture_array("main")

            now = time.monotonic()
            if now - last_inference_at < interval:
                continue

            last_inference_at = now

            query_embedding = extract_embedding(
                analyzer=analyzer,
                frame_bgr=frame_bgr,
            )

            if query_embedding is None:
                continue

            match = find_best_match(
                query_embedding=query_embedding,
                database=database,
            )

            log(
                f"face detected: similarity={match.similarity:.3f}, "
                f"accepted={match.similarity >= threshold}"
            )

            if match.similarity >= threshold:
                return match

        return None
    finally:
        try:
            camera.stop()
        finally:
            camera.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recognize one registered face for a fixed period "
            "using a Raspberry Pi Camera."
        )
    )
    parser.add_argument("--db", default=DB_PATH_DEFAULT)
    parser.add_argument("--model", default=MODEL_NAME_DEFAULT)
    parser.add_argument("--det-size", type=int, default=DET_SIZE_DEFAULT)
    parser.add_argument("--threshold", type=float, default=THRESHOLD_DEFAULT)
    parser.add_argument("--duration", type=float, default=DURATION_DEFAULT)
    parser.add_argument("--interval", type=float, default=INTERVAL_DEFAULT)
    parser.add_argument("--camera", type=int, default=CAMERA_INDEX_DEFAULT)
    parser.add_argument("--width", type=int, default=CAMERA_WIDTH_DEFAULT)
    parser.add_argument("--height", type=int, default=CAMERA_HEIGHT_DEFAULT)

    args = parser.parse_args()

    if args.det_size <= 0:
        parser.error("--det-size must be positive")
    if not -1.0 <= args.threshold <= 1.0:
        parser.error("--threshold must be between -1 and 1")
    if args.duration <= 0:
        parser.error("--duration must be positive")
    if args.interval < 0:
        parser.error("--interval must be zero or positive")
    if args.width <= 0 or args.height <= 0:
        parser.error("--width and --height must be positive")

    return args


def main() -> int:
    args = parse_args()

    try:
        database = load_face_database(Path(args.db))

        log(
            f"loaded {database.embeddings.shape[0]} embeddings "
            f"for {len(set(database.student_numbers))} students"
        )

        log(
            f"loading model: {args.model}, "
            f"det_size={args.det_size}"
        )
        analyzer = build_face_analyzer(
            model_name=args.model,
            det_size=args.det_size,
        )

        log(
            f"camera={args.camera}, "
            f"resolution={args.width}x{args.height}, "
            f"duration={args.duration:.1f}s, "
            f"interval={args.interval:.2f}s, "
            f"threshold={args.threshold:.3f}"
        )

        match = recognize_for_period(
            analyzer=analyzer,
            database=database,
            camera_index=args.camera,
            camera_width=args.width,
            camera_height=args.height,
            duration=args.duration,
            interval=args.interval,
            threshold=args.threshold,
        )

        if match is None:
            emit({
                "authenticated": False,
                "reason": "timeout",
            })
            return 0

        emit({
            "authenticated": True,
            "studentNumber": match.student_number,
            "similarity": round(match.similarity, 6),
        })
        return 0

    except KeyboardInterrupt:
        emit({
            "authenticated": False,
            "reason": "cancelled",
        })
        return 0

    except Exception as exc:
        log(f"error: {exc}")
        emit({
            "authenticated": False,
            "reason": "application_error",
        })
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
