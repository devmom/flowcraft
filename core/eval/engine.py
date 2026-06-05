"""Agent Execution Evaluation Engine - main orchestrator."""
from __future__ import annotations

import json
import logging
import statistics
import time
from pathlib import Path
from typing import Any

from flowcraft_core.app import FlowCraftApp
from flowcraft_core.config.settings import load_settings

from .llm_judge import LLMJudge
from .regression import RegressionDetector, save_baseline
from .report import EvalReportGenerator, quick_report, quick_report_zh
from .root_cause import RootCauseAnalyzer
from .rule_evaluators import (
    StateTransitionEvaluator,
    StructureEvaluator,
    SecurityEvaluator,
    ChainEvaluator,
    EfficiencyEvaluator,
)
from .runner import CaseRunner

logger = logging.getLogger(__name__)

RULE_EVALUATORS = [
    StateTransitionEvaluator(),
    StructureEvaluator(),
    SecurityEvaluator(),
    ChainEvaluator(),
    EfficiencyEvaluator(),
]

DIMENSION_MAP = {
    "state_transition": ["state_transition"],
    "structure": ["structure"],
    "security": ["security"],
    "chain": ["chain"],
    "efficiency": ["efficiency"],
    "llm_judge": ["llm_judge"],
}

ALL_DIMENSIONS = list(DIMENSION_MAP.keys())


