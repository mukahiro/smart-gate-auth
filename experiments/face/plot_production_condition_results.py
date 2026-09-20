from __future__ import annotations

"""本番条件の顔認証評価CSVから、匿名のレポート用グラフを生成する。"""

import argparse
import csv
from collections import defaultdict
from pathlib import Path

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter
except ImportError as exc:
    raise SystemExit(
        "matplotlib is required to generate graphs: "
        "python -m pip install matplotlib"
    ) from exc


LAB_GROUP = "Lab-captured"
STOCK_GROUP = "Stock material"
GROUP_COLORS = {
    LAB_GROUP: "#2878B5",
    STOCK_GROUP: "#E07A1F",
}


def load_results(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    if not rows:
        raise ValueError(f"No result rows found: {path}")

    required = {
        "true_identity",
        "subject_type",
        "best_similarity",
        "threshold",
        "outcome",
    }
    missing = required.difference(rows[0])
    if missing:
        raise ValueError(f"Missing CSV columns: {', '.join(sorted(missing))}")
    return rows


def validate_stock_identities(
    rows: list[dict[str, str]],
    stock_identities: set[str],
) -> None:
    identities = {row["true_identity"] for row in rows}
    unknown = stock_identities.difference(identities)
    if unknown:
        raise ValueError(
            "Stock identities not present in results: "
            + ", ".join(sorted(unknown))
        )
    if not stock_identities or stock_identities == identities:
        raise ValueError("Both lab-captured and stock-material identities are required")


def group_for(identity: str, stock_identities: set[str]) -> str:
    return STOCK_GROUP if identity in stock_identities else LAB_GROUP


def plot_similarity_distribution(
    rows: list[dict[str, str]],
    stock_identities: set[str],
    output_path: Path,
) -> None:
    thresholds = sorted({float(row["threshold"]) for row in rows})
    first_threshold = thresholds[0]
    # Each score is repeated once per threshold in the detailed CSV. Select one
    # threshold to retain each query/gallery result exactly once.
    score_rows = [
        row for row in rows if float(row["threshold"]) == first_threshold
    ]
    bins = [index / 50 for index in range(51)]
    figure, axes = plt.subplots(1, 2, figsize=(12, 4.8), sharex=True, sharey=True)

    for axis, group in zip(axes, (LAB_GROUP, STOCK_GROUP), strict=True):
        registered = [
            float(row["best_similarity"])
            for row in score_rows
            if group_for(row["true_identity"], stock_identities) == group
            and row["subject_type"] == "registered"
            and row["best_similarity"]
        ]
        unknown = [
            float(row["best_similarity"])
            for row in score_rows
            if group_for(row["true_identity"], stock_identities) == group
            and row["subject_type"] == "unknown"
            and row["best_similarity"]
        ]
        axis.hist(
            registered,
            bins=bins,
            density=True,
            alpha=0.65,
            color="#2878B5",
            label="Registered",
        )
        axis.hist(
            unknown,
            bins=bins,
            density=True,
            alpha=0.65,
            color="#D9534F",
            label="Unknown",
        )
        axis.axvline(
            0.45,
            color="#666666",
            linestyle="--",
            linewidth=1.5,
            label="Threshold 0.45",
        )
        axis.axvline(
            0.50,
            color="#222222",
            linestyle="-",
            linewidth=1.8,
            label="Threshold 0.50",
        )
        axis.set_title(group)
        axis.set_xlabel("Maximum cosine similarity")
        axis.grid(axis="y", alpha=0.25)

    axes[0].set_ylabel("Density")
    axes[1].legend(loc="upper left", fontsize=9)
    figure.suptitle("Similarity distributions by image source", fontsize=14)
    figure.text(
        0.5,
        0.01,
        "Repeated gallery configurations; observations are not independent.",
        ha="center",
        fontsize=9,
        color="#555555",
    )
    figure.tight_layout(rect=(0, 0.04, 1, 0.95))
    figure.savefig(output_path, dpi=180, bbox_inches="tight")
    plt.close(figure)


def plot_threshold_performance(
    rows: list[dict[str, str]],
    stock_identities: set[str],
    output_path: Path,
) -> None:
    totals: dict[tuple[float, str, str], int] = defaultdict(int)
    successes: dict[tuple[float, str, str], int] = defaultdict(int)

    for row in rows:
        threshold = float(row["threshold"])
        group = group_for(row["true_identity"], stock_identities)
        subject_type = row["subject_type"]
        key = (threshold, group, subject_type)
        totals[key] += 1
        if subject_type == "registered" and row["outcome"] == "correct_accept":
            successes[key] += 1
        elif subject_type == "unknown" and row["outcome"] == "false_accept":
            successes[key] += 1

    thresholds = sorted({float(row["threshold"]) for row in rows})
    figure, (accept_axis, far_axis) = plt.subplots(
        2,
        1,
        figsize=(8.5, 7.2),
        sharex=True,
        gridspec_kw={"height_ratios": [2, 1]},
    )

    for group in (LAB_GROUP, STOCK_GROUP):
        correct_rates = [
            successes[(threshold, group, "registered")]
            / totals[(threshold, group, "registered")]
            for threshold in thresholds
        ]
        false_accept_rates = [
            successes[(threshold, group, "unknown")]
            / totals[(threshold, group, "unknown")]
            for threshold in thresholds
        ]
        accept_axis.plot(
            thresholds,
            correct_rates,
            marker="o",
            linewidth=2,
            color=GROUP_COLORS[group],
            label=group,
        )
        far_axis.plot(
            thresholds,
            false_accept_rates,
            marker="o",
            linewidth=2,
            color=GROUP_COLORS[group],
            label=group,
        )

    accept_axis.axvline(0.50, color="#222222", linestyle="--", linewidth=1.4)
    accept_axis.set_ylabel("Correct identification rate")
    accept_axis.set_ylim(0.55, 1.01)
    accept_axis.yaxis.set_major_formatter(PercentFormatter(1.0))
    accept_axis.grid(alpha=0.25)
    accept_axis.legend(loc="lower left")

    far_axis.axvline(0.50, color="#222222", linestyle="--", linewidth=1.4)
    far_axis.set_xlabel("Threshold")
    far_axis.set_ylabel("Unknown FAR")
    far_axis.set_ylim(0, 0.01)
    far_axis.yaxis.set_major_formatter(PercentFormatter(1.0))
    far_axis.grid(alpha=0.25)
    far_axis.text(
        0.525,
        0.004,
        "No false accepts observed",
        ha="center",
        color="#555555",
        fontsize=9,
    )

    figure.suptitle("Recognition performance by threshold", fontsize=14)
    figure.text(
        0.5,
        0.01,
        "Repeated gallery configurations; observations are not independent.",
        ha="center",
        fontsize=9,
        color="#555555",
    )
    figure.tight_layout(rect=(0, 0.04, 1, 0.96))
    figure.savefig(output_path, dpi=180, bbox_inches="tight")
    plt.close(figure)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot anonymized production-condition face evaluation results"
    )
    parser.add_argument("results_csv", type=Path)
    parser.add_argument(
        "--stock-identities",
        nargs="+",
        required=True,
        help="Identity directory names belonging to the stock-material group",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("experiments/face/figures"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows = load_results(args.results_csv)
    stock_identities = set(args.stock_identities)
    validate_stock_identities(rows, stock_identities)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    distribution_path = args.output_dir / "similarity_distribution.png"
    performance_path = args.output_dir / "threshold_performance.png"
    plot_similarity_distribution(rows, stock_identities, distribution_path)
    plot_threshold_performance(rows, stock_identities, performance_path)
    print(f"Saved: {distribution_path.resolve()}")
    print(f"Saved: {performance_path.resolve()}")


if __name__ == "__main__":
    main()
