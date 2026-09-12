from __future__ import annotations

"""Raspberry Pi Cameraの顔を登録済み顔埋め込みと照合する。"""

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from terminal.models import AuthenticationResult


def _normalize(vector: np.ndarray) -> np.ndarray:
    """顔埋め込みのL2ノルムを1にし、比較可能な形にする。"""
    # DBから復元した配列の型と形をそろえ、後続の行列演算を安定させる。
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    # 長さ0のベクトルは除算できず、有効な顔特徴でもないため異常とする。
    if norm <= 1e-12:
        raise RuntimeError("face database contains a zero embedding")
    return vector / norm


class FaceAuthenticator:
    """モデルをメモリに維持し、認証中だけカメラを開く顔認証コンポーネント。"""

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
        """顔認証モデルと登録済み埋め込みを読み込む。"""
        # カメラと推論ライブラリはRaspberry Pi環境でだけ必要なため、クラス生成時に読み込む。
        from insightface.app import FaceAnalysis
        from picamera2 import Picamera2

        self._Picamera2 = Picamera2
        self._db_path = db_path
        self._threshold = threshold
        self._interval = inference_interval
        self._camera_index = camera_index
        self._width = width
        self._height = height
        # この端末はGPUを前提としないため、ONNX RuntimeのCPU実行を指定する。
        self._analyzer = FaceAnalysis(name=model_name, providers=["CPUExecutionProvider"])
        self._analyzer.prepare(ctx_id=-1, det_size=(det_size, det_size))

        # 学籍番号と埋め込みは同じ行順で持ち、類似度が最大の行から学籍番号を引けるようにする。
        self._student_numbers: list[str] = []
        self._embeddings = np.empty((0, 0), dtype=np.float32)
        self._db_mtime_ns = -1
        self.reload_database(force=True)

    def reload_database(self, *, force: bool = False) -> bool:
        """DBの更新を検知し、必要な場合だけ顔埋め込みを再読み込みする。"""
        # ファイルの更新時刻が同じなら、DBアクセスと配列再生成を省略する。
        mtime = self._db_path.stat().st_mtime_ns
        if not force and mtime == self._db_mtime_ns:
            return False
        with sqlite3.connect(self._db_path) as connection:
            # 学籍番号で並べ、読み込みごとに行順が不定にならないようにする。
            rows = connection.execute(
                "SELECT student_number, embedding FROM face_embeddings ORDER BY student_number"
            ).fetchall()
        if not rows:
            raise RuntimeError("no face embeddings are registered")
        numbers: list[str] = []
        vectors: list[np.ndarray] = []
        dimension: int | None = None
        for number, blob in rows:
            # SQLiteのBLOBを、登録時と同じfloat32の配列へ戻す。
            vector = _normalize(np.frombuffer(blob, dtype=np.float32).copy())
            # 次元が違うベクトルは1つの行列にできず、モデル不一致の可能性もある。
            if dimension is not None and vector.size != dimension:
                raise RuntimeError("face embedding dimensions are inconsistent")
            dimension = vector.size
            numbers.append(str(number))
            vectors.append(vector)
        # 全行の検証後にまとめて公開し、中途半端な登録データが認証に使われることを防ぐ。
        self._student_numbers = numbers
        self._embeddings = np.vstack(vectors).astype(np.float32, copy=False)
        self._db_mtime_ns = mtime
        return True

    def _open_camera(self) -> Any:
        """設定されたRaspberry Pi Cameraを開き、RGB撮影を開始する。"""
        # 設定された番号が、実際に接続されたカメラの範囲内か確認する。
        info = self._Picamera2.global_camera_info()
        if self._camera_index < 0 or self._camera_index >= len(info):
            raise RuntimeError("configured Raspberry Pi camera is unavailable")
        camera = self._Picamera2(self._camera_index)
        # InsightFaceへ渡す画像のサイズと色順序を固定する。
        config = camera.create_preview_configuration(
            main={"size": (self._width, self._height), "format": "RGB888"}, buffer_count=4
        )
        camera.configure(config)
        camera.start()
        return camera

    def authenticate(self, timeout: float, cancel: threading.Event) -> AuthenticationResult | None:
        """カメラの顔を登録情報と照合し、制限時間内の最初の成功結果を返す。"""
        # 登録APIによるDB更新を、次の認証開始時に反映する。
        self.reload_database()
        camera = self._open_camera()
        try:
            # OS時刻の変更に影響されないmonotonic時計で、認証の制限時間を管理する。
            deadline = time.monotonic() + timeout
            last_inference = -float("inf")
            while time.monotonic() < deadline and not cancel.is_set():
                frame = camera.capture_array("main")
                now = time.monotonic()
                # 全フレームを推論せず、設定間隔を空けてCPU負荷を抑える。
                if now - last_inference < self._interval:
                    continue
                last_inference = now
                faces = self._analyzer.get(frame)
                # 顔が0人または複数人の場合は、どの人を照合するか確定できないため見送る。
                if len(faces) != 1:
                    continue
                # モデルが正規化済み埋め込みを持たない場合は、元の埋め込みを正規化して使う。
                raw = getattr(faces[0], "normed_embedding", None)
                query = _normalize(raw if raw is not None else faces[0].embedding)
                # モデルとDBの埋め込み次元が違うと類似度を計算できない。
                if query.size != self._embeddings.shape[1]:
                    raise RuntimeError("face model and database dimensions do not match")
                # 正規化済み行列とベクトルの積で、全登録顔とのコサイン類似度を一度に求める。
                scores = self._embeddings @ query
                # 最も類似度の高い登録行を候補にし、閾値以上の場合だけ成功とする。
                best = int(np.argmax(scores))
                score = float(scores[best])
                if score >= self._threshold:
                    return AuthenticationResult(self._student_numbers[best], "face", score)
            return None
        finally:
            # 認証の成否や例外に関わらず、カメラを停止して次の利用のために解放する。
            try:
                camera.stop()
            finally:
                camera.close()

    def close(self) -> None:
        """保持している顔認証モデルへの参照を解放する。"""
        # FaceAnalysisに明示的なclose APIがないため、参照を外してPythonのリソース解放に任せる。
        self._analyzer = None