class AgentEvalEngine:
    """Main engine for automated agent execution evaluation."""

    def __init__(
        self,
        suite_path: str | Path = "eval/suites/manifest.json",
        baseline_path: str | Path | None = "eval/baselines/v0.1.json",
        judge_model: str = "default",
        output_dir: str | Path = "eval/results",
        app: FlowCraftApp | None = None,
    ):
        self.suite_path = Path(suite_path)
        self.baseline_path = Path(baseline_path) if baseline_path else None
        self.judge_model = judge_model
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        if app is None:
            settings = load_settings()
            self.app = FlowCraftApp(settings)
        else:
            self.app = app

        self.runner = CaseRunner(self.app, self.app.events)
        self.judge = LLMJudge(self.app.model_gateway, judge_model)
        self.report_gen = EvalReportGenerator()
        self.root_cause = RootCauseAnalyzer()
        self.regression_detector = RegressionDetector(
            self.baseline_path if self.baseline_path and self.baseline_path.exists()
            else None
        )

        self._cases: list[dict] = []
        self._results: list[dict] = []

    def load_suite(self, path: Path | None = None) -> list[dict]:
        """Load evaluation test cases from suite file or directory."""
        source = Path(path) if path else self.suite_path

        if source.is_dir():
            cases = []
            for f in sorted(source.rglob("*.json")):
                try:
                    with open(f, 'r', encoding='utf-8') as fh:
                        data = json.load(fh)
                    if isinstance(data, list):
                        cases.extend(data)
                    elif isinstance(data, dict) and "cases" in data:
                        cases.extend(data["cases"])
                    elif isinstance(data, dict) and "case_id" in data:
                        cases.append(data)
                except Exception as exc:
                    logger.warning("Failed to load %s: %s", f, exc)
            return cases

        if source.suffix == ".json":
            with open(source, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "cases" in data:
                # Manifest with case file paths → resolve and load each
                base_dir = source.parent
                loaded = []
                for entry in data["cases"]:
                    if isinstance(entry, str):
                        case_path = base_dir / entry
                        if case_path.exists():
                            try:
                                with open(case_path, 'r', encoding='utf-8') as cf:
                                    case_data = json.load(cf)
                                if isinstance(case_data, list):
                                    loaded.extend(case_data)
                                elif isinstance(case_data, dict) and "cases" in case_data:
                                    loaded.extend(case_data["cases"])
                                elif isinstance(case_data, dict) and "case_id" in case_data:
                                    loaded.append(case_data)
                            except Exception as exc:
                                logger.warning("Failed to load %s: %s", case_path, exc)
                        else:
                            logger.warning("Case file not found, skipped: %s", case_path)
                    elif isinstance(entry, dict):
                        loaded.append(entry)
                return loaded
            return [data]

        return []

    def run(self, cases: list[dict] | None = None,
            max_cases: int | None = None,
            skip_llm_judge: bool = False) -> dict[str, Any]:
        """Run evaluation on all test cases."""
        if cases is None:
            cases = self.load_suite()

        if max_cases:
            cases = cases[:max_cases]

        self._cases = cases
        self._results = []

        logger.info("Starting evaluation: %d cases", len(cases))
        t0 = time.monotonic()

        for i, case in enumerate(cases):
            cid = case.get("case_id", f"case_{i}")
            logger.info("[%d/%d] %s", i + 1, len(cases), cid)

            run_result = self.runner.run(case)

            traces = run_result.get("traces", [])
            rule_scores = {}
            for ev in RULE_EVALUATORS:
                try:
                    rule_scores[ev.name] = ev.evaluate(traces, case)
                except Exception as exc:
                    logger.warning("Evaluator %s failed: %s", ev.name, exc)
                    rule_scores[ev.name] = {"score": 0.5, "error": str(exc)}

            llm_result = {}
            if not skip_llm_judge and run_result.get("status") != "EXECUTION_ERROR":
                try:
                    llm_result = self.judge.evaluate(
                        case, traces,
                        run_result.get("final_output", ""))
                except Exception as exc:
                    logger.warning("LLM Judge failed for %s: %s", cid, exc)
                    llm_result = {"overall_score": 3.0, "fallback": True,
                                  "error": str(exc)}
            else:
                llm_result = {"overall_score": 3.0, "skipped": True}
            rule_scores["llm_judge"] = llm_result

            dim_scores = {}
            for dim_name in ALL_DIMENSIONS:
                if dim_name == "llm_judge":
                    dim_scores[dim_name] = llm_result.get("overall_score", 3.0)
                else:
                    scores_for_dim = [
                        rule_scores.get(ev_name, {}).get("score", 0.5)
                        for ev_name in DIMENSION_MAP.get(dim_name, [dim_name])
                    ]
                    dim_scores[dim_name] = (
                        sum(scores_for_dim) / len(scores_for_dim)
                        if scores_for_dim else 0.5
                    )

            overall = sum(dim_scores.values()) / len(dim_scores) if dim_scores else 3.0

            case_result = {
                "case_id": cid,
                "description": case.get("description", ""),
                "category": case.get("category", ""),
                "difficulty": case.get("difficulty", ""),
                "status": run_result.get("status", "UNKNOWN"),
                "overall_score": round(overall, 2),
                "dimension_scores": {k: round(v, 2) for k, v in dim_scores.items()},
                "rule_scores": {
                    k: round(v.get("score", 0), 2)
                    for k, v in rule_scores.items()
                    if k != "llm_judge"
                },
                "llm_judge": llm_result,
                "duration_ms": run_result.get("duration_ms", 0),
                "model_calls": run_result.get("model_calls", 0),
                "tool_calls": run_result.get("tool_calls", 0),
                "total_tokens": run_result.get("total_tokens", 0),
                "trace_count": run_result.get("trace_count", 0),
                "error": run_result.get("error", ""),
            }

            if overall < 3.0:
                case_result["root_cause"] = self.root_cause.analyze({
                    **run_result, "case_id": cid,
                })

            self._results.append(case_result)

        elapsed = time.monotonic() - t0
        logger.info("Evaluation complete: %d cases in %.1fs", len(cases), elapsed)

        aggregated = self._aggregate()
        aggregated["metadata"] = {
            "suite_path": str(self.suite_path),
            "case_count": len(cases),
            "elapsed_s": round(elapsed, 1),
            "judge_model": self.judge_model,
            "skip_llm_judge": skip_llm_judge,
        }

        regression = self.regression_detector.detect(aggregated)
        aggregated["regression"] = regression

        report_dir = self.report_gen.generate(aggregated, regression, self.output_dir)
        aggregated["report_dir"] = str(report_dir)

        summary = quick_report(aggregated, regression)
        logger.info(summary)
        print(summary)
        print(quick_report_zh(aggregated, regression))

        return aggregated

    def _aggregate(self) -> dict[str, Any]:
        """Aggregate per-case results into dimension-level statistics."""
        results = self._results
        if not results:
            return {
                "overall_score": 0, "case_count": 0,
                "pass_count": 0, "dimensions": {}, "per_case": [],
            }

        overall_scores = [r.get("overall_score", 0) for r in results]
        overall = statistics.mean(overall_scores) if overall_scores else 0
        pass_count = sum(1 for s in overall_scores if s >= 3.0)

        dimensions = {}
        for dim in ALL_DIMENSIONS:
            dim_scores = [
                r.get("dimension_scores", {}).get(dim, 0) for r in results
            ]
            if not dim_scores:
                continue
            dimensions[dim] = {
                "mean": round(statistics.mean(dim_scores), 2),
                "median": round(statistics.median(dim_scores), 2),
                "std": round(statistics.stdev(dim_scores), 2)
                if len(dim_scores) > 1 else 0,
                "min": round(min(dim_scores), 2),
                "max": round(max(dim_scores), 2),
                "pass_rate": round(
                    sum(1 for s in dim_scores if s >= 3.0) / len(dim_scores), 3),
                "sample_count": len(dim_scores),
            }

        efficiency_metrics = {
            "avg_duration_ms": statistics.mean(
                [r.get("duration_ms", 0) for r in results]),
            "avg_model_calls": statistics.mean(
                [r.get("model_calls", 0) for r in results]),
            "avg_tool_calls": statistics.mean(
                [r.get("tool_calls", 0) for r in results]),
            "avg_tokens": statistics.mean(
                [r.get("total_tokens", 0) for r in results]),
        }

        return {
            "overall_score": round(overall, 2),
            "case_count": len(results),
            "pass_count": pass_count,
            "fail_count": len(results) - pass_count,
            "dimensions": dimensions,
            "efficiency_metrics": efficiency_metrics,
            "per_case": results,
        }

    def save_baseline(self, version: str = "v0.1") -> Path:
        """Save current results as a new baseline."""
        if not self._results:
            raise ValueError("No evaluation results to save as baseline")

        aggregated = self._aggregate()
        baseline_path = self.output_dir / "baselines" / f"{version}.json"
        save_baseline(aggregated, baseline_path, version)

        if self.baseline_path:
            save_baseline(aggregated, self.baseline_path, version)

        logger.info("Baseline saved: %s", baseline_path)
        return baseline_path

    @property
    def results(self) -> list[dict]:
        return self._results

    @property
    def cases(self) -> list[dict]:
        return self._cases
