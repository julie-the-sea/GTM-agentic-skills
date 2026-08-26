#!/usr/bin/env python3
"""
segmentation_analyzer.py

Real-computation backend for the Segmentation Analyzer workflow (see
README.md for the full 8-step agent flow this supports). This file is
agent-agnostic: any AI agent with Python code execution -- Claude, GPT,
or otherwise -- can shell out to it, and a human can run it directly too.

It provides two subcommands, corresponding to the two steps in the
workflow that must use real computation instead of an agent's reasoning
or estimation:

  sufficiency   Checks whether a dataset has enough data to support a
                specific segmentation question (group sizes, sample-to-
                feature ratio, time-window coverage, missingness).

  cluster       Runs K-means clustering with a real stability/validation
                check (silhouette score + variance explained), so an
                agent can state concretely whether resulting segments
                hold up or are directional-only.

Examples:

  python segmentation_analyzer.py sufficiency \\
      --data accounts.csv \\
      --group-col channel --min-per-group 30 \\
      --feature-cols employees,engagement_score --min-samples-per-feature 15 \\
      --date-col last_engagement_date --min-window-days 90

  python segmentation_analyzer.py cluster \\
      --data accounts.csv \\
      --id-col account_id \\
      --numeric-cols employees,engagement_score,content_downloads \\
      --categorical-cols industry \\
      --out-assignments cluster_assignments.csv \\
      --out-summary cluster_summary.json

Requires: pandas, numpy, scikit-learn (pip install pandas numpy scikit-learn)
"""
import argparse
import json
import sys

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------
# sufficiency subcommand
# --------------------------------------------------------------------------

def check_group_sizes(df: pd.DataFrame, group_col: str, min_per_group: int) -> dict:
    counts = df.groupby(group_col).size().sort_values(ascending=False)
    groups = {str(k): int(v) for k, v in counts.items()}
    undersized = {k: v for k, v in groups.items() if v < min_per_group}
    return {
        "check": "group_sizes",
        "group_col": group_col,
        "min_required_per_group": min_per_group,
        "observed_counts": groups,
        "undersized_groups": undersized,
        "passed": len(undersized) == 0,
    }


def check_feature_ratio(df: pd.DataFrame, feature_cols: list, min_samples_per_feature: int) -> dict:
    n = len(df)
    n_features = len(feature_cols)
    required_n = n_features * min_samples_per_feature
    return {
        "check": "sample_to_feature_ratio",
        "feature_cols": feature_cols,
        "n_features": n_features,
        "min_samples_per_feature": min_samples_per_feature,
        "required_total_samples": required_n,
        "observed_total_samples": int(n),
        "passed": n >= required_n,
    }


def check_time_window(df: pd.DataFrame, date_col: str, min_window_days: int) -> dict:
    dates = pd.to_datetime(df[date_col], errors="coerce").dropna()
    if dates.empty:
        return {
            "check": "time_window",
            "date_col": date_col,
            "min_window_days": min_window_days,
            "observed_window_days": 0,
            "passed": False,
            "note": "No parseable dates found in date_col.",
        }
    span_days = int((dates.max() - dates.min()).days)
    return {
        "check": "time_window",
        "date_col": date_col,
        "min_window_days": min_window_days,
        "observed_window_days": span_days,
        "earliest": str(dates.min().date()),
        "latest": str(dates.max().date()),
        "passed": span_days >= min_window_days,
    }


def check_missingness(df: pd.DataFrame, columns: list, max_missing_pct: float) -> dict:
    results = {}
    for col in columns:
        if col not in df.columns:
            results[col] = {"error": "column not found"}
            continue
        pct_missing = float(df[col].isna().mean() * 100)
        results[col] = {"pct_missing": round(pct_missing, 1), "passed": pct_missing <= max_missing_pct}
    all_passed = all(v.get("passed", False) for v in results.values())
    return {
        "check": "missingness",
        "max_missing_pct_allowed": max_missing_pct,
        "columns": results,
        "passed": all_passed,
    }


