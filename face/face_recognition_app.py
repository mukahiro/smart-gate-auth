from __future__ import annotations

"""
Smart Gate - face recognition application.

Behavior:
    1. Start the process.
    2. Load registered embeddings from face.db.
    3. Open the camera.
    4. For a fixed period, periodically run face recognition.
    5. If a registered student exceeds the similarity threshold, print one JSON
       result to stdout and exit successfully.
    6. If the timeout expires, print {"authenticated": false, ...} and exit.

The application does NOT train a model and does NOT write captured frames to disk.

Default environment variables:
    FACE_AUTH_DB_PATH=./face.db
    FACE_AUTH_MODEL_NAME=buffalo_sc
    FACE_AUTH_DET_SIZE=320
    FACE_AUTH_THRESHOLD=0.5
    FACE_AUTH_DURATION_SECONDS=8
    FACE_AUTH_INFERENCE_INTERVAL_SECONDS=0.3
    FACE_AUTH_CAMERA_INDEX=0

Example:
    python face_recognition_app.py

    python face_recognition_app.py \
        --duration 8 \
        --threshold 0.5 \
        --camera 0

Output on success:
    {"authenticated":true,"studentNumber":"1234567890","similarity":0.812}

Output on timeout:
    {"authenticated":false,"reason":"timeout"}

Exit codes:
    0: recognition completed (success or timeout)
    1: application/configuration/runtime error
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

import cv2
import numpy as np
from insightface.app import FaceAnalysis


DB_PATH_DEFAULT = os.getenv("FACE_AUTH_DB_PATH", "./face.db")
MODEL_NAME_DEFAULT = os.getenv("FACE_AUTH_MODEL_NAME", "buffalo_sc")
DET_SIZE_DEFAULT = int(os.getenv("FACE_AUTH_DET_SIZE", "320"))
THRESHOLD_DEFAULT = float(os.getenv("FACE_AUTH_THRESHOLD", "0.5"))
DURATION_DEFAULT = float(os.getenv("FACE_AUTH_DURATION_SECONDS", "8"))
INTERVAL_DEFAULT = float(
    os.getenv("FACE_AUTH_INFERENCE_INTERVAL_SECONDS", "0.3")
)
CAMERA_INDEX_DEFAULT = int(os.getenv("FACE_AUTH_CAMERA_INDEX", "0"))

# InsightFace currently emits a scikit-image deprecation warning internally.
# It is unrelated to recognition correctness, so keep normal output clean.
warnings.filterwarnings(
    "ignore",
    message="`estimate` is deprecated",
    category=FutureWarning,
)


@dataclass(frozen=True)
class FaceDatabase:
    student_numbers: list[str]
    embeddings: np.ndarray  # shape: (N, D), L2-normalized


@dataclass(frozen=True)
class MatchResult:
    student_number: str
    similarity: float


def emit(payload: dict) -> None:
    """
    stdout is reserved for the machine-readable final result.
    """
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )


def log(message: str) -> None:
    """
    Human-readable diagnostics go to stderr so callers can parse stdout safely.
    """
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
            expected_dim = int(vector.size)
        elif vector.size != expected_dim:
            raise RuntimeError(
                "Face database contains embeddings with inconsistent dimensions"
            )

        vectors.append(l2_normalize(vector))
        student_numbers.append(str(student_number))

    embeddings = np.vstack(vectors).astype(np.float32, copy=False)

    return FaceDatabase(
        student_numbers=student_numbers,
        embeddings=embeddings,
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
    """
    Return one normalized embedding.

    For an entrance terminal, recognition is accepted only when exactly one
    face is visible. Zero or multiple faces simply cause this frame to be
    skipped; the application keeps trying until timeout.
    """
    faces = analyzer.get(frame_bgr)

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
    """
    Compare against every enrolled embedding.

    Because both the query and stored vectors are L2-normalized, dot product is
    cosine similarity. Multiple embeddings may belong to the same student; the
    single highest-scoring registered embedding wins.
    """
    if query_embedding.size != database.embeddings.shape[1]:
        raise RuntimeError(
            "Query embedding dimension does not match stored embeddings. "
            "The recognition model may differ from the enrollment model."
        )

    similarities = database.embeddings @ query_embedding
    best_index = int(np.argmax(similarities))

    return MatchResult(
        student_number=database.student_numbers[best_index],
        similarity=float(similarities[best_index]),
    )


def open_camera(camera_index: int) -> cv2.VideoCapture:
    camera = cv2.VideoCapture(camera_index)

    if not camera.isOpened():
        camera.release()
        raise RuntimeError(f"Could not open camera index {camera_index}")

    # Avoid building up an old-frame queue on backends that support this.
    camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    return camera


def recognize_for_period(
    *,
    analyzer: FaceAnalysis,
    database: FaceDatabase,
    camera_index: int,
    duration: float,
    interval: float,
    threshold: float,
) -> MatchResult | None:
    camera = open_camera(camera_index)

    try:
        deadline = time.monotonic() + duration
        last_inference_at = -float("inf")

        # Give auto exposure / white balance a short opportunity to settle while
        # still reading frames. This delay is included in the requested period.
        while time.monotonic() < deadline:
            ok, frame = camera.read()

            if not ok or frame is None:
                continue

            now = time.monotonic()

            if now - last_inference_at < interval:
                continue

            last_inference_at = now

            query_embedding = extract_embedding(analyzer, frame)

            if query_embedding is None:
                continue

            match = find_best_match(query_embedding, database)

            log(
                "face detected: "
                f"best_similarity={match.similarity:.3f}, "
                f"accepted={match.similarity >= threshold}"
            )

            if match.similarity >= threshold:
                return match

        return None
    finally:
        camera.release()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recognize one registered face for a fixed period."
    )
    parser.add_argument(
        "--db",
        default=DB_PATH_DEFAULT,
        help=f"SQLite face DB path (default: {DB_PATH_DEFAULT})",
    )
    parser.add_argument(
        "--model",
        default=MODEL_NAME_DEFAULT,
        help=f"InsightFace model name (default: {MODEL_NAME_DEFAULT})",
    )
    parser.add_argument(
        "--det-size",
        type=int,
        default=DET_SIZE_DEFAULT,
        help=f"Face detector input size (default: {DET_SIZE_DEFAULT})",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=THRESHOLD_DEFAULT,
        help=f"Cosine-similarity threshold (default: {THRESHOLD_DEFAULT})",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=DURATION_DEFAULT,
        help=f"Recognition period in seconds (default: {DURATION_DEFAULT})",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=INTERVAL_DEFAULT,
        help=(
            "Minimum seconds between inference attempts "
            f"(default: {INTERVAL_DEFAULT})"
        ),
    )
    parser.add_argument(
        "--camera",
        type=int,
        default=CAMERA_INDEX_DEFAULT,
        help=f"OpenCV camera index (default: {CAMERA_INDEX_DEFAULT})",
    )

    args = parser.parse_args()

    if args.det_size <= 0:
        parser.error("--det-size must be positive")
    if not -1.0 <= args.threshold <= 1.0:
        parser.error("--threshold must be between -1 and 1")
    if args.duration <= 0:
        parser.error("--duration must be positive")
    if args.interval < 0:
        parser.error("--interval must be zero or positive")

    return args


def main() -> int:
    args = parse_args()

    try:
        db_path = Path(args.db)

        log(f"loading face database: {db_path}")
        database = load_face_database(db_path)

        unique_students = len(set(database.student_numbers))
        log(
            f"loaded {database.embeddings.shape[0]} embeddings "
            f"for {unique_students} students"
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
            f"recognition started: duration={args.duration:.1f}s, "
            f"interval={args.interval:.2f}s, "
            f"threshold={args.threshold:.3f}"
        )

        match = recognize_for_period(
            analyzer=analyzer,
            database=database,
            camera_index=args.camera,
            duration=args.duration,
            interval=args.interval,
            threshold=args.threshold,
        )

        if match is None:
            emit(
                {
                    "authenticated": False,
                    "reason": "timeout",
                }
            )
            return 0

        emit(
            {
                "authenticated": True,
                "studentNumber": match.student_number,
                "similarity": round(match.similarity, 6),
            }
        )
        return 0

    except KeyboardInterrupt:
        emit(
            {
                "authenticated": False,
                "reason": "cancelled",
            }
        )
        return 0
    except Exception as exc:
        # Do not dump registered student numbers or embeddings.
        log(f"error: {exc}")
        emit(
            {
                "authenticated": False,
                "reason": "application_error",
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
