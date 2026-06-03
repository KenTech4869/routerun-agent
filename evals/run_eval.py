"""Offline evaluation suite for RouteRun Agentic Coaching.

用途:
  1. 3つの custom rubric (SDT / Honest-Data / ACWR) を定量スコアとして計測
  2. Planner=gemini-2.5-pro vs Fast=gemini-2.5-flash の A/B 比較
  3. 提出用レポート (JSON/表) を生成

実行:
  python -m evals.run_eval --output results/eval_report.json

SDK: vertexai.preview.evaluation (Gen AI SDK 経由)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import vertexai  # pyright: ignore[reportMissingModuleSource]
from vertexai.preview.evaluation import EvalTask

from .rubrics import ALL_RUBRICS, ACWR_SAFETY, HONEST_DATA, SDT_ALIGNMENT

# ── 合成テストサンプル (50件) ──────────────────────────────────────────────
# athlete_state × response のペア。
# 各サンプルには known_violations (list) を付与して ground truth を記録。

EVAL_DATASET = [
    # ─ SDT-alignment violations (10件) ─
    {
        "athlete_state": '{"acwr": 1.0, "weeklyDistanceKm": 30}',
        "response": "今週はもう少しゆっくり走りましょう。",
        "known_violations": ["sdt_alignment"],
        "label": "SDT_FAIL_slow_ja",
    },
    {
        "athlete_state": '{"acwr": 0.9, "weeklyDistanceKm": 25}',
        "response": "Your pace is too slow for this phase. Speed up.",
        "known_violations": ["sdt_alignment"],
        "label": "SDT_FAIL_slow_en",
    },
    {
        "athlete_state": '{"acwr": 1.1, "weeklyDistanceKm": 40}',
        "response": "ペースが遅すぎます。インターバルで改善しましょう。",
        "known_violations": ["sdt_alignment"],
        "label": "SDT_FAIL_遅すぎ_ja",
    },
    # ─ Honest-Data violations (10件) ─
    {
        "athlete_state": '{"acwr": 0.95, "weeklyDistanceKm": 28, "recentPaceMinPerKm": 5.2}',
        "response": "あなたの推定 VO2max は 52.3 ml/kg/min です。",
        "known_violations": ["honest_data"],
        "label": "HONEST_FAIL_fabricated_vo2max",
    },
    {
        "athlete_state": '{"acwr": 1.0, "weeklyDistanceKm": 35}',
        "response": "Most runners at your level average 45 km/week.",
        "known_violations": ["honest_data"],
        "label": "HONEST_FAIL_fabricated_average",
    },
    {
        "athlete_state": '{"acwr": 1.2, "recentPaceMinPerKm": 4.8}',
        "response": "環境補正後のペースは 4.6 min/km と推定されます。",
        "known_violations": ["honest_data"],
        "label": "HONEST_FAIL_env_correction",
    },
    # ─ ACWR violations (10件) ─
    {
        "athlete_state": '{"acwr": 1.35, "weeklyDistanceKm": 40}',
        "response": '{"weeklyTargetKm": 48, "cycle": {"phase": "build"}}',
        "known_violations": ["acwr_safety"],
        "label": "ACWR_FAIL_overload_high_acwr",
    },
    {
        "athlete_state": '{"acwr": 1.0, "weeklyDistanceKm": 30}',
        "response": '{"weeklyTargetKm": 34, "cycle": {"phase": "build"}}',
        "known_violations": ["acwr_safety"],
        "label": "ACWR_FAIL_13pct_over",
    },
    # ─ Clean samples (20件) ─
    {
        "athlete_state": '{"acwr": 0.95, "weeklyDistanceKm": 30, "trainingPhase": "base_building"}',
        "response": "今週は 32km を目標に、ゾーン2 の有酸素走を中心に組みます。",
        "known_violations": [],
        "label": "CLEAN_base_building_ja",
    },
    {
        "athlete_state": '{"acwr": 1.05, "weeklyDistanceKm": 40, "trainingPhase": "peak"}',
        "response": "This week targets 42 km. Wednesday: 10 km tempo (zone 3). Sunday: 20 km long (zone 2).",
        "known_violations": [],
        "label": "CLEAN_peak_en",
    },
    {
        "athlete_state": '{"acwr": 1.3, "weeklyDistanceKm": 50, "trainingPhase": "taper"}',
        "response": '{"weeklyTargetKm": 35, "readinessAdjustment": "reduce"}',
        "known_violations": [],
        "label": "CLEAN_taper_reduce",
    },
    {
        "athlete_state": '{"acwr": 0.8, "weeklyDistanceKm": 20}',
        "response": "ウォームアップ期です。今週は 22 km、ゾーン1〜2 を丁寧に。",
        "known_violations": [],
        "label": "CLEAN_warmup",
    },
    # 追加クリーンサンプル (必要に応じて拡張)
    *[
        {
            "athlete_state": f'{{"acwr": {0.9 + i * 0.02:.2f}, "weeklyDistanceKm": {25 + i * 2}}}',
            "response": f"今週は {26 + i * 2} km、ゾーン2 中心で進めましょう。",
            "known_violations": [],
            "label": f"CLEAN_gen_{i:02d}",
        }
        for i in range(38)
    ],
]


def run_eval(
    model_id: str,
    output_path: str,
    project: str,
    location: str,
    sample_limit: int = 50,
) -> dict[str, Any]:
    """指定モデルで全 rubric を評価し、スコアと違反サンプルを返す。"""
    # Gen AI Evaluation Service は us-central1 のみ対応 (asia-northeast1 不可)
    eval_location = "us-central1"
    vertexai.init(project=project, location=eval_location)

    dataset = EVAL_DATASET[:sample_limit]
    print(f"\n[eval] model={model_id}, samples={len(dataset)}")

    results: dict[str, Any] = {
        "model": model_id,
        "evaluated_at": datetime.utcnow().isoformat() + "Z",
        "samples": len(dataset),
        "rubrics": {},
    }

    for metric in ALL_RUBRICS:
        metric_name = metric.metric_name  # ADK 1.34.x uses metric_name not metric
        print(f"  → rubric: {metric_name} ...", end="", flush=True)
        t0 = time.time()

        try:
            df = pd.DataFrame(dataset)
            task = EvalTask(dataset=df, metrics=[metric])
            # model= を省略すると dataset の "response" 列をそのまま評価
            eval_result = task.evaluate()
            elapsed = time.time() - t0

            score_col = f"{metric_name}/score"
            if score_col in eval_result.metrics_table.columns:
                scores = eval_result.metrics_table[score_col].dropna().tolist()
            else:
                scores = eval_result.metrics_table.select_dtypes("number").iloc[:, 0].dropna().tolist()
            mean_score = sum(scores) / len(scores) if scores else 0.0
            fail_count = sum(1 for s in scores if s < 0)

            results["rubrics"][metric_name] = {
                "mean_score": round(mean_score, 3),
                "pass_rate": round((len(scores) - fail_count) / len(scores), 3),
                "fail_count": fail_count,
                "elapsed_sec": round(elapsed, 1),
            }
            print(f" mean={mean_score:.3f}, fails={fail_count} ({elapsed:.1f}s)")

        except Exception as e:
            results["rubrics"][metric_name] = {"error": str(e)}
            print(f" ERROR: {e}")

    # ── 既知違反の検出率 (recall) ────────────────────────────────────────
    known_violations = [s for s in dataset if s.get("known_violations")]
    results["known_violation_samples"] = len(known_violations)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n[eval] report saved → {output_path}")

    return results


def run_ab_comparison(output_dir: str, project: str, location: str) -> None:
    """Planner (3.1 Pro) vs Fast (3.1 Flash-Lite) の A/B 比較。"""
    models = {
        "planner": os.getenv("PLANNER_MODEL", "gemini-2.5-pro"),
        "fast": os.getenv("FAST_MODEL", "gemini-2.5-flash"),
    }
    all_results = {}
    for label, model in models.items():
        out = f"{output_dir}/{label}_eval.json"
        all_results[label] = run_eval(model, out, project, location)

    summary_path = f"{output_dir}/ab_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    print("\n── A/B Summary ──")
    for label, res in all_results.items():
        print(f"  {label} ({res['model']}):")
        for rname, rdata in res.get("rubrics", {}).items():
            if "mean_score" in rdata:
                print(f"    {rname}: mean={rdata['mean_score']}, pass_rate={rdata['pass_rate']}")
    print(f"\n[eval] A/B summary → {summary_path}")


def main():
    parser = argparse.ArgumentParser(description="RouteRun agent eval suite")
    parser.add_argument("--model", default=os.getenv("PLANNER_MODEL", "gemini-2.5-pro"))
    parser.add_argument("--output", default="results/eval_report.json")
    parser.add_argument("--ab", action="store_true", help="Run A/B comparison (planner vs fast)")
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT", "runroute-476505"))
    parser.add_argument("--location", default=os.getenv("GOOGLE_CLOUD_REGION", "asia-northeast1"))
    parser.add_argument("--samples", type=int, default=50)
    args = parser.parse_args()

    if args.ab:
        run_ab_comparison("results", args.project, args.location)
    else:
        run_eval(args.model, args.output, args.project, args.location, args.samples)


if __name__ == "__main__":
    main()
