# RouteRun Agentic Coaching

**Google for Startups AI Agents Challenge — 2026 提出物**

RouteRun の「あゆむ」AI コーチを、Vertex AI Agent Engine 上の **Supervisor マルチエージェント**として再構築したサブプロジェクト。iOS アプリのランニング完了イベントをトリガーとして自律的に計画を立案・評価・永続化する。

---

## ハイライト

| 項目 | 値 |
|------|----|
| Agent Engine | `projects/231451951695/locations/asia-northeast1/reasoningEngines/3999011751151534080` |
| Head Coach モデル | `gemini-2.5-pro` (thinking_budget=2048) |
| Periodization モデル | `gemini-2.5-pro` (thinking_budget=4096) |
| Readiness モデル | `gemini-2.5-flash` (thinking_budget=1024) |
| Evaluator モデル | `gemini-2.5-flash-lite` (thinking 不要、パターンマッチ特化) |
| パターン | B (Supervisor マルチエージェント) + D (In-loop Evaluator) |
| Eval rubrics | SDT Alignment / Honest Data / ACWR Safety |
| SDT pass rate | 94% (gemini-2.5-pro / gemini-2.5-flash 同等) |
| Memory Bank | Firestore `users/{uid}.trainingPlan` |
| デモユーザー | `test_sub4` (サブ4目標) / `test_firstfull` (初フル完走目標) |

---

## アーキテクチャ

```
iOS App (RunningActiveView)
   │  ← run完了イベント
   ▼
onRunCompleted (Cloud Functions v2 / Firestore trigger)
   │  DEMO_UIDS allowlist → agent_planning_queue に enqueue
   ▼
Vertex AI Agent Engine  ←───────────────────────────────┐
   │                                                      │
   ▼  [Head Coach — gemini-2.5-pro]                      │
   ├─ get_athlete_state()  ← Firestore goalContext        │
   │                                                      │
   ├─ AgentTool: periodization  [gemini-2.5-pro]          │
   │   └─ get_athlete_state() → 週次メソサイクル設計      │
   │                                                      │
   ├─ AgentTool: readiness  [gemini-2.5-flash + thinking] │
   │   └─ get_athlete_state() → HRV/睡眠評価 → adjust    │
   │                                                      │
   ├─ AgentTool: evaluator  [gemini-2.5-flash]            │
   │   └─ SDT / Honest-Data / ACWR gate → PASS/FAIL       │
   │                                                      │
   └─ write_training_plan()  → Firestore Memory Bank ─────┘
         users/{uid}.trainingPlan
```

### エージェント役割分担

| Agent | モデル | Thinking Budget | 役割 |
|-------|--------|----------------|------|
| **Head Coach** (`ayumu_head_coach`) | gemini-2.5-pro | 2048 | 全体ループ制御・計画統合・最終出力 |
| **Periodization** | gemini-2.5-pro | 4096 | レース目標に向けたメソ/マイクロサイクル構築 |
| **Readiness** | gemini-2.5-flash | 1024 | HRV / 睡眠 / 安静時 HR から負荷調整判定 |
| **Evaluator** | gemini-2.5-flash-lite | 0 | SDT / Honest-Data / ACWR パターンマッチゲート |

---

## 絶対制約（Evaluator Gate）

| 制約 | 内容 |
|------|------|
| SDT Alignment | 「slow / 遅い / 遅すぎ」を出力しない。すべてのペースを等しく尊重する |
| Honest Data Only | `athlete_state` 入力に存在しない統計値・VO2max 推定・環境補正ペースを創作しない |
| ACWR Safety | 週次負荷増加を +10% 以内に制限。`acwr > 1.3` のときは負荷増加を禁止 |

---

## ディレクトリ構成

