from __future__ import annotations

"""LFWを使ったInsightFace顔認証の簡易評価。

学習は行わない。

1. LFWから登録人物と未登録人物を選ぶ
2. 登録人物の複数画像からArcFace embeddingを生成する
3. embeddingの平均を人物prototypeとして登録する
4. 別画像をprototypeとcosine similarityで照合する
5. similarityが閾値未満なら unknown と判定する

実運用を想定した「embedding登録 + 類似度検索」の動作確認用。
"""

import argparse
import csv
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np
from insightface.app import FaceAnalysis
from sklearn.datasets import fetch_lfw_people


@dataclass(frozen=True)
class Config:
    registered_people: int = 10
    unknown_people: int = 10
    enroll_images: int = 3
    max_test_images_per_person: int = 5
    threshold: float = 0.50
    min_faces_per_person: int = 8
    model_name: str = "buffalo_l"
    seed: int = 42
    output_dir: Path = Path("./lfw_output")
    data_home: Path | None = None


@dataclass
class Prototype:
    person_name: str
    embedding: np.ndarray


@dataclass
class TestResult:
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
    # embeddingは正規化済みなので内積だけでcosine similarityになる。
    return float(np.dot(a, b))


def build_face_analyzer(model_name: str) -> FaceAnalysis:
    """CPUでInsightFaceを初期化する。初回はモデルがダウンロードされる。"""
    app = FaceAnalysis(
        name=model_name,
        providers=["CPUExecutionProvider"],
    )
    app.prepare(ctx_id=-1, det_size=(640, 640))
    return app


def lfw_rgb_to_bgr(image_rgb: np.ndarray) -> np.ndarray:
    """sklearnのLFW画像をInsightFaceに渡せるuint8/BGRへ変換する。"""
    image = np.asarray(image_rgb)

    if image.dtype != np.uint8:
        # fetch_lfw_peopleは通常0..255のfloat画像を返す。
        if image.max() <= 1.0:
            image = image * 255.0
        image = np.clip(image, 0, 255).astype(np.uint8)

    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(f"Expected RGB image, got shape={image.shape}")

    return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)


def select_best_face(faces: Iterable) -> object:
    faces = list(faces)
    if not faces:
        raise ValueError("No face detected")

    # LFWは基本的に1画像1人だが、複数検出時は最大の顔を採用する。
    def score(face: object) -> float:
        x1, y1, x2, y2 = np.asarray(face.bbox, dtype=np.float32)
        return float(max(0.0, x2 - x1) * max(0.0, y2 - y1))

    return max(faces, key=score)


def extract_embedding(app: FaceAnalysis, image_rgb: np.ndarray) -> np.ndarray:
    image_bgr = lfw_rgb_to_bgr(image_rgb)
    faces = app.get(image_bgr)
    face = select_best_face(faces)

    # normed_embeddingが利用できる場合はそれを優先する。
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
        person_name: cosine_similarity(embedding, prototype.embedding)
        for person_name, prototype in prototypes.items()
    }
    best_match_name = max(scores, key=scores.get)
    best_similarity = scores[best_match_name]

    if best_similarity < threshold:
        return "unknown", best_match_name, best_similarity

    return best_match_name, best_match_name, best_similarity


def choose_people(
    targets: np.ndarray,
    target_names: np.ndarray,
    config: Config,
) -> tuple[list[int], list[int], dict[int, list[int]]]:
    person_to_indices: dict[int, list[int]] = {}
    for image_index, target in enumerate(targets):
        person_to_indices.setdefault(int(target), []).append(image_index)

    # 登録画像 + 最低1枚のテスト画像を確保できる人物だけ使う。
    required_images = config.enroll_images + 1
    eligible = [
        person_id
        for person_id, indices in person_to_indices.items()
        if len(indices) >= required_images
    ]

    rng = random.Random(config.seed)
    rng.shuffle(eligible)

    required_people = config.registered_people + config.unknown_people
    if len(eligible) < required_people:
        raise ValueError(
            f"Not enough eligible identities: need {required_people}, got {len(eligible)}. "
            "Reduce --registered-people / --unknown-people or --enroll-images."
        )

    registered_ids = eligible[: config.registered_people]
    unknown_ids = eligible[
        config.registered_people : config.registered_people + config.unknown_people
    ]

    return registered_ids, unknown_ids, person_to_indices


