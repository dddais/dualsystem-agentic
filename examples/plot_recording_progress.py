"""Plot a manual_bridge recording export: pip install matplotlib."""

import argparse
import csv
import math
from pathlib import Path


def finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording-dir", type=Path, required=True, help="Directory containing progress.csv")
    parser.add_argument("--output", type=Path, help="Defaults to <recording-dir>/progress.png; SVG/PDF also supported")
    parser.add_argument("--time-basis", choices=["publication", "observation"], default="publication")
    args = parser.parse_args()
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    with (args.recording_dir / "progress.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    groups = {}
    for row in rows:
        groups.setdefault(row["monitor_id"], []).append(row)
    time_key = "elapsed_s" if args.time_basis == "publication" else "observation_elapsed_s"
    fig, (progress, scores) = plt.subplots(2, 1, figsize=(11, 7), sharex=True, layout="constrained")
    colors = plt.get_cmap("tab10")

    def series(axis, values, key, label, color, style="-"):
        points = sorted((x, y) for row in values
                        if (x := finite(row.get(time_key))) is not None and (y := finite(row.get(key))) is not None)
        if points:
            axis.plot(*zip(*points), marker=".", markersize=6, label=label, color=color, linestyle=style)

    for index, (mid, values) in enumerate(groups.items()):
        label, color = f"Task {index + 1} ({mid[-8:]})", colors(index % 10)
        series(progress, values, "progress", label + " GRM", color)
        series(progress, values, "baseline_progress", label + " baseline", color, "--")
        for mode, style in (("forward", "-"), ("incremental", "--"), ("backward", ":")):
            series(scores, values, mode + "_score", f"Task {index + 1} {mode}", color, style)
    for axis in (progress, scores):
        axis.grid(alpha=.25)
        if axis.lines:
            axis.legend(fontsize=8, loc="best")
        else:
            axis.text(.5, .5, "No GRM scores in this recording window", ha="center", transform=axis.transAxes)
    progress.set(ylabel="Fused progress (0–1)", ylim=(-.03, 1.03), title=args.recording_dir.name)
    scores.set(ylabel="Raw mode score", xlabel=f"Seconds from recording start ({args.time_basis} time)")
    output = args.output or args.recording_dir / "progress.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=180)
    plt.close(fig)
    print(output.resolve())


if __name__ == "__main__":
    main()
