from __future__ import annotations

"""本番と同じ顔照合条件で、少人数データを交差検証する。

データセットの各サブディレクトリを1人として扱う。6人から1人を未登録者
として外し、残りの人物では指定枚数を登録、残りをテストに使用する。
"""

import argparse
import csv
import json
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

import cv2
import numpy as np
from insightface.app import FaceAnalysis

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
PRODUCTION_MODEL = "buffalo_sc"
PRODUCTION_DET_SIZE = 320
DEFAULT_THRESHOLDS = (0.40, 0.45, 0.50, 0.55, 0.60, 0.65)


@dataclass(frozen=True)
class Config:
    dataset_dir: Path
    enroll_images: int
    thresholds: tuple[float, ...]
    model_name: str
    det_size: int
    output_dir: Path


@dataclass(frozen=True)
class Sample:
    identity: str
    path: Path
    embedding: np.ndarray | None
    detection_status: str


@dataclass(frozen=True)
class Result:
    unknown_identity: str
    split: int
    subject_type: str
    true_identity: str
    image_path: Path
    detection_status: str
    best_match_identity: str | None
    best_similarity: float | None


@dataclass(frozen=True)
class SkippedSplit:
    unknown_identity: str
    split: int
    registered_identity: str
    image_path: Path
    detection_status: str


def normalize(vector: np.ndarray) -> np.ndarray:
    """本番と同じfloat32変換とL2正規化を行う。"""
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-12:
        raise ValueError("face model returned a zero embedding")
    return vector / norm


def find_best_match(
    identities: list[str],
    embeddings: np.ndarray,
    query: np.ndarray,
) -> tuple[str, float]:
    """本番と同じく全登録Embeddingの内積が最大の登録行を選ぶ。"""
    query = normalize(query)
    if query.size != embeddings.shape[1]:
        raise ValueError("query and registered embedding dimensions do not match")
    scores = embeddings @ query
    best = int(np.argmax(scores))
    return identities[best], float(scores[best])


