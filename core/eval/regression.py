"""Regression detection for agent evaluation results."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class RegressionDetector:
    """Compares current evaluation scores against baseline to detect regressions."""

    def __init__(self, baseline_path: Path | None = None):
        self.baseline = None
        if baseline_path and baseline_path.exists():
            with open(baseline_path, 'r', encoding='utf-8') as f:
                self.baseline = json.load(f)

    def detect(self, current: dict, tolerance: dict | None = None) -> dict[str, Any]:
        """Detect regressions by comparing current scores to baseline.

        Args:
            current: Aggregated evaluation results with dimension scores.
            tolerance: Per-metric tolerance dict, e.g. {"overall_score": 0.3}.

        Returns:
            Dict with has_regression, regressions list, and verdict.
        """
        if self.baseline is None:
            return {
                "has_regression": False,
                "regressions": [],
                "verdict": "NO_BASELINE",
                "message": "No baseline available for comparison",
            }

        tolerance = tolerance or {}
        regressions = []

        # Compare dimension scores
        baseline_dims = self.baseline.get("dimensions", {})
        current_dims = current.get("dimensions", {})

        for dim, cur_data in current_dims.items():
            base_data = baseline_dims.get(dim, {})
            if not base_data:
                continue

            cur_mean = cur_data.get("mean", 0)
            base_mean = base_data.get("mean", 0)
            tol = tolerance.get(dim, tolerance.get("default", 0.3))

            if cur_mean < base_mean - tol:
                regressions.append({
                    "dimension": dim,
                    "metric": "mean_score",
                    "baseline": round(base_mean, 2),
                    "current": round(cur_mean, 2),
                    "delta": round(cur_mean - base_mean, 2),
                    "tolerance": tol,
                    "direction": "decreased",
                    "severity": "error" if dim == "output" else "warning",
                })

            # Check pass rate regression
            cur_pass = cur_data.get("pass_rate", 1.0)
            base_pass = base_data.get("pass_rate", 1.0)
            pass_tol = tolerance.get("pass_rate", 0.1)
            if cur_pass < base_pass - pass_tol:
                regressions.append({
                    "dimension": dim,
                    "metric": "pass_rate",
                    "baseline": round(base_pass, 3),
                    "current": round(cur_pass, 3),
                    "delta": round(cur_pass - base_pass, 3),
                    "tolerance": pass_tol,
                    "direction": "decreased",
                    "severity": "warning",
                })

        # Compare overall score
        cur_overall = current.get("overall_score", 0)
        base_overall = self.baseline.get("overall_score", 0)
        overall_tol = tolerance.get("overall_score", 0.3)
        if cur_overall < base_overall - overall_tol:
            regressions.append({
                "dimension": "overall",
                "metric": "overall_score",
                "baseline": round(base_overall, 2),
                "current": round(cur_overall, 2),
                "delta": round(cur_overall - base_overall, 2),
                "tolerance": overall_tol,
                "direction": "decreased",
                "severity": "error",
            })

        # Determine verdict
        if not regressions:
            verdict = "PASS"
        elif all(r["severity"] == "warning" for r in regressions):
            verdict = "WARN"
        else:
            verdict = "FAIL"

        return {
            "has_regression": len(regressions) > 0,
            "regression_count": len(regressions),
            "regressions": regressions,
            "verdict": verdict,
            "baseline_version": self.baseline.get("baseline_version", "unknown"),
        }


def load_baseline(path: Path) -> dict:
    """Load a baseline JSON file."""
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_baseline(results: dict, path: Path, version: str = "v0.1") -> None:
    """Save evaluation results as a new baseline."""
    import datetime
    baseline = {
        "baseline_version": version,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "overall_score": results.get("overall_score", 0),
        "dimensions": results.get("dimensions", {}),
        "metadata": results.get("metadata", {}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(baseline, f, indent=2, ensure_ascii=False)
    logger.info("Baseline saved to %s (v=%s, score=%.2f)",
                path, version, baseline["overall_score"])