def run_sufficiency(args) -> int:
    df = pd.read_csv(args.data)

    checks = []
    if args.group_col:
        checks.append(check_group_sizes(df, args.group_col, args.min_per_group))
    if args.feature_cols:
        feature_cols = [c.strip() for c in args.feature_cols.split(",")]
        checks.append(check_feature_ratio(df, feature_cols, args.min_samples_per_feature))
    if args.date_col:
        checks.append(check_time_window(df, args.date_col, args.min_window_days))
    if args.missingness_cols:
        cols = [c.strip() for c in args.missingness_cols.split(",")]
        checks.append(check_missingness(df, cols, args.max_missing_pct))

    if not checks:
        print("No checks requested. Pass at least one of --group-col, --feature-cols, --date-col, --missingness-cols.", file=sys.stderr)
        return 2

    result = {
        "data_file": args.data,
        "n_rows": int(len(df)),
        "n_columns": int(len(df.columns)),
        "checks": checks,
        "all_passed": all(c["passed"] for c in checks),
    }

    output = json.dumps(result, indent=2)
    print(output)
    if args.out:
        with open(args.out, "w") as f:
            f.write(output)

    return 0 if result["all_passed"] else 1


# --------------------------------------------------------------------------
# cluster subcommand
# --------------------------------------------------------------------------

def stability_label(score: float) -> str:
    if score >= 0.5:
        return "strong"
    if score >= 0.25:
        return "moderate"
    return "weak"