def list_images(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def load_dataset_paths(dataset_dir: Path, enroll_images: int) -> dict[str, list[Path]]:
    if not dataset_dir.is_dir():
        raise ValueError(f"Dataset directory does not exist: {dataset_dir}")

    dataset: dict[str, list[Path]] = {}
    for identity_dir in sorted(path for path in dataset_dir.iterdir() if path.is_dir()):
        images = list_images(identity_dir)
        if not images:
            continue
        if len(images) <= enroll_images:
            raise ValueError(
                f"{identity_dir.name} needs at least {enroll_images + 1} images; "
                f"found {len(images)}"
            )
        dataset[identity_dir.name] = images

    if len(dataset) < 3:
        raise ValueError("At least three identities are required")
    return dataset


def build_analyzer(model_name: str, det_size: int) -> FaceAnalysis:
    analyzer = FaceAnalysis(
        name=model_name,
        providers=["CPUExecutionProvider"],
    )
    analyzer.prepare(ctx_id=-1, det_size=(det_size, det_size))
    return analyzer


def load_image_bgr(path: Path) -> np.ndarray:
    encoded = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("image_decode_failed")
    return image


def extract_sample(analyzer: FaceAnalysis, identity: str, path: Path) -> Sample:
    try:
        image = load_image_bgr(path)
    except ValueError as exc:
        return Sample(identity, path, None, str(exc))

    faces = analyzer.get(image)
    if len(faces) == 0:
        return Sample(identity, path, None, "no_face")
    if len(faces) > 1:
        # 本番は最大の顔を選ばず、顔が1人になるまでこのフレームを見送る。
        return Sample(identity, path, None, "multiple_faces")

    embedding = getattr(faces[0], "normed_embedding", None)
    if embedding is None:
        embedding = faces[0].embedding
    return Sample(identity, path, normalize(embedding), "ok")


def extract_dataset(
    analyzer: FaceAnalysis,
    dataset_paths: dict[str, list[Path]],
) -> dict[str, list[Sample]]:
    dataset: dict[str, list[Sample]] = {}
    total = sum(len(paths) for paths in dataset_paths.values())
    current = 0
    for identity, paths in dataset_paths.items():
        dataset[identity] = []
        for path in paths:
            current += 1
            print(f"[{current}/{total}] {identity}/{path.name}", flush=True)
            sample = extract_sample(analyzer, identity, path)
            dataset[identity].append(sample)
            if sample.detection_status != "ok":
                print(f"  detection skipped: {sample.detection_status}")
    return dataset


def evaluate(
    dataset: dict[str, list[Sample]],
    enroll_images: int,
) -> tuple[list[Result], list[SkippedSplit]]:
    identities = sorted(dataset)
    split_plans = {
        identity: list(combinations(range(len(samples)), enroll_images))
        for identity, samples in dataset.items()
    }
    results: list[Result] = []
    skipped: list[SkippedSplit] = []

    for unknown_identity in identities:
        registered_identities = [item for item in identities if item != unknown_identity]
        # 画像枚数が異なる場合も全人物の組合せを一巡できるよう、短い側は循環する。
        split_count = max(len(split_plans[item]) for item in registered_identities)
        print(
            f"Evaluating unknown={unknown_identity}: "
            f"registered={len(registered_identities)}, splits={split_count}"
        )

        for split_index in range(split_count):
            selected: dict[str, tuple[int, ...]] = {
                identity: split_plans[identity][split_index % len(split_plans[identity])]
                for identity in registered_identities
            }
            invalid_enrollment: list[tuple[str, Sample]] = []
            gallery_identities: list[str] = []
            gallery_embeddings: list[np.ndarray] = []

            for identity in registered_identities:
                for image_index in selected[identity]:
                    sample = dataset[identity][image_index]
                    if sample.embedding is None:
                        invalid_enrollment.append((identity, sample))
                    else:
                        gallery_identities.append(identity)
                        gallery_embeddings.append(sample.embedding)

            # 登録APIは選択された画像が1枚でも不正なら、その人物の登録を更新しない。
            # 不完全なgalleryを評価に使わず、該当splitを明示的に記録して除外する。
            if invalid_enrollment:
                skipped.extend(
                    SkippedSplit(
                        unknown_identity=unknown_identity,
                        split=split_index + 1,
                        registered_identity=identity,
                        image_path=sample.path,
                        detection_status=sample.detection_status,
                    )
                    for identity, sample in invalid_enrollment
                )
                continue

            gallery = np.vstack(gallery_embeddings).astype(np.float32, copy=False)

            def run_sample(sample: Sample, subject_type: str) -> None:
                best_identity: str | None = None
                best_similarity: float | None = None
                if sample.embedding is not None:
                    best_identity, best_similarity = find_best_match(
                        gallery_identities,
                        gallery,
                        sample.embedding,
                    )
                results.append(
                    Result(
                        unknown_identity=unknown_identity,
                        split=split_index + 1,
                        subject_type=subject_type,
                        true_identity=sample.identity,
                        image_path=sample.path,
                        detection_status=sample.detection_status,
                        best_match_identity=best_identity,
                        best_similarity=best_similarity,
                    )
                )

            for identity in registered_identities:
                enrollment_indices = set(selected[identity])
                for image_index, sample in enumerate(dataset[identity]):
                    if image_index not in enrollment_indices:
                        run_sample(sample, "registered")

            for sample in dataset[unknown_identity]:
                run_sample(sample, "unknown")

    if not results:
        raise ValueError("No valid gallery split could be evaluated")
    return results, skipped


def classify(result: Result, threshold: float) -> tuple[str, str]:
    predicted = "unknown"
    if result.best_similarity is not None and result.best_similarity >= threshold:
        predicted = result.best_match_identity or "unknown"

    if result.subject_type == "unknown":
        outcome = "correct_reject" if predicted == "unknown" else "false_accept"
    elif predicted == result.true_identity:
        outcome = "correct_accept"
    elif predicted == "unknown":
        outcome = "false_reject"
    else:
        outcome = "misidentification"
    return predicted, outcome


def save_results(
    results: list[Result],
    thresholds: tuple[float, ...],
    output_dir: Path,
) -> Path:
    path = output_dir / "production_condition_results.csv"
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "unknown_identity",
                "split",
                "subject_type",
                "true_identity",
                "image_path",
                "detection_status",
                "best_match_identity",
                "best_similarity",
                "threshold",
                "predicted_identity",
                "outcome",
            ]
        )
        for result in results:
            for threshold in thresholds:
                predicted, outcome = classify(result, threshold)
                writer.writerow(
                    [
                        result.unknown_identity,
                        result.split,
                        result.subject_type,
                        result.true_identity,
                        result.image_path,
                        result.detection_status,
                        result.best_match_identity or "",
                        (
                            ""
                            if result.best_similarity is None
                            else f"{result.best_similarity:.6f}"
                        ),
                        f"{threshold:.3f}",
                        predicted,
                        outcome,
                    ]
                )
    return path


