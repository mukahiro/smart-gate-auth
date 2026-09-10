from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from terminal.models import AuthenticationResult


def _normalize(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-12:
        raise RuntimeError("face database contains a zero embedding")
    return vector / norm


class FaceAuthenticator:
    """Model-resident face authenticator; camera is open only during a session."""

    def __init__(
        self,
        db_path: Path,
        *,
        model_name: str = "buffalo_sc",
        det_size: int = 320,
        threshold: float = 0.5,
        inference_interval: float = 0.3,
        camera_index: int = 0,
        width: int = 640,
        height: int = 480,
    ):
        from insightface.app import FaceAnalysis
        from picamera2 import Picamera2

        self._Picamera2 = Picamera2
        self._db_path = db_path
        self._threshold = threshold
        self._interval = inference_interval
        self._camera_index = camera_index
        self._width = width
        self._height = height
        self._analyzer = FaceAnalysis(name=model_name, providers=["CPUExecutionProvider"])
        self._analyzer.prepare(ctx_id=-1, det_size=(det_size, det_size))
        self._student_numbers: list[str] = []
        self._embeddings = np.empty((0, 0), dtype=np.float32)
        self._db_mtime_ns = -1
        self.reload_database(force=True)

    def reload_database(self, *, force: bool = False) -> bool:
        mtime = self._db_path.stat().st_mtime_ns
        if not force and mtime == self._db_mtime_ns:
            return False
        with sqlite3.connect(self._db_path) as connection:
            rows = connection.execute(
                "SELECT student_number, embedding FROM face_embeddings ORDER BY student_number"
            ).fetchall()
        if not rows:
            raise RuntimeError("no face embeddings are registered")
        numbers: list[str] = []
        vectors: list[np.ndarray] = []
        dimension: int | None = None
        for number, blob in rows:
            vector = _normalize(np.frombuffer(blob, dtype=np.float32).copy())
            if dimension is not None and vector.size != dimension:
                raise RuntimeError("face embedding dimensions are inconsistent")
            dimension = vector.size
            numbers.append(str(number))
            vectors.append(vector)
        # Publish a complete snapshot only after all rows have passed validation.
        self._student_numbers = numbers
        self._embeddings = np.vstack(vectors).astype(np.float32, copy=False)
        self._db_mtime_ns = mtime
        return True

    def _open_camera(self) -> Any:
        info = self._Picamera2.global_camera_info()
        if self._camera_index < 0 or self._camera_index >= len(info):
            raise RuntimeError("configured Raspberry Pi camera is unavailable")
        camera = self._Picamera2(self._camera_index)
        config = camera.create_preview_configuration(
            main={"size": (self._width, self._height), "format": "RGB888"}, buffer_count=4
        )
        camera.configure(config)
        camera.start()
        return camera

    def authenticate(self, timeout: float, cancel: threading.Event) -> AuthenticationResult | None:
        self.reload_database()
        camera = self._open_camera()
        try:
            deadline = time.monotonic() + timeout
            last_inference = -float("inf")
            while time.monotonic() < deadline and not cancel.is_set():
                frame = camera.capture_array("main")
                now = time.monotonic()
                if now - last_inference < self._interval:
                    continue
                last_inference = now
                faces = self._analyzer.get(frame)
                if len(faces) != 1:
                    continue
                raw = getattr(faces[0], "normed_embedding", None)
                query = _normalize(raw if raw is not None else faces[0].embedding)
                if query.size != self._embeddings.shape[1]:
                    raise RuntimeError("face model and database dimensions do not match")
                scores = self._embeddings @ query
                best = int(np.argmax(scores))
                score = float(scores[best])
                if score >= self._threshold:
                    return AuthenticationResult(self._student_numbers[best], "face", score)
            return None
        finally:
            try:
                camera.stop()
            finally:
                camera.close()

    def close(self) -> None:
        # The model remains resident for the process lifetime and owns no
        # explicit close API. Each session closes its camera in authenticate().
        pass


    def close(self) -> None:
        self._analyzer = None