def build_feature_matrix(df: pd.DataFrame, numeric_cols: list, categorical_cols: list):
    from sklearn.compose import ColumnTransformer
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    transformers = []
    if numeric_cols:
        transformers.append(("num", StandardScaler(), numeric_cols))
    if categorical_cols:
        transformers.append(("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), categorical_cols))
    ct = ColumnTransformer(transformers)
    return ct.fit_transform(df[numeric_cols + categorical_cols])


def between_ss_ratio(X: np.ndarray, labels: np.ndarray, centers: np.ndarray) -> float:
    """Fraction of total variance explained by the clustering (higher = stronger split)."""
    overall_mean = X.mean(axis=0)
    total_ss = np.sum((X - overall_mean) ** 2)
    between_ss = 0.0
    for k in range(centers.shape[0]):
        n_k = np.sum(labels == k)
        between_ss += n_k * np.sum((centers[k] - overall_mean) ** 2)
    if total_ss == 0:
        return 0.0
    return float(between_ss / total_ss)


def run_cluster(args) -> int:
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score

    numeric_cols = [c.strip() for c in args.numeric_cols.split(",") if c.strip()]
    categorical_cols = [c.strip() for c in args.categorical_cols.split(",") if c.strip()]
    if not numeric_cols and not categorical_cols:
        print("Provide at least one of --numeric-cols or --categorical-cols.", file=sys.stderr)
        return 2

    df = pd.read_csv(args.data)
    feature_df = df.dropna(subset=numeric_cols + categorical_cols).copy()
    dropped = len(df) - len(feature_df)

    X = build_feature_matrix(feature_df, numeric_cols, categorical_cols)

    n_samples = X.shape[0]
    max_k = min(args.k_max, n_samples - 1)
    if max_k < args.k_min:
        print(f"Not enough rows ({n_samples}) after dropping missing values to try even k={args.k_min}.", file=sys.stderr)
        return 2

    results = []
    for k in range(args.k_min, max_k + 1):
        km = KMeans(n_clusters=k, random_state=args.random_state, n_init=10)
        labels = km.fit_predict(X)
        sil = float(silhouette_score(X, labels)) if k > 1 and len(set(labels)) > 1 else float("nan")
        variance_explained = between_ss_ratio(X, labels, km.cluster_centers_)
        results.append({
            "k": k,
            "silhouette_score": round(sil, 4),
            "variance_explained": round(variance_explained, 4),
            "labels": labels,
        })

    best = max(results, key=lambda r: r["silhouette_score"])
    feature_df["cluster"] = best["labels"]

    cluster_profiles = {}
    for cluster_id in sorted(feature_df["cluster"].unique()):
        subset = feature_df[feature_df["cluster"] == cluster_id]
        profile = {"n_members": int(len(subset)), "pct_of_total": round(len(subset) / len(feature_df) * 100, 1)}
        for col in numeric_cols:
            profile[col] = {
                "mean": round(float(subset[col].mean()), 2),
                "median": round(float(subset[col].median()), 2),
                "std": round(float(subset[col].std()), 2),
            }
        for col in categorical_cols:
            top_values = subset[col].value_counts(normalize=True).head(3)
            profile[col] = {str(k): round(float(v) * 100, 1) for k, v in top_values.items()}
        cluster_profiles[str(int(cluster_id))] = profile

    summary = {
        "data_file": args.data,
        "n_rows_used": int(len(feature_df)),
        "n_rows_dropped_missing": int(dropped),
        "features_used": {"numeric": numeric_cols, "categorical": categorical_cols},
        "k_search_range": [args.k_min, max_k],
        "k_candidates": [
            {"k": r["k"], "silhouette_score": r["silhouette_score"], "variance_explained": r["variance_explained"]}
            for r in results
        ],
        "chosen_k": best["k"],
        "chosen_silhouette_score": best["silhouette_score"],
        "chosen_variance_explained": best["variance_explained"],
        "stability": stability_label(best["silhouette_score"]),
        "cluster_profiles": cluster_profiles,
    }

    feature_df[[args.id_col, "cluster"] + numeric_cols + categorical_cols].to_csv(args.out_assignments, index=False)
    with open(args.out_summary, "w") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps({k: v for k, v in summary.items() if k != "cluster_profiles"}, indent=2))
    print(f"\nWrote row-level cluster assignments to: {args.out_assignments}")
    print(f"Wrote full summary (including cluster profiles) to: {args.out_summary}")
    tail = "Treat as directional-only in any output." if summary["stability"] == "weak" else ""
    print(f"\nStability: {summary['stability']} (silhouette={summary['chosen_silhouette_score']}). {tail}")

    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Real-computation backend for the Segmentation Analyzer workflow.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_suff = sub.add_parser("sufficiency", help="Check whether data meets the sample-size/time-window bar for a question.")
    p_suff.add_argument("--data", required=True, help="Path to CSV file.")
    p_suff.add_argument("--group-col", help="Column to check group sizes for (e.g. channel, persona, cluster candidate).")
    p_suff.add_argument("--min-per-group", type=int, default=30, help="Minimum rows required per group (default 30).")
    p_suff.add_argument("--feature-cols", help="Comma-separated columns intended as clustering features.")
    p_suff.add_argument("--min-samples-per-feature", type=int, default=15, help="Minimum samples per feature (default 15).")
    p_suff.add_argument("--date-col", help="Date/timestamp column to check time-window coverage.")
    p_suff.add_argument("--min-window-days", type=int, default=90, help="Minimum days of history required (default 90).")
    p_suff.add_argument("--missingness-cols", help="Comma-separated columns to check for missing-data rate.")
    p_suff.add_argument("--max-missing-pct", type=float, default=20.0, help="Max allowed pct missing per column (default 20).")
    p_suff.add_argument("--out", help="Optional path to write the JSON result to.")
    p_suff.set_defaults(func=run_sufficiency)

    p_clus = sub.add_parser("cluster", help="Run K-means clustering with a silhouette-based stability check.")
    p_clus.add_argument("--data", required=True, help="Path to CSV file.")
    p_clus.add_argument("--id-col", required=True, help="Column identifying each row (account_id, persona_id, etc.).")
    p_clus.add_argument("--numeric-cols", default="", help="Comma-separated numeric feature columns.")
    p_clus.add_argument("--categorical-cols", default="", help="Comma-separated categorical feature columns.")
    p_clus.add_argument("--k-min", type=int, default=2)
    p_clus.add_argument("--k-max", type=int, default=6)
    p_clus.add_argument("--random-state", type=int, default=42)
    p_clus.add_argument("--out-assignments", default="cluster_assignments.csv")
    p_clus.add_argument("--out-summary", default="cluster_summary.json")
    p_clus.set_defaults(func=run_cluster)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