def summarize(results: list[Result], threshold: float) -> dict[str, int | float]:
    outcomes = [classify(result, threshold)[1] for result in results]
    registered_total = sum(result.subject_type == "registered" for result in results)
    unknown_total = sum(result.subject_type == "unknown" for result in results)
    correct_accept = outcomes.count("correct_accept")
    false_reject = outcomes.count("false_reject")
    misidentification = outcomes.count("misidentification")
    false_accept = outcomes.count("false_accept")
    correct_reject = outcomes.count("correct_reject")
    detection_skips = sum(result.detection_status != "ok" for result in results)
    return {
        "threshold": threshold,
        "registered_total": registered_total,
        "correct_accept": correct_accept,
        "false_reject": false_reject,
        "misidentification": misidentification,
        "registered_correct_rate": (
            correct_accept / registered_total if registered_total else 0.0
        ),
        "unknown_total": unknown_total,
        "correct_reject": correct_reject,
        "false_accept": false_accept,
        "unknown_false_accept_rate": (
            false_accept / unknown_total if unknown_total else 0.0
        ),
        "detection_skips": detection_skips,
    }


def save_summary(summaries: list[dict[str, int | float]], output_dir: Path) -> Path:
    path = output_dir / "production_condition_summary.csv"
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)
    return path


def save_skipped_splits(skipped: list[SkippedSplit], output_dir: Path) -> Path | None:
    if not skipped:
        return None
    path = output_dir / "production_condition_skipped_splits.csv"
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "unknown_identity",
                "split",
                "registered_identity",
                "image_path",
                "detection_status",
            ]
        )
        for item in skipped:
            writer.writerow(
                [
                    item.unknown_identity,
                    item.split,
                    item.registered_identity,
                    item.image_path,
                    item.detection_status,
                ]
            )
    return path


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description="Production-condition leave-one-identity-out face recognition test"
    )
    parser.add_argument("dataset_dir", type=Path)
    parser.add_argument("--enroll-images", type=int, default=3)
    parser.add_argument("--thresholds", type=float, nargs="+", default=DEFAULT_THRESHOLDS)
    parser.add_argument("--model", default=PRODUCTION_MODEL)
    parser.add_argument("--det-size", type=int, default=PRODUCTION_DET_SIZE)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./production_condition_face_output"),
    )
    args = parser.parse_args()

    if args.enroll_images < 1:
        parser.error("--enroll-images must be positive")
    if args.det_size < 64:
        parser.error("--det-size must be at least 64")
    if not args.thresholds or any(not -1.0 <= value <= 1.0 for value in args.thresholds):
        parser.error("--thresholds must be between -1 and 1")

    return Config(
        dataset_dir=args.dataset_dir,
        enroll_images=args.enroll_images,
        thresholds=tuple(sorted(set(args.thresholds))),
        model_name=args.model,
        det_size=args.det_size,
        output_dir=args.output_dir,
    )


def main() -> None:
    config = parse_args()
    dataset_paths = load_dataset_paths(config.dataset_dir, config.enroll_images)
    print(
        f"Model={config.model_name}, det_size={config.det_size}, "
        f"identities={len(dataset_paths)}, enroll_images={config.enroll_images}"
    )
    analyzer = build_analyzer(config.model_name, config.det_size)
    dataset = extract_dataset(analyzer, dataset_paths)
    results, skipped = evaluate(dataset, config.enroll_images)

    config.output_dir.mkdir(parents=True, exist_ok=True)
    summaries = [summarize(results, threshold) for threshold in config.thresholds]
    result_path = save_results(results, config.thresholds, config.output_dir)
    summary_path = save_summary(summaries, config.output_dir)
    skipped_path = save_skipped_splits(skipped, config.output_dir)
    metadata_path = config.output_dir / "production_condition_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "dataset_dir": str(config.dataset_dir.resolve()),
                "model": config.model_name,
                "det_size": config.det_size,
                "enroll_images": config.enroll_images,
                "thresholds": config.thresholds,
                "identity_count": len(dataset),
                "image_count": sum(len(samples) for samples in dataset.values()),
                "evaluated_rows_before_threshold_expansion": len(results),
                "skipped_split_rows": len(skipped),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print("\n=== Summary ===")
    for summary in summaries:
        print(
            f"threshold={summary['threshold']:.3f} | "
            f"registered correct={summary['registered_correct_rate']:.2%} "
            f"({summary['correct_accept']}/{summary['registered_total']}), "
            f"false reject={summary['false_reject']}, "
            f"misidentification={summary['misidentification']} | "
            f"unknown FAR={summary['unknown_false_accept_rate']:.2%} "
            f"({summary['false_accept']}/{summary['unknown_total']})"
        )
    print(f"\nDetailed results: {result_path.resolve()}")
    print(f"Summary: {summary_path.resolve()}")
    print(f"Metadata: {metadata_path.resolve()}")
    if skipped_path is not None:
        print(f"Skipped enrollment splits: {skipped_path.resolve()}")
    print(
        "Note: repeated image combinations are correlated; "
        "do not treat all CSV rows as independent observations."
    )


if __name__ == "__main__":
    main()
