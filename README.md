# RouteRun Agentic Coaching

**Google for Startups AI Agents Challenge — 2026 Submission**

> 日本語版の概要は [末尾のセクション](#日本語概要) を参照してください。

A sub-project that rebuilds RouteRun's "Ayumu" AI coach as a **Supervisor multi-agent system** on Vertex AI Agent Engine. Triggered by a run-completion event in the iOS app, the system autonomously plans, evaluates, and persists personalized training plans for every Pro user.

---

## Highlights

| Item | Value |
|------|-------|
| Agent Engine | `projects/231451951695/locations/asia-northeast1/reasoningEngines/1418026952203173888` |
| Head Coach model | `gemini-2.5-pro` (thinking_budget=2048) |
| Periodization model | `gemini-2.5-pro` (thinking_budget=4096) |
| Readiness model | `gemini-2.5-flash` (thinking_budget=1024) |
| Evaluator model | `gemini-2.5-flash-lite` (no thinking — pattern-match gate) |
| Pattern | B (Supervisor multi-agent) + D (In-loop Evaluator) |
| Eval rubrics | SDT Alignment / Honest Data / ACWR Safety / Injury Disclaimer (4 total) |
| SDT pass rate | 94% (gemini-2.5-pro / gemini-2.5-flash equivalent) |
| Memory Bank | Firestore `users/{uid}.trainingPlan` |
| Demo users | `test_sub4` (sub-4hr marathon goal) / `test_firstfull` (first full marathon goal) |

---

## Architecture

```
iOS App (RunningActiveView)
   │  ← run-completion event
   ▼
onRunCompleted (Cloud Functions v2 / Firestore trigger)
   │  if (isPro) → daily transaction rate-limit check
   │  → agent_planning_queue.add({ uid, runId, status: "pending" })
   ▼
onAgentPlanningQueue (Cloud Functions v2 / Firestore trigger)
   │  GCP Metadata Server auth (ADC) → access_token
   │  → Vertex AI Agent Engine: create session → streamQuery
   ▼
Vertex AI Agent Engine  ←────────────────────────────────┐
   │                                                       │
   ▼  [Head Coach — gemini-2.5-pro]                       │
   ├─ get_athlete_state()  ← Firestore goalContext         │
   │                                                       │
   ├─ AgentTool: periodization  [gemini-2.5-pro]           │
   │   └─ get_athlete_state() → weekly mesocycle design    │
   │                                                       │
   ├─ AgentTool: readiness  [gemini-2.5-flash + thinking]  │
   │   └─ get_athlete_state() → HRV/sleep → load adjust    │
   │                                                       │
   ├─ AgentTool: evaluator  [gemini-2.5-flash-lite]        │
   │   └─ SDT / Honest-Data / ACWR gate → PASS/FAIL        │
   │                                                       │
   └─ write_training_plan()  → Firestore Memory Bank ──────┘
         users/{uid}.trainingPlan
```

### Agent Roles

| Agent | Model | Thinking Budget | Responsibility |
|-------|-------|----------------|----------------|
| **Head Coach** (`ayumu_head_coach`) | gemini-2.5-pro | 2048 | Orchestration, plan integration, final output |
| **Periodization** | gemini-2.5-pro | 4096 | Meso/micro-cycle design toward race goal |
| **Readiness** | gemini-2.5-flash | 1024 | Load adjustment based on HRV / sleep / resting HR |
| **Evaluator** | gemini-2.5-flash-lite | 0 | SDT / Honest-Data / ACWR pattern-match gate |

---

## What Data the Head Coach Considers

### Input from the trigger message (latest completed run)

| Field | Description |
|-------|-------------|
| `distance_km` | Run distance in km |
| `duration_min` | Run duration in minutes |
| `avg_pace_min_per_km` | Average pace |
| `avg_hr` | Average heart rate |
| `date` | Run date |

### Input from `get_athlete_state()` — Firestore `users/{uid}.goalContext`

| Field | Description |
|-------|-------------|
| `acwr` | Acute:Chronic Workload Ratio (safe range: 0.8–1.3) |
| `trainingPhase` | `base_building` / `build` / `peak` / `taper` / `recovery` |
| `weeklyVolumeTrend` | `increasing` / `stable` / `decreasing` |
| `readinessAdjustment` | `maintain` / `reduce` / `intensify` |
| `todayWorkout` | Scheduled session for today |
| `raceTarget` | Target race name, date, distance |
| `currentFitness.vo2MaxEstimate` | VO2max estimate (from HealthKit) |
| `currentFitness.recentPaceMinPerKm` | Recent average pace |
| `currentFitness.monthlyDistanceKm` | Monthly total distance |

> **Current limitation**: Physical profile data (age, weight, height, gender) stored in the user document is **not yet forwarded to `goalContext`**, so the Head Coach does not currently factor these into its plans. A future update will enrich `goalContext` with these fields.

### On-device Agentic Loop (iOS, complementary)

In parallel, the iOS app runs an on-device agentic loop that pulls HealthKit data directly:
- VO2max / HRV / recovery score / sleep duration & quality
- Run history / pace / CTL / ATL

Sleep data (`sleepDurationHours`, `sleepQualityScore`) aligns with the Readiness agent's input, creating a two-layer safety check.

---

## Safety Constraints (Evaluator Gate)

| Constraint | Description | Guard Layer |
|------------|-------------|-------------|
| SDT Alignment | Never output "slow / 遅い / too slow". Respect all paces equally. | Evaluator LLM + `write_training_plan` rule-based |
| Honest Data Only | Do not fabricate stats, VO2max estimates, or environment-adjusted paces absent from `athlete_state` | Evaluator LLM |
| ACWR Safety | Cap weekly load increase at +10%. Block any load increase when `acwr > 1.3`. | Evaluator LLM + `check_acwr_safety()` rule-based |
| Injury Disclaimer | Require a physician-consultation disclaimer in any response containing injury-related keywords | **Eval rubric only** (not yet wired into Agent Gate) |

### Evaluator Few-Shot Examples (6 total — JA + EN)

| # | Language | Verdict | Violation |
|---|----------|---------|-----------|
| 1 | JA | PASS | None |
| 2 | JA | FAIL | SDT (遅すぎ) |
| 3 | JA | FAIL | ACWR exceeded |
| 4 | EN | PASS | None |
| 5 | EN | FAIL | Honest-Data (fabricated VO2max) |
| 6 | EN | FAIL | SDT + ACWR compound violation |

---

## Directory Structure

```
routerun-agent/
├── agent/
│   ├── __init__.py
│   ├── agents.py          ← ADK Agent definitions (head_coach + sub-agents)
│   ├── instructions.py    ← System instructions for all agents (CoT 7-step + JA/EN few-shot)
│   └── tools.py           ← Cloud Functions / Firestore wrappers (Pydantic MCP schema)
├── deploy/
│   ├── deploy_agent_engine.py  ← Agent Engine deploy + Secret Manager auto-save
│   └── monitoring_alert.yaml   ← PubSub DLQ + CF error-rate Cloud Monitoring alert
├── demo/
│   ├── seed_demo_data.py  ← Firebase Auth user creation + Firestore seed data
│   └── run_timeline.py    ← 8-week compressed timeline demo
├── evals/
│   ├── rubrics.py         ← Custom PointwiseMetric definitions (4 rubrics)
│   ├── run_eval.py        ← Gen AI Evaluation Service A/B comparison
│   └── collect_traces.py  ← Trace collection utility
├── results/
│   ├── ab_summary.json    ← A/B eval results (gemini-2.5-pro vs gemini-2.5-flash)
│   ├── planner_eval.json
│   └── fast_eval.json
├── logs/
│   └── timeline_test_sub4_*.json  ← 8-week timeline run logs
├── pyproject.toml
├── .env                   ← AGENT_ENGINE_RESOURCE_NAME etc. (git-ignored)
└── README.md
```

---

## ADK Tool Reference

| Tool | Schema | Purpose |
|------|--------|---------|
| `get_athlete_state(uid)` | `str` → `AthleteStateOutput` | Read Firestore `users/{uid}.goalContext` — returns ACWR, readinessReason, trainingPhase, raceTarget, currentFitness |
| `suggest_route_areas(...)` | `float×4 + opt` → `RouteAreaOutput` | Call `suggestRouteAreas` CF for AI-recommended route areas per session |
| `recommend_races(...)` | `float×2 + opt` → `RaceRecommendationOutput` | Call `recommendEventsV3` CF to suggest races matching fitness and goals |
| `chat_with_coach(...)` | `str×2` → `dict` | Call `chatWithCoach` CF to answer runner questions |
| `write_training_plan(uid, plan)` | `str + dict` → `WritePlanOutput` | Persist plan to Firestore Memory Bank + update `goalContext.readinessAdjustment/Reason`. Idempotent via `planVersion`. |
| ~~`check_acwr_safety`~~ | internal helper | Called internally inside `write_training_plan`. **Not registered** as an ADK tool in `agents.py`. |

> **MCP-compliant**: All tool I/O types are defined as Pydantic models (`AthleteStateOutput` etc.), functioning as JSON-RPC 2.0 schemas.

---

## Evaluation Results (Gen AI Evaluation Service — 50 samples)

Dataset: 20 clean samples + 30 intentional violation samples (SDT / Honest-Data / ACWR — 10 each)

| Rubric | gemini-2.5-pro | gemini-2.5-flash | Interpretation |
|--------|:--------------:|:----------------:|----------------|
| SDT Alignment | mean=0.88, pass=94% | mean=0.88, pass=94% | Both models detect "slow" language with high accuracy |
| Honest Data | mean=-0.88, pass=6% | mean=-0.88, pass=6% | Fabricated stats in violation samples correctly detected (low pass rate = high sensitivity) |
| ACWR Safety | mean=0.02, pass=52% | mean=0.06, pass=54% | Majority of ACWR violations detected; Flash slightly better |
| Injury Disclaimer | — | — | Requires disclaimer when injury keywords present |

> **Precision/Recall separation**: `run_eval.py` separately measures `precision_clean_only_pass_rate` (false-positive rate inverse) and `recall_violation_detect_rate` (violation detection rate). Use `--clean-only` for precision-only evaluation.

```bash
# Full A/B evaluation
python -m evals.run_eval --ab

# Precision-only (no distribution bias, faster)
python -m evals.run_eval --clean-only
```

---

## Setup

### Prerequisites

- Python 3.10+
- Google Cloud SDK (`gcloud auth application-default login`)
- Firebase Admin SDK Service Account (or ADC)
- Vertex AI API / Agent Engine API enabled

```bash
cd routerun-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Environment Variables (`.env`)

```env
GOOGLE_CLOUD_PROJECT=runroute-476505
GOOGLE_CLOUD_REGION=asia-northeast1
FIREBASE_DATABASE_ID=routerun-db
STAGING_BUCKET=gs://runroute-476505-agent-staging

PLANNER_MODEL=gemini-2.5-pro
FAST_MODEL=gemini-2.5-flash

# Auto-saved to Secret Manager by deploy_agent_engine.py
AGENT_ENGINE_RESOURCE_NAME=projects/231451951695/locations/asia-northeast1/reasoningEngines/1418026952203173888
```

---

## Demo

### 1. Seed demo data (first time only)

```bash
python -m demo.seed_demo_data
# Options:
#   --dry-run    Preview without writing
#   --cleanup    Remove all test data
#   --get-tokens Fetch Firebase ID tokens
```

### 2. Re-deploy Agent Engine

```bash
python -m deploy.deploy_agent_engine
# resource_name is auto-saved to Secret Manager:
# projects/runroute-476505/secrets/AGENT_ENGINE_RESOURCE_NAME
```

### 3. Deploy monitoring alert (first time only)

```bash
gcloud alpha monitoring channels list --project=runroute-476505
# Replace REPLACE_WITH_CHANNEL_ID in monitoring_alert.yaml, then:
gcloud alpha monitoring policies create \
  --policy-from-file=deploy/monitoring_alert.yaml
```

### 4. Run 8-week compressed timeline demo

```bash
# Production (Agent Engine)
AGENT_ENGINE_RESOURCE_NAME=<resource_name> python -m demo.run_timeline --uid test_sub4

# Local ADK (development)
python -m demo.run_timeline --uid test_sub4 --local
```

Week 3 triggers a re-plan event:

```
============================================================
★ PERTURBATION DETECTED — RE-PLAN IN PROGRESS ★
  Week 3: HRV drop / HR spike / long run skipped → Readiness: reduce
============================================================
```

### 5. A/B evaluation

```bash
python -m evals.run_eval --ab --output results/ab_summary.json
```

### 6. Cleanup

```bash
python -m demo.seed_demo_data --cleanup
```

Deletes all documents tagged `isDemo: true` (`run_history`, `agent_planning_queue`, `trainingPlan`, `goalContext`).

---

## Known Limitations & Next Actions

| Item | Status | Action Required |
|------|--------|----------------|
| Physical profile (age, weight, height) not in Head Coach context | ⚠️ Not implemented | Enrich `goalContext` with user profile fields; update `get_athlete_state` schema |
| Injury Disclaimer — not in Agent Gate | ⚠️ Not implemented | Add Step 4 to EVALUATOR instruction, re-deploy Agent Engine |
| Injury Disclaimer — eval samples not run | ℹ️ Pending | Add injury samples to `EVAL_DATASET` in `run_eval.py`, re-run `--ab` |

---

## Implementation Notes

### Two-copy instruction architecture

| File | Usage | Characteristics |
|------|-------|----------------|
| `agent/instructions.py` | Referenced by `agent/agents.py` for local execution | Full CoT 7-step with titles and Pydantic output schemas |
| `deploy/deploy_agent_engine.py` | Deployed to Agent Engine (the live system) | Inlined due to cloudpickle constraints; simplified "action loop" format |

Updating `instructions.py` does **not** affect the live Agent Engine. Changes must also be applied to `deploy_agent_engine.py` and re-deployed.

### Solving `No module named 'agent'` with cloudpickle

cloudpickle serializes top-level module functions as **module reference + qualname**. When the remote container loads `agent_engine.pkl`, it fails with `ModuleNotFoundError` if the `agent` package is not installed.

**Solution**: Define all tool functions as local closures inside `deploy()`. cloudpickle serializes local functions as **inline bytecode**, eliminating the remote package dependency.

```python
def deploy():
    # Local definition → cloudpickle inlines bytecode
    def get_athlete_state(uid: str) -> dict:
        ...
    def write_training_plan(uid: str, plan: dict) -> bool:
        ...
    head_coach = Agent(name="ayumu_head_coach", tools=[get_athlete_state, ...])
    agent_engines.create(agent_engine=head_coach, ...)  # no extra_packages needed
```

### Model availability in asia-northeast1

| Model ID | asia-northeast1 | Notes |
|----------|:--------------:|-------|
| `gemini-2.5-pro` | ✅ | Head Coach / Periodization |
| `gemini-2.5-flash` | ✅ | Readiness / Evaluator |
| `gemini-3.1-pro-preview` | ❌ | Does not exist |
| `gemini-3.1-flash-lite` | ❌ | Does not exist |

### IAM (Agent Engine → Firestore)

Grant to `service-231451951695@gcp-sa-aiplatform-re.iam.gserviceaccount.com`:
- `roles/datastore.user`
- `roles/firebase.viewer`

---

## Trace Viewer

```
https://console.cloud.google.com/vertex-ai/agents?project=runroute-476505
```

---

## Related iOS App Files

| File | Role |
|------|------|
| `RouteRun_ios/AICoachingView.swift` | Ayumu AI coach UI |
| `RouteRun_ios/GoalContextManager.swift` | Firestore real-time listener for `users/{uid}.goalContext` — includes `readinessReason` / `agentUpdatedAt` |
| `RouteRun_ios/AgentActionBanner.swift` | Three banner variants: `AgentActionBanner` (reduce/intensify), `AgentMaintainBanner` (maintain), `AgentReadinessTeaserBanner` (free user) |
| `RouteRun_ios/DashboardCardsView.swift` | Routes to the correct banner variant based on `readinessAdjustment` value |
| `functions/src/coaching/onRunCompleted.ts` | Run-completion trigger. All Pro users (no DEMO_UIDS). `if (isPro)` → daily rate-limit via `usage_counters/{uid}/daily/{dateKey}.agentPlanCount` → enqueue to `agent_planning_queue` |
| `functions/src/coaching/onAgentPlanningQueue.ts` | Triggered on `agent_planning_queue/{docId}` creation. GCP Metadata Server auth (no extra npm packages) → Vertex AI Agent Engine session → `streamQuery` → mark `completed`/`failed` |
| `functions/src/coaching/updateGoalContext.ts` | Supports `readinessReason` field. When `agentUpdatedAt` is present, agent judgment takes priority over ACWR rule-based overwrite |
| `RouteRun_ios/TrainingPlanManager.swift` | Reads and displays `users/{uid}.trainingPlan` |

---

## Agent Output → iOS Display Flow

```
Vertex AI Agent Engine
  └─ write_training_plan(uid, plan)
       ├─ users/{uid}.trainingPlan  ← persists the plan
       └─ users/{uid}.goalContext.currentFitness  ← writes readinessAdjustment + readinessReason + agentUpdatedAt
                                                           ↓ (Firestore real-time listener)
                                                    GoalContextManager (iOS)
                                                           ↓
                                                    DashboardCardsView
                                                      reduce/intensify → AgentActionBanner(reason: "HRV dropped...")
                                                      maintain        → AgentMaintainBanner(reason: "All metrics normal...")
                                                      free user       → AgentReadinessTeaserBanner
```

---

## 日本語概要

RouteRun の「あゆむ」AI コーチを Vertex AI Agent Engine 上の **Supervisor マルチエージェント**として構築したサブプロジェクトです。

### システム概要

iOS でランニングが完了すると、Cloud Functions がトリガーされ、Vertex AI Agent Engine 上のエージェントが自律的に8週間トレーニング計画を生成・評価・Firestore に永続化します。

### Head Coach が参照するデータ

- **最新ランのデータ**（トリガーメッセージ）: 距離 / 時間 / ペース / 平均心拍数
- **goalContext**（Firestore）: ACWR / トレーニングフェーズ / 週間ボリューム傾向 / VO2max 推定 / 月間距離 / 目標レース
- **現在の制限**: 年齢・体重・身長・性別などの身体プロフィールは未対応（将来対応予定）

### エージェント構成

| エージェント | モデル | 役割 |
|---|---|---|
| Head Coach | gemini-2.5-pro | 全体制御・計画統合 |
| Periodization | gemini-2.5-pro | メソ/マイクロサイクル設計 |
| Readiness | gemini-2.5-flash | HRV/睡眠による負荷調整 |
| Evaluator | gemini-2.5-flash-lite | SDT/Honest-Data/ACWR ゲート |

### 絶対制約

- **SDT**: 「遅い/slow」系ワードを出力禁止。全ランナーのペースを尊重。
- **Honest Data**: 入力データにない統計値・VO2max を創作禁止。
- **ACWR 安全**: 週次負荷増加 +10% 以内。`acwr > 1.3` 時は負荷増加禁止。
- **Injury Disclaimer**: 怪我キーワード含む応答に医師相談の免責事項を付与（eval rubric で測定）。

### デモ実行

```bash
cd routerun-agent
source .venv/bin/activate && source .env
python -m demo.run_timeline --uid test_sub4 --local
```

Week 3 で `★ PERTURBATION ★`（HRV 低下シナリオ）が発動し、`readinessAdjustment: reduce` で再プランが生成されます。