```
routerun-agent/
├── agent/
│   ├── __init__.py
│   ├── agents.py          ← ADK Agent 定義 (head_coach + sub-agents)
│   ├── instructions.py    ← 全エージェントのシステムインストラクション
│   └── tools.py           ← Cloud Functions / Firestore ラッパー (5 ツール)
├── deploy/
│   └── deploy_agent_engine.py  ← Agent Engine デプロイスクリプト
├── demo/
│   ├── seed_demo_data.py  ← Firebase Auth ユーザー作成 + Firestore シードデータ
│   └── run_timeline.py    ← 8週圧縮タイムラインデモ
├── evals/
│   ├── rubrics.py         ← カスタム PointwiseMetric 定義 (3 rubrics)
│   └── run_eval.py        ← Gen AI Evaluation Service A/B 比較
├── results/
│   ├── ab_summary.json    ← A/B eval 結果 (gemini-2.5-pro vs gemini-2.5-flash)
│   ├── planner_eval.json
│   └── fast_eval.json
├── logs/
│   └── timeline_test_sub4_*.json  ← 8週タイムライン実行ログ
├── pyproject.toml
├── .env                   ← AGENT_ENGINE_RESOURCE_NAME 等
└── README.md
```

---

## ADK ツール一覧

| ツール | 用途 |
|--------|------|
| `get_athlete_state(uid)` | Firestore `users/{uid}.goalContext` を読み、ACWR / trainingPhase / raceTarget 等を返す |
| `suggest_route_areas(...)` | `suggestRouteAreas` CF を呼び、セッション向けルートエリアを AI 推薦 |
| `recommend_races(...)` | `recommendEventsV3` CF を呼び、体力・目標に合うレースを推薦 |
| `chat_with_coach(...)` | `chatWithCoach` CF を呼び、走者の質問に応答 |
| `write_training_plan(uid, plan)` | Firestore Memory Bank (`users/{uid}.trainingPlan`) に計画を永続化 |

---

## 評価結果 (Gen AI Evaluation Service — 50 サンプル)

評価データセット：クリーンサンプル 20 件 + 意図的違反サンプル 30 件（SDT/Honest-Data/ACWR 各 10 件）

| Rubric | gemini-2.5-pro | gemini-2.5-flash | 解釈 |
|--------|:--------------:|:----------------:|------|
| SDT Alignment | mean=0.88, pass=94% | mean=0.88, pass=94% | 「遅い」系言語を両モデルが高精度で検出 |
| Honest Data | mean=-0.88, pass=6% | mean=-0.88, pass=6% | 捏造統計を含む違反サンプルを正確に検出 (低 pass rate = 高感度) |
| ACWR Safety | mean=0.02, pass=52% | mean=0.06, pass=54% | ACWR 超過を過半数検出。Flash が微差で優位 |

> `pass_rate` はテストデータセット全体（違反込み）に対する割合。意図的違反サンプルを含むため、低 pass rate は Evaluator の高検出精度を示す。

---

## セットアップ

### 前提条件

- Python 3.10+
- Google Cloud SDK (`gcloud auth application-default login`)
- Firebase Admin SDK Service Account (または ADC)
- Vertex AI API / Agent Engine API 有効化済み

```bash
cd routerun-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 環境変数 (`.env`)

```env
GOOGLE_CLOUD_PROJECT=runroute-476505
GOOGLE_CLOUD_REGION=asia-northeast1
FIREBASE_DATABASE_ID=routerun-db
STAGING_BUCKET=gs://runroute-476505-agent-staging

PLANNER_MODEL=gemini-2.5-pro
FAST_MODEL=gemini-2.5-flash

AGENT_ENGINE_RESOURCE_NAME=projects/231451951695/locations/asia-northeast1/reasoningEngines/3999011751151534080
```

---

## デモ実行手順

### 1. シードデータ投入（初回のみ）

```bash
python -m demo.seed_demo_data
# オプション:
#   --dry-run    実際の書き込みなしで確認
#   --cleanup    作成したテストデータを削除
#   --get-tokens Firebase IDトークン取得
```

### 2. Agent Engine デプロイ（再デプロイ時）

```bash
python -m deploy.deploy_agent_engine
# 完了後 .env の AGENT_ENGINE_RESOURCE_NAME を更新
```

### 3. 圧縮タイムラインデモ（8週再現）

```bash
# Agent Engine を使用（本番デモ）
AGENT_ENGINE_RESOURCE_NAME=<resource_name> python -m demo.run_timeline --uid test_sub4