def evaluate(config: Config) -> list[TestResult]:
    random.seed(config.seed)
    np.random.seed(config.seed)

    # 全体画像(250x250)を取得する。デフォルトの顔部分だけの切り出しより、
    # InsightFaceの検出器にそのまま入力しやすい。
    lfw = fetch_lfw_people(
        data_home=str(config.data_home) if config.data_home else None,
        funneled=True,
        resize=1.0,
        min_faces_per_person=config.min_faces_per_person,
        color=True,
        slice_=(slice(0, 250), slice(0, 250)),
        download_if_missing=True,
    )

    print(f"LFW images: {len(lfw.images)}")
    print(f"LFW identities: {len(lfw.target_names)}")

    registered_ids, unknown_ids, person_to_indices = choose_people(
        lfw.target,
        lfw.target_names,
        config,
    )

    app = build_face_analyzer(config.model_name)
    rng = random.Random(config.seed)

    prototypes: dict[str, Prototype] = {}
    registered_test_items: list[tuple[str, int]] = []
    unknown_test_items: list[tuple[str, int]] = []

    print("\n=== Enrollment ===")
    for person_id in registered_ids:
        person_name = str(lfw.target_names[person_id])
        indices = list(person_to_indices[person_id])
        rng.shuffle(indices)

        enroll_indices = indices[: config.enroll_images]
        test_indices = indices[config.enroll_images :]
        if config.max_test_images_per_person > 0:
            test_indices = test_indices[: config.max_test_images_per_person]

        embeddings: list[np.ndarray] = []
        failed = 0
        for image_index in enroll_indices:
            try:
                embeddings.append(extract_embedding(app, lfw.images[image_index]))
            except ValueError as exc:
                failed += 1
                print(f"  enrollment detection failed: {person_name}: {exc}")

        if not embeddings:
            print(f"  SKIP {person_name}: no enrollment face was detected")
            continue

        prototypes[person_name] = Prototype(
            person_name=person_name,
            embedding=build_prototype(embeddings),
        )

        registered_test_items.extend((person_name, idx) for idx in test_indices)
        print(
            f"  {person_name}: enrolled={len(embeddings)}, "
            f"failed={failed}, test={len(test_indices)}"
        )

    print("\n=== Unknown identities ===")
    for person_id in unknown_ids:
        person_name = str(lfw.target_names[person_id])
        indices = list(person_to_indices[person_id])
        rng.shuffle(indices)

        if config.max_test_images_per_person > 0:
            indices = indices[: config.max_test_images_per_person]

        unknown_test_items.extend((person_name, idx) for idx in indices)
        print(f"  {person_name}: test={len(indices)}")

    print(f"\nRegistered prototypes: {len(prototypes)}")
    print(f"Threshold: {config.threshold:.3f}")

    results: list[TestResult] = []

    def run_one(true_name: str, image_index: int, is_registered: bool) -> None:
        try:
            embedding = extract_embedding(app, lfw.images[image_index])
        except ValueError as exc:
            print(f"  test detection failed: {true_name}: {exc}")
            return

        predicted_name, best_match_name, similarity = recognize(
            embedding,
            prototypes,
            config.threshold,
        )

        if is_registered:
            correct = predicted_name == true_name
        else:
            correct = predicted_name == "unknown"

        results.append(
            TestResult(
                true_name=true_name,
                predicted_name=predicted_name,
                best_match_name=best_match_name,
                similarity=similarity,
                is_registered=is_registered,
                correct=correct,
            )
        )

    print("\n=== Recognition test ===")
    for true_name, image_index in registered_test_items:
        # enrollment失敗で登録されなかった人物は評価対象から外す。
        if true_name in prototypes:
            run_one(true_name, image_index, True)

    for true_name, image_index in unknown_test_items:
        run_one(true_name, image_index, False)

    return results


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    return float(np.percentile(np.asarray(values, dtype=np.float32), q))


def print_summary(results: list[TestResult]) -> None:
    registered = [result for result in results if result.is_registered]
    unknown = [result for result in results if not result.is_registered]

    registered_correct = sum(result.correct for result in registered)
    unknown_correct = sum(result.correct for result in unknown)
    all_correct = sum(result.correct for result in results)

    registered_accepted = [
        result for result in registered if result.predicted_name != "unknown"
    ]
    registered_identity_correct = sum(
        result.predicted_name == result.true_name for result in registered
    )
    false_accepts = [result for result in unknown if result.predicted_name != "unknown"]

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

    genuine_scores = [result.similarity for result in registered]
    impostor_scores = [result.similarity for result in unknown]

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
    """同じ評価データ上で最良閾値を探す。参考値であり正式な評価値ではない。"""
    if not results:
        return None

    best_threshold = 0.0
    best_accuracy = -1.0

    for threshold in np.linspace(-0.10, 0.90, 201):
        correct = 0
        for result in results:
            if result.similarity < threshold:
                prediction = "unknown"
            else:
                prediction = result.best_match_name

            expected = result.true_name if result.is_registered else "unknown"
            correct += prediction == expected

        accuracy = correct / len(results)
        if accuracy > best_accuracy:
            best_accuracy = accuracy
            best_threshold = float(threshold)

    return best_threshold, best_accuracy


def save_results(results: list[TestResult], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "lfw_results.csv"

    with output_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
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
                    result.true_name,
                    result.predicted_name,
                    result.best_match_name,
                    f"{result.similarity:.6f}",
                    int(result.is_registered),
                    int(result.correct),
                ]
            )

    return output_path


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description="LFW + InsightFace prototype-based face recognition test"
    )
    parser.add_argument("--registered-people", type=int, default=10)
    parser.add_argument("--unknown-people", type=int, default=10)
    parser.add_argument("--enroll-images", type=int, default=3)
    parser.add_argument("--max-test-images-per-person", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=0.50)
    parser.add_argument("--min-faces-per-person", type=int, default=8)
    parser.add_argument("--model", default="buffalo_l")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-dir", type=Path, default=Path("./lfw_output"))
    parser.add_argument("--data-home", type=Path, default=None)
    args = parser.parse_args()

    if args.registered_people < 1:
        parser.error("--registered-people must be >= 1")
    if args.unknown_people < 1:
        parser.error("--unknown-people must be >= 1")
    if args.enroll_images < 1:
        parser.error("--enroll-images must be >= 1")
    if args.max_test_images_per_person == 0 or args.max_test_images_per_person < -1:
        parser.error("--max-test-images-per-person must be -1 or >= 1")

    return Config(
        registered_people=args.registered_people,
        unknown_people=args.unknown_people,
        enroll_images=args.enroll_images,
        max_test_images_per_person=args.max_test_images_per_person,
        threshold=args.threshold,
        min_faces_per_person=max(
            args.min_faces_per_person,
            args.enroll_images + 1,
        ),
        model_name=args.model,
        seed=args.seed,
        output_dir=args.output_dir,
        data_home=args.data_home,
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
    print(f"\nCSV saved to: {output_path.resolve()}")


if __name__ == "__main__":
    main()
