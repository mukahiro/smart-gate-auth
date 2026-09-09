from __future__ import annotations

"""手元の画像を使ったInsightFace顔認証の簡易評価。

学習は行わない。

推奨ディレクトリ構成:

face_dataset/
├── registered/
│   ├── person_a/
│   │   ├── enroll/
│   │   │   ├── 01.jpg
│   │   │   ├── 02.jpg
│   │   │   └── 03.jpg
│   │   └── test/
│   │       ├── 04.jpg
│   │       └── 05.jpg
│   └── person_b/
│       ├── enroll/
│       └── test/
└── unknown/
    ├── stranger_a/
    │   ├── 01.jpg
    │   └── 02.jpg
    └── stranger_b/
        └── 01.jpg

registered/<person>/enroll の画像から人物prototypeを作る。
registered/<person>/test で登録済み人物の識別性能を測る。
unknown/<person> 以下はすべて未登録人物として unknown 判定を評価する。
"""

import argparse
import csv
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np
from insightface.app import FaceAnalysis

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


@dataclass(frozen=True)
class Config:
    dataset_dir: Path
    threshold: float = 0.50
    model_name: str = "buffalo_l"
    det_size: int = 640
    output_dir: Path = Path("./local_face_output")


@dataclass
class Prototype:
    person_name: str
    embedding: np.ndarray


@dataclass
class TestResult:
    image_path: Path
    true_name: str
    predicted_name: str
    best_match_name: str
    similarity: float
    is_registered: bool
    correct: bool


def l2_normalize(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-12:
        raise ValueError("Zero-length embedding was returned")
    return vector / norm


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))


def build_face_analyzer(model_name: str, det_size: int) -> FaceAnalysis:
    print(f"Loading InsightFace model: {model_name}")
    print(f"Detection size: {det_size}x{det_size}")
    app = FaceAnalysis(
        name=model_name,
        providers=["CPUExecutionProvider"],
    )
    app.prepare(ctx_id=-1, det_size=(det_size, det_size))
    return app