# ローカル ADK を使用（開発用）
python -m demo.run_timeline --uid test_sub4 --local
```

Week 3 到達時に以下の出力が表示される：

```
============================================================
★ PERTURBATION DETECTED — RE-PLAN IN PROGRESS ★
  Week 3: HRV 低下 / HR上昇 / ロング走スキップ → Readiness: reduce
============================================================
```

### 4. A/B 評価

```bash
python -m evals.run_eval --ab --output results/ab_summary.json
```

### 5. デモデータ削除（クリーンアップ）

```bash
python -m demo.seed_demo_data --cleanup
```

`isDemo: true` フラグ付きドキュメント（`run_history`, `agent_planning_queue`, `trainingPlan`, `goalContext`）を全削除する。

---

## デモビデオ収録チェックリスト

| # | 画面 | 確認内容 |
|---|------|---------|
| 1 | iOS シミュレータ | ランニング完了 → `onRunCompleted` CF が `agent_planning_queue` に enqueue |
| 2 | ターミナル | `run_timeline` 実行 → Week 3 `★ PERTURBATION ★` 表示・`readinessAdjustment: reduce` |
| 3 | Firestore Console | `users/test_sub4.trainingPlan` の `evaluatorPassed: true` |
| 4 | Vertex AI Console | Trace Viewer で Head Coach → Periodization → Readiness → Evaluator 呼び出しグラフ |
| 5 | ターミナル | `ab_summary.json` の SDT 94% pass rate |

Vertex AI Trace Viewer URL:
```
https://console.cloud.google.com/vertex-ai/agents?project=runroute-476505
```

---

## 実装ポイント（技術的判断記録）

### cloudpickle による `No module named 'agent'` 問題と解決策

cloudpickle はトップレベルモジュール関数を **モジュール参照（module + qualname）** でシリアライズする。`agent_engines.create()` が生成する `agent_engine.pkl` をリモートコンテナがロードする際、`agent` パッケージが未インストールだと `ModuleNotFoundError` で起動失敗する。

**解決策**: すべてのツール関数を `deploy()` 関数のローカルスコープで定義する。cloudpickle はローカル関数（クロージャ）を **バイトコード埋め込み** でシリアライズするため、リモートコンテナに `agent` パッケージ不要。

```python
def deploy():
    # ローカル定義 → cloudpickle がバイトコードをインライン化
    def get_athlete_state(uid: str) -> dict:
        ...
    def write_training_plan(uid: str, plan: dict) -> bool:
        ...
    head_coach = Agent(name="ayumu_head_coach", tools=[get_athlete_state, ...])
    agent_engines.create(agent_engine=head_coach, ...)  # extra_packages 不要
```

### モデル可用性（asia-northeast1）

| モデル ID | asia-northeast1 | 備考 |
|-----------|:--------------:|------|
| `gemini-2.5-pro` | ✅ | Head Coach / Periodization に採用 |
| `gemini-2.5-flash` | ✅ | Readiness / Evaluator に採用 |
| `gemini-3.1-pro-preview` | ❌ | 存在しない (プラン当初記載のモデル ID) |
| `gemini-3.1-flash-lite` | ❌ | 存在しない |

### IAM 設定（Agent Engine → Firestore）

Agent Engine の実行サービスアカウント (`service-231451951695@gcp-sa-aiplatform-re.iam.gserviceaccount.com`) に以下を付与：

- `roles/datastore.user`
- `roles/firebase.viewer`

---

## Trace Viewer

Vertex AI Console でエージェント実行トレース（ツール呼び出し順・レイテンシ・トークン数）を確認できる：

```
https://console.cloud.google.com/vertex-ai/agents?project=runroute-476505
```

---

## 関連ファイル（iOS アプリ側）

| ファイル | 役割 |
|---------|------|
| `RouteRun_ios/AICoachingView.swift` | あゆむ AI コーチ UI（既存コーチング画面） |
| `RouteRun_ios/GoalContextManager.swift` | `users/{uid}.goalContext` リアルタイムリスナー |
| `functions/src/coaching/onRunCompleted.ts` | run完了トリガー + DEMO_UIDS agent_planning_queue enqueue |
| `RouteRun_ios/TrainingPlanManager.swift` | `users/{uid}.trainingPlan` 読み取り・UI 表示 |
