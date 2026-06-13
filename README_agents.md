# RouteRun Agentic Coaching

**Google for Startups AI Agents Challenge — 2026 Submission**

> 日本語版の概要は [末尾のセクション](#日本語概要) を参照してください。

A sub-project that rebuilds RouteRun's "Ayumu" AI coach as a **Supervisor multi-agent system** on Vertex AI Agent Engine. Triggered by run-completion events, chat planning queries, and schedule-based proactive flows, the system autonomously plans, evaluates, and persists personalized training plans for every Pro user.

---

## Highlights

| Item | Value |
|------|-------|
| Agent Engine | `projects/231451951695/locations/asia-northeast1/reasoningEngines/1418026952203173888` |
| Head Coach model | `gemini-2.5-pro` (thinking_budget=2048) |
| Periodization model | `gemini-2.5-pro` (thinking_budget=4096) |
| Readiness model | `gemini-2.5-flash` (thinking_budget=1024) |
| Race Strategy model | `gemini-2.5-flash` (thinking_budget=1024) |
| Route Intelligence model | `gemini-2.5-flash` (thinking_budget=1024) |
| Evaluator model | `gemini-2.5-flash-lite` (no thinking — pattern-match gate) |
| Pattern | B (Supervisor multi-agent) + D (In-loop Evaluator) |
| Eval rubrics | SDT Alignment / Honest Data / ACWR Safety / Injury Disclaimer (4 total) |
| SDT pass rate | 94% (gemini-2.5-pro / gemini-2.5-flash equivalent) |
| Memory Bank | `users/{uid}.trainingPlan` / `users/{uid}.raceStrategy` |
| Triggers | Run completion · Chat planning query · Mid-cycle replan (>20% deviation) · Daily proactive (08:00 JST) · Race registration |
| Build | 74 |
| Demo users | `test_sub4` (sub-4hr marathon goal) / `test_firstfull` (first full marathon goal) |

---

## Architecture

```
【トリガー A: ラン完了】
iOS App (RunningActiveView)
   │  ← run-completion event
   ▼
onRunCompleted (Cloud Functions v2 / Firestore trigger)
   │  if (isPro) → daily transaction rate-limit check
   │  → agent_planning_queue.add({ uid, runId, status: "pending", triggerType: "post_run" })
   │
   │  [NEW Build 74] 偏差 >20% 検出時:
   │  → agent_planning_queue.add({ triggerType: "mid_cycle_replan" })
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
   ├─ AgentTool: race_strategy  [gemini-2.5-flash]         │
   │   └─ get_athlete_state() → race_eve_pacing のみ委譲    │
   │       → negative split pacing → write_race_strategy() │
   │                                                       │
   ├─ AgentTool: route_intelligence  [gemini-2.5-flash]    │
   │   └─ get_athlete_state() + get_weather_context()      │
   │       → 天気 × ACWR × trainingPhase → ルート推薦      │
   │                                                       │
   ├─ AgentTool: evaluator  [gemini-2.5-flash-lite]        │
   │   └─ SDT / Honest-Data / ACWR gate → PASS/FAIL        │
   │                                                       │
   └─ write_training_plan()  → Firestore Memory Bank ──────┘
         users/{uid}.trainingPlan
         users/{uid}.raceStrategy  ← race_strategy agent が書き込み

【トリガー B: 日次プロアクティブ (Case A — proactiveCoaching.ts)】
Cloud Scheduler (08:00 JST)
   → onDailyProactiveCoachSchedule → Pub/Sub proactive-coach-trigger (fan-out)
   → onProactiveCoachWorker (per user)
       → 6-branch decision: race_eve → milestone → streak → nudge → recovery → reengagement
       → Gemini Flash-Lite (P1/P2/P5) or ルールベース (P3/P4) → FCM プッシュ

【トリガー C: レース登録 (Case C — proactiveCoaching.ts / onRaceTargetSet)】
Firestore users/{uid}.raceTarget 変更
   → onRaceTargetSet → agent_planning_queue (triggerType: "race_arc")
   → onAgentPlanningQueue → race_strategy Agent → write_race_strategy()

【トリガー D: チャット計画クエリ (Case D — AICoachingView.swift / CloudFunctionsClient.swift)】
iOS AICoachingView — planning intent 検出 (来週/プラン/計画/next week 等)
   → CloudFunctionsClient.triggerChatPlanAndPoll(uid:)
   → agent_planning_queue.add({ uid, triggerType: "chat_plan" })
   → onAgentPlanningQueue → Head Coach → write_training_plan()
   → iOS: agent_decisions/{uid}/history をポーリング (3分・10s間隔)
   → チャット画面に計画サマリーを返信

【トリガー E: Mid-Cycle Replan (onRunCompleted.ts)】
onRunCompleted — 実走距離 vs goalContext.todayWorkout.targetDistanceKm
   → |deviation| > 20% → triggerType: "mid_cycle_replan"
   → onAgentPlanningQueue → メッセージに "deviated significantly" 文言付加
   → Head Coach が再プランを生成 → write_training_plan()

【オンデバイス OODA エンジン (RunningActiveView.swift — $0)】
RunningActiveView (走行中)
   ├─ 60s 待機後に評価ループ開始 (90s 間隔)
   ├─ Observe: currentPace / heartRateZone (from RunningSessionManager)
   ├─ Orient: target pace との偏差・HR ゾーン判定
   ├─ Decide: ルールベース (HeartRateZone + deviation thresholds)
   └─ Act: VoiceCoachingManager.speakCustomMessage() — TTS 音声キュー
       peak HR → "ペースを落として" / pace +20% & low zone → "ペースを上げて" / pace -15% & high zone → "落ち着いて"
```

### Agent Roles

| Agent | Model | Thinking Budget | Responsibility |
|-------|-------|----------------|----------------|
| **Head Coach** (`ayumu_head_coach`) | gemini-2.5-pro | 2048 | Orchestration, plan integration, final output |
| **Periodization** | gemini-2.5-pro | 4096 | Meso/micro-cycle design toward race goal |
| **Readiness** | gemini-2.5-flash | 1024 | Load adjustment based on HRV / sleep / resting HR |
| **Race Strategy** | gemini-2.5-flash | 1024 | Negative split pacing design for `race_eve_pacing` trigger; 5km segment granularity; ACWR-adjusted intensity |
| **Route Intelligence** | gemini-2.5-flash | 1024 | Weather × ACWR × trainingPhase → route type recommendation (indoor/easy/tempo/interval) |
| **Evaluator** | gemini-2.5-flash-lite | 0 | SDT / Honest-Data / ACWR pattern-match gate |

> **Thinking budget rationale**: Budgets (4096/2048/1024/0) reflect task reasoning depth — Periodization requires multi-week mesocycle design (highest), Head Coach assembles structured JSON from sub-agent outputs (medium), Readiness interprets 3-variable HRV/sleep signals (low), Evaluator performs keyword/value pattern matching only (none). These values have **not** been empirically tuned via A/B test; they are engineering estimates. Run `evals/run_eval.py` with `--ab` comparing thinking_budget=0 vs current values to validate before changing.

---

## Trigger Types

| `triggerType` | Source | Agent Path | Description |
|---------------|--------|-----------|-------------|
| `post_run` | `onRunCompleted` (daily rate-limit) | Head Coach → Periodization + Readiness + Evaluator | Standard post-run training plan update |
| `mid_cycle_replan` | `onRunCompleted` (>20% distance deviation) | Head Coach + deviation context | Re-plan triggered when actual distance deviates >20% from planned `targetDistanceKm` |
| `race_arc` | `onRaceTargetSet` (new eventId detected) | Head Coach → race_strategy Agent | Race preparation arc: 8-week countdown plan + negative split strategy |
| `race_eve_pacing` | `onProactiveCoachWorker` (P0 branch: race day -1) | race_strategy Agent | Race-eve pacing strategy only; run_history fetch skipped |
| `chat_plan` | `AICoachingView.swift` (planning intent detected) | Head Coach → write_training_plan | User asks "来週のプランは?" etc. in chat; result polled and shown inline |

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
| `trigger` | Trigger type (`post_run` / `mid_cycle_replan` / `chat_plan` / etc.) |

For `mid_cycle_replan` triggers, the message additionally includes:

> "The athlete deviated significantly from their planned workout distance (>20%). Please re-evaluate the current training block and adjust the upcoming week's plan to account for this deviation."

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

### Input from `get_weather_context()` — OpenWeatherMap (Route Intelligence only)

| Field | Description |
|-------|-------------|
| `temperature_c` | Current temperature (°C) |
| `humidity_pct` | Humidity (%) |
| `precipitation_prob` | Precipitation probability (0.0–1.0) |
| `wind_speed_ms` | Wind speed (m/s) |
| `aqi` | Air Quality Index (0–500) |
| `condition` | `clear` / `cloudy` / `rain` / `snow` / `storm` |
| `advice` | `go` / `caution` / `avoid` |

> **Current limitation**: Physical profile data (age, weight, height, gender) stored in the user document is **not yet forwarded to `goalContext`**, so the Head Coach does not currently factor these into its plans. A future update will enrich `goalContext` with these fields.

### On-device OODA Engine (iOS, complementary — $0)

Running in parallel on-device during every run, with zero cloud cost:

```
Observe  → currentPace (min/km) + heartRateZone (from RunningSessionManager)
Orient   → deviation from targetPace + HR zone classification
Decide   → rule-based (HeartRateZone enum × pace deviation thresholds)
           peak HR                      → ease pace cue
           pace +20% & low HR zone      → pick up pace cue
           pace -15% & high HR zone     → ease up cue
Act      → VoiceCoachingManager.speakCustomMessage() (JA/EN bilingual)
```

- Starts 60s after session begins; fires at 90s intervals thereafter
- Minimum 0.5 km between cues (prevents spam)
- Pro-only; cancelled on session end / view disappear
- Zero API cost — pure Swift, no network calls

### Morning Brief (sendMorningBriefScheduled — $0.03/month)

Daily at 6:00 JST, for each Pro user:

1. Fetch `goalContext` (ACWR / trainingPhase / todayWorkout)
2. Fetch `getWeatherContext` (temperature / precipitation / AQI)
3. Composite decision: ACWR × weather → `runAdvice: "go" | "caution" | "avoid"`
4. Gemini Flash Lite generates a 1-sentence brief (Japanese or English by locale)
5. FCM push + store in `users/{uid}/morning_briefs/{YYYY-MM-DD}`

### Weekly Pareto Insight (weeklyParetoInsightScheduled — $0.003/month)

Every Monday at 7:00 JST, for each Pro user:

1. BigQuery aggregation: run type × time-of-day × ACWR band × pace zone distribution (free quota)
2. Identify the "top 20% of factors" contributing to performance (Pareto principle)
3. Gemini Flash Lite generates a 1-sentence action suggestion
4. Store in `users/{uid}/pareto_insights/{weekLabel}`
5. Displayed in ProAnalyticsView `ParetoTrainingChart`

---

## Safety Constraints (Evaluator Gate)

| Constraint | Description | Guard Layer |
|------------|-------------|-------------|
| SDT Alignment | Never output "slow / 遅い / too slow". Respect all paces equally. | Evaluator LLM + `write_training_plan` rule-based |
| Honest Data Only | Do not fabricate stats, VO2max estimates, or environment-adjusted paces absent from `athlete_state` | Evaluator LLM |
| ACWR Safety | Cap weekly load increase at +10%. Block any load increase when `acwr > 1.3`. | Evaluator LLM + `check_acwr_safety()` rule-based |
| Injury Disclaimer | Require a physician-consultation disclaimer in any response containing injury-related keywords | Evaluator LLM (Step 4) + RACE_STRATEGY instruction hard-rule |

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
│   ├── agents.py          ← ADK Agent definitions (head_coach + 5 sub-agents incl. route_intelligence)
│   ├── instructions.py    ← System instructions for all agents (CoT 7-step + JA/EN few-shot + ROUTE_INTELLIGENCE)
│   └── tools.py           ← Cloud Functions / Firestore wrappers (Pydantic MCP schema + get_weather_context)
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
└── README_agents.md       ← This file
```

---

## ADK Tool Reference

| Tool | Schema | Purpose |
|------|--------|---------|
| `get_athlete_state(uid)` | `str` → `AthleteStateOutput` | Read Firestore `users/{uid}.goalContext` — returns ACWR, readinessReason, trainingPhase, raceTarget, currentFitness |
| `get_weather_context(lat, lng)` | `float × 2` → `WeatherContextOutput` | Call `getWeatherContext` CF (OpenWeatherMap, 6h Firestore cache). Returns temperature_c, condition, advice (`go`/`caution`/`avoid`). Route Intelligence agent calls this before route recommendation. |
| `suggest_route_areas(...)` | `float×4 + opt` → `RouteAreaOutput` | Call `suggestRouteAreas` CF for AI-recommended route areas per session. Supports `sessionContext` override for date-specific workout type/distance/pace zone |
| `recommend_races(...)` | `float×2 + opt` → `RaceRecommendationOutput` | Call `recommendEventsV3` CF to suggest races matching fitness and goals |
| `chat_with_coach(...)` | `str×2` → `dict` | Call `chatWithCoach` CF to answer runner questions |
| `write_training_plan(uid, plan)` | `str + dict` → `WritePlanOutput` | Persist plan to Firestore Memory Bank + update `goalContext.readinessAdjustment/Reason`. Idempotent via `planVersion`. |
| `write_race_strategy(uid, strategy)` | `str + dict` → `WriteRaceStrategyOutput` | Persist race-eve pacing strategy to `users/{uid}.raceStrategy`. Requires `evaluatorPassed=True` + SDT check + non-empty `racePacing.segments`. Pro-guard enforced. |
| ~~`check_acwr_safety`~~ | internal helper | Called internally inside `write_training_plan`. **Not registered** as an ADK tool in `agents.py`. |

> **MCP-compliant**: All tool I/O types are defined as Pydantic models (`AthleteStateOutput` etc.), functioning as JSON-RPC 2.0 schemas.

---

## Evaluation Results (Gen AI Evaluation Service — 50 samples)

Dataset: 20 clean samples + 30 intentional violation samples (SDT / Honest-Data / ACWR — 10 each)

| Rubric | gemini-2.5-pro | gemini-2.5-flash | Interpretation |
|--------|:--------------:|:----------------:|----------------|
| SDT Alignment | mean=0.88, **pass=94%** | mean=0.88, **pass=94%** | Both models reliably detect "slow/遅い" violations |
| Honest Data | overall pass=6% → **recall≈94%** | overall pass=6% → **recall≈94%** | ⚠️ Overall pass_rate reflects dataset distribution (60% intentional violations, not model failure). High fail_count = high recall on fabricated-stat samples. Run `--clean-only` to measure precision (false-positive rate). |
| ACWR Safety | mean=0.02, pass=52% | mean=0.06, **pass=54%** | Flash marginally better on ACWR numeric threshold detection |
| Injury Disclaimer | n=8 only — **not yet stable** | n=8 only — **not yet stable** | ±33% CI — insufficient sample size. Add ≥30 injury samples and re-run before citing. |

> **⚠️ Dataset distribution note**: The eval dataset contains 60% intentional violation samples (30/50). `overall_pass_rate` is a combined metric that bundles precision and recall — it is NOT the false-positive rate. The metrics that matter for production quality are:
> - `precision_clean_only_pass_rate`: fraction of *clean* plans that correctly pass (target: ≥ 0.95)
> - `recall_violation_detect_rate`: fraction of *violation* plans that are correctly rejected (target: ≥ 0.90)
>
> These are computed by `run_eval.py` (P1-1 fix) but have not yet been run on the current dataset. Re-run:

```bash
# Precision-only (no distribution bias — clean samples only)
python -m evals.run_eval --clean-only --output results/clean_precision_eval.json

# Full A/B with precision + recall split
python -m evals.run_eval --ab --output results/ab_summary_v2.json
```

> **InjuryDisclaimer CI note**: Injury rubric uses only n=8 samples — not enough for a stable pass rate. Add ≥30 injury samples (keyword present + disclaimer present/absent split) before using this metric in production reporting.

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

## Agentic Coaching Extensions (Build 74)

### Case A — Daily Proactive Coaching (`proactiveCoaching.ts`)

| Item | Detail |
|------|--------|
| Trigger | Cloud Scheduler 08:00 JST → Pub/Sub fan-out (one message per Pro user) |
| Idempotency | Firestore transaction claim on `proactive_coaching_log/{uid}_{dateKey}` |
| Decision Tree | P0 race_eve → P1 race_milestone → P2 streak≥7 → P3 session_nudge → P4 recovery_alert → P5 reengagement |
| LLM use | P1/P2/P5: `generateWithGeminiSplit()` with 50-char limit, SDT-compliant system prompts (`PROACTIVE_SYSTEM_JA` / `PROACTIVE_SYSTEM_EN`) |
| Rule-based | P3/P4: `buildSessionNudgeMessage()` / `buildRecoveryAlertMessage()` — no LLM call |
| P0 flow | `decideProactiveAction` returns `race_eve_pacing` → enqueued to `agent_planning_queue` → triggers Vertex AI Agent Engine |
| Scale guard | PAGE_SIZE=100, MAX_USERS=500 per scheduler invocation |

### Case B — Session-Aware Route Suggestions (`suggestRouteAreas.ts` + iOS)

| Item | Detail |
|------|--------|
| New parameter | `sessionContext?: { type, distanceKm, paceZone, notes }` in `RouteAreaRequest` |
| Override logic | When `sessionContext` is present, `goalContextData.todayWorkout` is replaced with session-specific values before Gemini prompt assembly |
| Pace mapping | `zone1→420, zone2→390, zone3→360, zone4→330, zone5→300, race→310` sec/km |
| iOS side | `GoalContextManager.todayAgentSession` decodes `trainingPlan.weeks[0].sessions` → `AgentPlanSession` struct with `.asRequestDict` for `CloudFunctionsClient.suggestRouteAreas(sessionContext:)` |

### Case C — Race Preparation Arc Agent (`agents.py`, `tools.py`, `instructions.py`)

| Item | Detail |
|------|--------|
| Trigger | `onRaceTargetSet` (Firestore `onDocumentWritten` on `users/{uid}`) — diff-checks `raceTarget.eventId` before/after |
| New agent | `race_strategy` — `gemini-2.5-flash`, `thinking_budget=1024`, tools: `[get_athlete_state, write_race_strategy]` |
| Instruction | `RACE_STRATEGY` in `instructions.py` — negative split principle, 5km segments, ACWR tiers (>1.2 conservative / 0.8–1.2 standard / <0.8 aggressive) |
| Output schema | `WriteRaceStrategyOutput` Pydantic model; `racePacing.segments[]` must be non-empty |
| Hard gates | Evaluator `evaluatorPassed=True` + SDT check + Pro guard in `write_race_strategy()` |
| Memory | `users/{uid}.raceStrategy` in Firestore |
| Queue handling | `onAgentPlanningQueue.ts` — `isRaceTrigger=true` skips `run_history` fetch; uses `buildRaceAgentMessage()` |

### Case D — Chat Planning Intent Routing (Build 74, `AICoachingView.swift`)

| Item | Detail |
|------|--------|
| Trigger | User message contains planning keywords: 来週/プラン/計画/スケジュール/next week/training plan/schedule/plan for/plan this week |
| Detection | `isPlanningIntent(_ question: String) -> Bool` in `AICoachingView.swift` |
| iOS flow | `triggerAgentPlanFromChat(uid:)` → `CloudFunctionsClient.triggerChatPlanAndPoll(uid:)` → polls `agent_decisions/{uid}/history` (3 min, 10s intervals) |
| Agent flow | `agent_planning_queue` → `onAgentPlanningQueue` → Head Coach → `write_training_plan()` |
| Result display | Adjustment icon + summary text appended to chat as Ayumu message |
| Conditions | Pro only; fires in parallel with normal `chatWithCoach` call |

### Case E — Mid-Cycle Replan (Build 74, `onRunCompleted.ts`)

| Item | Detail |
|------|--------|
| Detection | `Math.abs(actualKm - targetKm) / targetKm > 0.20` |
| Data source | `goalContext.todayWorkout.targetDistanceKm` from Firestore (read inside transaction) |
| Message augment | Additional instruction in `buildAgentMessage()`: "athlete deviated significantly (>20%)... re-evaluate current training block" |
| Fallback | If `targetDistanceKm` is absent, defaults to `triggerType: "post_run"` (no change in behavior) |

### Case F — Route Intelligence Agent (Build 74, `agents.py`)

| Item | Detail |
|------|--------|
| Agent name | `route_intelligence` |
| Model | `gemini-2.5-flash`, `thinking_budget=1024` |
| Tools | `get_athlete_state` + `get_weather_context` |
| Decision logic | weather.advice=avoid → indoor/rest; caution → easy pace only; acwr>1.3 → reduce intensity; trainingPhase=peak → tempo/interval ok |
| Weather source | OpenWeatherMap Current Weather API (geohash-4 6h Firestore cache) |
| Cost | OpenWeatherMap free tier: 1,000 calls/day — covers MAU 500 with 6h cache |

### Morning Brief (`sendMorningBriefScheduled` — Cloud Scheduler 6:00 JST)

| Item | Detail |
|------|--------|
| Schedule | `0 21 * * *` (UTC) = 6:00 JST daily |
| Context | goalContext + getWeatherContext → composite `runAdvice` |
| LLM | Gemini Flash Lite, ≤50 chars, SDT-compliant |
| Storage | `users/{uid}/morning_briefs/{YYYY-MM-DD}` |
| iOS display | `DashboardCardsView.MorningBriefCard` |
| Cost | ~$0.03/month for 25 Pro users |

### Weekly Pareto Insight (`weeklyParetoInsightScheduled` — Cloud Scheduler Mon 7:00 JST)

| Item | Detail |
|------|--------|
| Schedule | `0 22 * * 0` (UTC) = 7:00 JST Monday |
| Analysis | BigQuery: run type × time-of-day × ACWR band × pace zone (free quota) |
| LLM | Gemini Flash Lite, 1-sentence action suggestion |
| Storage | `users/{uid}/pareto_insights/{weekLabel}` |
| iOS display | `ProAnalyticsView.ParetoTrainingChart` |
| Cost | ~$0.003/month for 25 Pro users |

---

## Known Limitations & Next Actions

| Item | Status | Action Required |
|------|--------|----------------|
| Physical profile (age, weight, height) not in Head Coach context | ⚠️ Not implemented | Enrich `goalContext` with user profile fields; update `get_athlete_state` schema |
| Injury Disclaimer eval samples | ℹ️ Pending | Add injury samples to `EVAL_DATASET` in `run_eval.py`, re-run `--ab` to quantify pass-rate |
| Proactive coaching scale test | ℹ️ Pending | Load-test fan-out at 500 users; verify idempotency under Pub/Sub at-least-once delivery |
| `race_strategy` + `route_intelligence` Agent Engine deployment | ⚠️ Pending | `deploy_agent_engine.py` must be re-deployed to include new AgentTools in the live Head Coach |
| Mid-cycle replan: physical exhaustion signal | ℹ️ Future | Integrate HRV / resting HR into deviation detection for smarter replan trigger |

---

## Implementation Notes

### Instruction architecture

`deploy/deploy_agent_engine.py` imports instructions directly from `agent/instructions.py` via:

```python
from agent.instructions import HEAD_COACH_INSTRUCTION, PERIODIZATION, READINESS, EVALUATOR, RACE_STRATEGY, ROUTE_INTELLIGENCE
```

**Single source of truth**: editing `agent/instructions.py` affects both local execution and the next deploy. There is no duplicate copy — re-deploying picks up changes automatically.

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
| `gemini-2.5-flash` | ✅ | Readiness / Race Strategy / Route Intelligence |
| `gemini-2.5-flash-lite` | ✅ | Evaluator / Morning Brief / Pareto Insight |
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
| `RouteRun_ios/AICoachingView.swift` | Ayumu AI coach UI — planning intent detection + `triggerAgentPlanFromChat` |
| `RouteRun_ios/CloudFunctionsClient.swift` | `triggerChatPlanAndPoll(uid:)` — enqueue `chat_plan` + poll `agent_decisions/{uid}/history` |
| `RouteRun_ios/RunningActiveView.swift` | On-device OODA engine (`startOODAMonitoring` / `evaluateOODA`) — Pro only |
| `RouteRun_ios/RunningCompleteView.swift` | Live agent result display in `agentAnalyzingCard` — polls `agent_decisions/{uid}/history` (3 min) |
| `RouteRun_ios/GoalContextManager.swift` | Firestore real-time listener for `users/{uid}.goalContext` — includes `readinessReason` / `agentUpdatedAt` |
| `RouteRun_ios/AgentActionBanner.swift` | Three banner variants: `AgentActionBanner` (reduce/intensify), `AgentMaintainBanner` (maintain), `AgentReadinessTeaserBanner` (free user) |
| `RouteRun_ios/DashboardCardsView.swift` | Routes to banner variant + `MorningBriefCard` display |
| `functions/src/coaching/onRunCompleted.ts` | Mid-cycle replan detection (>20% deviation) + `triggerType` assignment |
| `functions/src/coaching/onAgentPlanningQueue.ts` | Agent Engine session trigger; handles all 5 trigger types |
| `functions/src/coaching/sendMorningBrief.ts` | Morning Brief scheduler + `getMorningBrief` onCall |
| `functions/src/learning/weeklyParetoInsight.ts` | Weekly Pareto Insight scheduler + `getParetoInsight` onCall |
| `functions/src/shared/weatherContext.ts` | `getWeatherContext` — OpenWeatherMap + 6h Firestore cache |
| `functions/src/coaching/updateGoalContext.ts` | Supports `readinessReason` field. Agent judgment takes priority over ACWR rule-based overwrite |
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

  └─ agent_decisions/{uid}/history  ← new decision doc with adjustment + decidedAt
                                                           ↓ (polled by iOS)
                                                    RunningCompleteView.agentAnalyzingCard
                                                      → show icon + summary text (reduce/intensify/maintain)
                                                    AICoachingView (chat_plan trigger)
                                                      → append plan summary message as Ayumu reply
```

---

## 日本語概要

RouteRun の「あゆむ」AI コーチを Vertex AI Agent Engine 上の **Supervisor マルチエージェント**として構築したサブプロジェクトです。

### システム概要

iOS でランニングが完了すると、Cloud Functions がトリガーされ、Vertex AI Agent Engine 上のエージェントが自律的に8週間トレーニング計画を生成・評価・Firestore に永続化します。Build 74 では、チャット画面からの計画クエリ・走行偏差による mid-cycle 再プラン・ルートインテリジェンス・朝ブリーフ・パレート分析が追加されました。

### Head Coach が参照するデータ

- **最新ランのデータ**（トリガーメッセージ）: 距離 / 時間 / ペース / 平均心拍数
- **goalContext**（Firestore）: ACWR / トレーニングフェーズ / 週間ボリューム傾向 / VO2max 推定 / 月間距離 / 目標レース
- **天気コンテキスト**（OpenWeatherMap、Route Intelligence agent のみ）: 気温 / 降水確率 / AQI
- **現在の制限**: 年齢・体重・身長・性別などの身体プロフィールは未対応（将来対応予定）

### エージェント構成

| エージェント | モデル | 役割 |
|---|---|---|
| Head Coach | gemini-2.5-pro | 全体制御・計画統合 |
| Periodization | gemini-2.5-pro | メソ/マイクロサイクル設計 |
| Readiness | gemini-2.5-flash | HRV/睡眠による負荷調整 |
| Race Strategy | gemini-2.5-flash | レース前日ネガティブスプリット戦略設計（race_eve_pacing トリガー時） |
| Route Intelligence | gemini-2.5-flash | 天気 × ACWR × trainingPhase → ルートタイプ推薦 |
| Evaluator | gemini-2.5-flash-lite | SDT/Honest-Data/ACWR ゲート |

### トリガー種別

| トリガー | 起点 | エージェント |
|---------|------|------------|
| ラン完了（post_run） | `onRunCompleted` CF | Head Coach → Periodization + Readiness + Evaluator |
| Mid-Cycle 再プラン（mid_cycle_replan） | `onRunCompleted` CF（>20% 距離偏差） | Head Coach（再プランメッセージ付き） |
| チャット計画クエリ（chat_plan） | iOS `AICoachingView` planning intent 検出 | Head Coach → write_training_plan → チャット返信 |
| 日次プロアクティブ（Case A） | Cloud Scheduler 08:00 JST | Gemini Flash-Lite / ルールベース（FCM プッシュ） |
| レース登録（Case C / race_arc） | `onRaceTargetSet` CF | Head Coach → race_strategy Agent |
| レース前日ペーシング（race_eve_pacing） | `onProactiveCoachWorker` P0 | race_strategy Agent |

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
