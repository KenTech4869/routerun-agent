# results/

This directory stores agent evaluation outputs and benchmark results produced by the RouteRun multi-agent system.

## Contents

| File pattern | Description |
|---|---|
| `eval_YYYYMMDD.json` | Per-run evaluation scores from the Evaluator Agent (coherence, personalization, safety) |
| `training_plan_sample_*.json` | Sample `goalContext` snapshots written by `write_training_plan` |
| `acwr_benchmark.csv` | ACWR accuracy comparison: rule-based baseline vs. Ayumu Head Coach |

## Generating results

```bash
# Run the evaluation harness against the deployed Agent Engine
cd deploy/
python eval_agent.py --uid <TEST_UID> --runs 10 --output ../results/eval_$(date +%Y%m%d).json
```

> **Note:** Actual user data is never committed here.  
> All sample files use anonymised or synthetic run records.