def list_images(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def select_best_face(faces: Iterable) -> object:
    faces = list(faces)
    if not faces:
        raise ValueError("No face detected")

    # 複数人が写っている場合は最大の顔を採用する。
    def score(face: object) -> float:
        x1, y1, x2, y2 = np.asarray(face.bbox, dtype=np.float32)
        return float(max(0.0, x2 - x1) * max(0.0, y2 - y1))

    return max(faces, key=score)


def load_image_bgr(image_path: Path) -> np.ndarray:
    # cv2.imread は日本語パス等で失敗する環境があるため imdecode を使う。
    data = np.fromfile(str(image_path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Could not read image")
    return image


def extract_embedding(app: FaceAnalysis, image_path: Path) -> np.ndarray:
    image_bgr = load_image_bgr(image_path)
    faces = app.get(image_bgr)
    face = select_best_face(faces)

    embedding = getattr(face, "normed_embedding", None)
    if embedding is None:
        embedding = face.embedding

    return l2_normalize(embedding)


def build_prototype(embeddings: list[np.ndarray]) -> np.ndarray:
    if not embeddings:
        raise ValueError("At least one embedding is required")
    mean_embedding = np.mean(np.stack(embeddings), axis=0)
    return l2_normalize(mean_embedding)


def recognize(
    embedding: np.ndarray,
    prototypes: dict[str, Prototype],
    threshold: float,
) -> tuple[str, str, float]:
    if not prototypes:
        raise ValueError("No registered prototypes")

    scores = {
        name: cosine_similarity(embedding, prototype.embedding)
        for name, prototype in prototypes.items()
    }
    best_match_name = max(scores, key=scores.get)
    best_similarity = scores[best_match_name]

    predicted_name = best_match_name if best_similarity >= threshold else "unknown"
    return predicted_name, best_match_name, best_similarity


def validate_dataset(dataset_dir: Path) -> tuple[Path, Path]:
    registered_dir = dataset_dir / "registered"
    unknown_dir = dataset_dir / "unknown"

    if not registered_dir.is_dir():
        raise ValueError(f"Missing directory: {registered_dir}")

    return registered_dir, unknown_dir


def evaluate(config: Config) -> list[TestResult]:
    registered_dir, unknown_dir = validate_dataset(config.dataset_dir)
    app = build_face_analyzer(config.model_name, config.det_size)

    prototypes: dict[str, Prototype] = {}
    registered_test_items: list[tuple[str, Path]] = []
    unknown_test_items: list[tuple[str, Path]] = []

    print("\n=== Enrollment ===")
    person_dirs = sorted(path for path in registered_dir.iterdir() if path.is_dir())
    if not person_dirs:
        raise ValueError(f"No person directories found in {registered_dir}")

    for person_dir in person_dirs:
        person_name = person_dir.name
        enroll_images = list_images(person_dir / "enroll")
        test_images = list_images(person_dir / "test")

        if not enroll_images:
            print(f"  SKIP {person_name}: no images in {person_dir / 'enroll'}")
            continue

        embeddings: list[np.ndarray] = []
        failed = 0
        for image_path in enroll_images:
            try:
                embeddings.append(extract_embedding(app, image_path))
            except ValueError as exc:
                failed += 1
                print(f"  enrollment failed: {image_path}: {exc}")

        if not embeddings:
            print(f"  SKIP {person_name}: no enrollment face was detected")
            continue

        prototypes[person_name] = Prototype(
            person_name=person_name,
            embedding=build_prototype(embeddings),
        )
        registered_test_items.extend((person_name, path) for path in test_images)

        print(
            f"  {person_name}: enrolled={len(embeddings)}, "
            f"failed={failed}, test={len(test_images)}"
        )

    if not prototypes:
        raise ValueError("No valid person could be enrolled")

    print("\n=== Unknown identities ===")
    if unknown_dir.is_dir():
        unknown_person_dirs = sorted(path for path in unknown_dir.iterdir() if path.is_dir())
        for person_dir in unknown_person_dirs:
            images = list_images(person_dir)
            unknown_test_items.extend((person_dir.name, path) for path in images)
            print(f"  {person_dir.name}: test={len(images)}")

        # unknown/ 直下に画像を置くことも許可する。
        direct_images = list_images(unknown_dir)
        unknown_test_items.extend((path.stem, path) for path in direct_images)
        if direct_images:
            print(f"  direct unknown images: test={len(direct_images)}")
    else:
        print("  unknown directory not found; unknown evaluation will be skipped")

    print(f"\nRegistered prototypes: {len(prototypes)}")
    print(f"Registered test images: {len(registered_test_items)}")
    print(f"Unknown test images: {len(unknown_test_items)}")
    print(f"Threshold: {config.threshold:.3f}")

    results: list[TestResult] = []

    def run_one(true_name: str, image_path: Path, is_registered: bool) -> None:
        try:
            embedding = extract_embedding(app, image_path)
        except ValueError as exc:
            print(f"  test failed: {image_path}: {exc}")
            return

        predicted_name, best_match_name, similarity = recognize(
            embedding,
            prototypes,
            config.threshold,
        )

        correct = (
            predicted_name == true_name
            if is_registered
            else predicted_name == "unknown"
        )

        results.append(
            TestResult(
                image_path=image_path,
                true_name=true_name,
                predicted_name=predicted_name,
                best_match_name=best_match_name,
                similarity=similarity,
                is_registered=is_registered,
                correct=correct,
            )
        )

        status = "OK" if correct else "FAIL"
        print(
            f"  [{status}] {image_path.name}: true={true_name}, "
            f"pred={predicted_name}, best={best_match_name}, sim={similarity:.3f}"
        )

    print("\n=== Registered recognition test ===")
    for true_name, image_path in registered_test_items:
        if true_name in prototypes:
            run_one(true_name, image_path, True)

    print("\n=== Unknown recognition test ===")
    for true_name, image_path in unknown_test_items:
        run_one(true_name, image_path, False)

    return results


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    return float(np.percentile(np.asarray(values, dtype=np.float32), q))


def print_summary(results: list[TestResult]) -> None:
    registered = [r for r in results if r.is_registered]
    unknown = [r for r in results if not r.is_registered]

    registered_correct = sum(r.correct for r in registered)
    registered_accepted = [r for r in registered if r.predicted_name != "unknown"]
    registered_identity_correct = sum(r.predicted_name == r.true_name for r in registered)
    unknown_correct = sum(r.correct for r in unknown)
    false_accepts = [r for r in unknown if r.predicted_name != "unknown"]
    all_correct = sum(r.correct for r in results)

    print("\n=== Result ===")
    if registered:
        print(
            "Registered identification accuracy : "
            f"{registered_identity_correct / len(registered):.3%} "
            f"({registered_identity_correct}/{len(registered)})"
        )
        print(
            "Registered accept rate             : "
            f"{len(registered_accepted) / len(registered):.3%} "
            f"({len(registered_accepted)}/{len(registered)})"
        )
        print(
            "Registered correct rate            : "
            f"{registered_correct / len(registered):.3%} "
            f"({registered_correct}/{len(registered)})"
        )

    if unknown:
        print(
            "Unknown reject rate                : "
            f"{unknown_correct / len(unknown):.3%} "
            f"({unknown_correct}/{len(unknown)})"
        )
        print(
            "Unknown false accept rate          : "
            f"{len(false_accepts) / len(unknown):.3%} "
            f"({len(false_accepts)}/{len(unknown)})"
        )

    if results:
        print(
            "Overall open-set accuracy          : "
            f"{all_correct / len(results):.3%} ({all_correct}/{len(results)})"
        )

    genuine_scores = [r.similarity for r in registered]
    impostor_scores = [r.similarity for r in unknown]

    if genuine_scores:
        print(
            "Genuine best similarity            : "
            f"median={np.median(genuine_scores):.3f}, "
            f"p05={percentile(genuine_scores, 5):.3f}, "
            f"min={min(genuine_scores):.3f}"
        )
    if impostor_scores:
        print(
            "Unknown best similarity            : "
            f"median={np.median(impostor_scores):.3f}, "
            f"p95={percentile(impostor_scores, 95):.3f}, "
            f"max={max(impostor_scores):.3f}"
        )


def find_diagnostic_threshold(results: list[TestResult]) -> tuple[float, float] | None:
    if not results:
        return None

    best_threshold = 0.0
    best_accuracy = -1.0

    for threshold in np.linspace(-0.10, 0.90, 201):
        correct = 0
        for result in results:
            prediction = (
                "unknown"
                if result.similarity < threshold
                else result.best_match_name
            )
            expected = result.true_name if result.is_registered else "unknown"
            correct += prediction == expected

        accuracy = correct / len(results)
        if accuracy > best_accuracy:
            best_accuracy = accuracy
            best_threshold = float(threshold)

    return best_threshold, best_accuracy


def save_results(results: list[TestResult], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "local_results.csv"

    with output_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "image_path",
                "true_name",
                "predicted_name",
                "best_match_name",
                "similarity",
                "is_registered",
                "correct",
            ]
        )
        for result in results:
            writer.writerow(
                [
                    str(result.image_path),
                    result.true_name,
                    result.predicted_name,
                    result.best_match_name,
                    f"{result.similarity:.6f}",
                    int(result.is_registered),
                    int(result.correct),
                ]
            )

    return output_path


def save_failures(results: list[TestResult], output_dir: Path) -> Path | None:
    failures = [r for r in results if not r.correct]
    if not failures:
        return None

    failures_dir = output_dir / "failures"
    failures_dir.mkdir(parents=True, exist_ok=True)

    for index, result in enumerate(failures, start=1):
        safe_true = result.true_name.replace("/", "_").replace("\\", "_")
        safe_pred = result.predicted_name.replace("/", "_").replace("\\", "_")
        dst = failures_dir / (
            f"{index:03d}_true-{safe_true}_pred-{safe_pred}_"
            f"sim-{result.similarity:.3f}{result.image_path.suffix.lower()}"
        )
        shutil.copy2(result.image_path, dst)

    return failures_dir


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description="Local images + InsightFace prototype-based face recognition test"
    )
    parser.add_argument("dataset_dir", type=Path)
    parser.add_argument("--threshold", type=float, default=0.50)
    parser.add_argument("--model", default="buffalo_l")
    parser.add_argument("--det-size", type=int, default=640)
    parser.add_argument("--output-dir", type=Path, default=Path("./local_face_output"))
    args = parser.parse_args()

    if args.det_size < 64:
        parser.error("--det-size must be >= 64")

    return Config(
        dataset_dir=args.dataset_dir,
        threshold=args.threshold,
        model_name=args.model,
        det_size=args.det_size,
        output_dir=args.output_dir,
    )


def main() -> None:
    config = parse_args()
    results = evaluate(config)
    print_summary(results)

    diagnostic = find_diagnostic_threshold(results)
    if diagnostic is not None:
        threshold, accuracy = diagnostic
        print(
            "\nDiagnostic best threshold on THIS test set: "
            f"{threshold:.3f} (open-set accuracy={accuracy:.3%})"
        )
        print(
            "Do not treat this as an unbiased threshold estimate; "
            "use a separate validation set for final tuning."
        )

    output_path = save_results(results, config.output_dir)
    failures_dir = save_failures(results, config.output_dir)

    print(f"\nCSV saved to: {output_path.resolve()}")
    if failures_dir is not None:
        print(f"Failure images copied to: {failures_dir.resolve()}")


if __name__ == "__main__":
    main()
