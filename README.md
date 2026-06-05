# RouteRun Agentic Coaching

**Google for Startups AI Agents Challenge — 2026 提出物**

RouteRun の「あゆむ」AI コーチを、Vertex AI Agent Engine 上の **Supervisor マルチエージェント**として再構築したサブプロジェクト。iOS アプリのランニング完了イベントをトリガーとして自律的に計画を立案・評価・永続化する。

---

## ハイライト

| 項目 | 値 |
|------|----|
| Agent Engine | `projects/231451951695/locations/asia-northeast1/reasoningEngines/1418026952203173888` |
| Head Coach モデル | `gemini-2.5-pro` (thinking_budget=2048) |
| Periodization モデル | `gemini-2.5-pro` (thinking_budget=4096) |
| Readiness モデル | `gemini-2.5-flash` (thinking_budget=1024) |
| Evaluator モデル | `gemini-2.5-flash-lite` (thinking 不要、パターンマッチ特化) |
| パターン | B (Supervisor マルチエージェント) + D (In-loop Evaluator) |
| Eval rubrics | SDT Alignment / Honest Data / ACWR Safety / Injury Disclaimer (4本) |
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
   │  if (isPro) → 1日1回 transaction rate limit
   │  → agent_planning_queue.add({ uid, runId, status: "pending" })
   ▼
onAgentPlanningQueue (Cloud Functions v2 / Firestore trigger)
   │  GCP Metadata Server 認証 (ADC) → access_token 取得
   │  → Vertex AI Agent Engine: セッション作成 → streamQuery 実行
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
   ├─ AgentTool: evaluator  [gemini-2.5-flash-lite]       │
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

| 制約 | 内容 | ガード層 |
|------|------|---------|
| SDT Alignment | 「slow / 遅い / 遅すぎ」を出力しない。すべてのペースを等しく尊重する | Evaluator LLM + `write_training_plan` rule-based |
| Honest Data Only | `athlete_state` 入力に存在しない統計値・VO2max 推定・環境補正ペースを創作しない | Evaluator LLM |
| ACWR Safety | 週次負荷増加を +10% 以内に制限。`acwr > 1.3` のときは負荷増加を禁止 | Evaluator LLM + `check_acwr_safety()` rule-based |
| Injury Disclaimer | 怪我関連キーワードを含む応答には医師への相談を促す免責事項を必ず付与する | **eval rubric のみ**（Agent Gate 対象外） |

### Evaluator Few-Shot 言語対応

Evaluator には JA / EN 両言語の Few-Shot 例（計6例）を提供。英語ユーザーが英語で質問した場合も正確にパターンマッチを実行する。

| # | 言語 | 判定 | 違反種別 |
|---|------|------|---------|
| 1 | JA | PASS | なし |
| 2 | JA | FAIL | SDT（遅すぎ） |
| 3 | JA | FAIL | ACWR 超過 |
| 4 | EN | PASS | なし |
| 5 | EN | FAIL | Honest-Data（VO2max 捏造） |
| 6 | EN | FAIL | SDT + ACWR 複合違反 |

---

## ディレクトリ構成

```
routerun-agent/
├── agent/
│   ├── __init__.py
│   ├── agents.py          ← ADK Agent 定義 (head_coach + sub-agents)
│   ├── instructions.py    ← 全エージェントのシステムインストラクション (CoT 7ステップ + JA/EN Few-Shot)
│   └── tools.py           ← Cloud Functions / Firestore ラッパー (Pydantic MCP スキーマ付き)
├── deploy/
│   ├── deploy_agent_engine.py  ← Agent Engine デプロイ + Secret Manager 自動保存
│   └── monitoring_alert.yaml   ← PubSub DLQ + CF エラー率 Cloud Monitoring アラート
├── demo/
│   ├── seed_demo_data.py  ← Firebase Auth ユーザー作成 + Firestore シードデータ
│   └── run_timeline.py    ← 8週圧縮タイムラインデモ
├── evals/
│   ├── rubrics.py         ← カスタム PointwiseMetric 定義 (4 rubrics: SDT / Honest Data / ACWR Safety / Injury Disclaimer)
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

| ツール | 入力スキーマ | 用途 |
|--------|------------|------|
| `get_athlete_state(uid)` | `str` → `AthleteStateOutput` | Firestore `users/{uid}.goalContext` を読み、ACWR / readinessReason / trainingPhase / raceTarget 等を返す |
| `suggest_route_areas(...)` | `float×4 + opt` → `RouteAreaOutput` | `suggestRouteAreas` CF を呼び、セッション向けルートエリアを AI 推薦 |
| `recommend_races(...)` | `float×2 + opt` → `RaceRecommendationOutput` | `recommendEventsV3` CF を呼び、体力・目標に合うレースを推薦 |
| `chat_with_coach(...)` | `str×2` → `dict` | `chatWithCoach` CF を呼び、走者の質問に応答 |
| `write_training_plan(uid, plan)` | `str + dict` → `WritePlanOutput` | Firestore Memory Bank + goalContext.readinessAdjustment/Reason を同時更新。planVersion で冪等性保証 |
| ~~`check_acwr_safety`~~ | 内部ヘルパー | `write_training_plan` 内から内部呼び出し。ADK ツールとして Agent には公開されない（`agents.py` の tools list に未登録） |

> **MCP 準拠**: 全ツールの入出力型を Pydantic モデル (`AthleteStateOutput` 等) で定義。JSON-RPC 2.0 スキーマとして機能する。

---

## iOS オンデバイス Agentic Loop（Build 70 追加）

Vertex AI Agent Engine（クラウド）に加えて、iOS アプリ内でも **オンデバイス Agentic Loop** を実装。

```
ユーザー質問
   ↓
classifyAgentIntent()  — 5種別に分類
   ↓
buildAgentToolContext() — HealthKit / Firestore をオンデバイスで自律取得
  ├─ .healthData    → VO2max / HRV / 回復スコア / 睡眠時間・品質
  ├─ .runHistory    → 走行履歴 / ペース / CTL / ATL
  ├─ .racePreparation → レース名 / 残日数 / フェーズ
  ├─ .injuryPrevention → HRV 低下警告 / 回復スコア不足
  └─ .general       → ツール不要（汎用）
   ↓
enrichedForLLM  →  Foundation Models (Layer 1) or Gemini (Layer 2)
```

**睡眠データ統合（Build 70）:**  
`BiodataContext.sleepDurationHours / sleepQualityScore` が `get_athlete_state` 相当の情報として Readiness Agent の入力と整合する。iOS オンデバイス Readiness 判定（睡眠不足検出・6h未満警告・深睡眠30%未満警告）が Vertex AI Readiness Agent の判定ロジックと二重構造で安全性を確保。

---

## 評価結果 (Gen AI Evaluation Service — 50 サンプル)

評価データセット：クリーンサンプル 20 件 + 意図的違反サンプル 30 件（SDT/Honest-Data/ACWR 各 10 件）

| Rubric | gemini-2.5-pro | gemini-2.5-flash | 解釈 |
|--------|:--------------:|:----------------:|------|
| SDT Alignment | mean=0.88, pass=94% | mean=0.88, pass=94% | 「遅い」系言語を両モデルが高精度で検出 |
| Honest Data | mean=-0.88, pass=6% | mean=-0.88, pass=6% | 捏造統計を含む違反サンプルを正確に検出 (低 pass rate = 高感度) |
| ACWR Safety | mean=0.02, pass=52% | mean=0.06, pass=54% | ACWR 超過を過半数検出。Flash が微差で優位 |
| Injury Disclaimer | — | — | 怪我関連キーワード含む応答に免責事項を要求 |

> **Precision / Recall 分離評価**: `run_eval.py` は全体 pass_rate に加え `precision_clean_only_pass_rate`（クリーンサンプルの誤検知率の逆数）と `recall_violation_detect_rate`（違反サンプルの検出率）を分離計測。`--clean-only` フラグで精度専用評価が実行可能。

```bash
# 全体 A/B 評価
python -m evals.run_eval --ab

# precision 専用評価（分布バイアスなし、高速）
python -m evals.run_eval --clean-only
```

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

# deploy_agent_engine.py が自動で Secret Manager に保存する
# (デプロイ後に手動更新が不要になった)
AGENT_ENGINE_RESOURCE_NAME=projects/231451951695/locations/asia-northeast1/reasoningEngines/1418026952203173888

# Secret Manager のシークレット名（デフォルト: AGENT_ENGINE_RESOURCE_NAME）
# AGENT_ENGINE_SECRET_NAME=AGENT_ENGINE_RESOURCE_NAME
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
# resource_name は Secret Manager に自動保存される
# projects/runroute-476505/secrets/AGENT_ENGINE_RESOURCE_NAME

# Secret Manager への保存が失敗した場合は手動で .env を更新:
# AGENT_ENGINE_RESOURCE_NAME=projects/.../reasoningEngines/...
```

### 3. PubSub DLQ 監視アラートのデプロイ（初回のみ）

```bash
# 通知チャネル ID を取得
gcloud alpha monitoring channels list --project=runroute-476505

# monitoring_alert.yaml 内の REPLACE_WITH_CHANNEL_ID を実際の ID に置換後:
gcloud alpha monitoring policies create \
  --policy-from-file=deploy/monitoring_alert.yaml
```

### 4. 圧縮タイムラインデモ（8週再現）

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

### 5. A/B 評価

```bash
python -m evals.run_eval --ab --output results/ab_summary.json
```

### 6. デモデータ削除（クリーンアップ）

```bash
python -m demo.seed_demo_data --cleanup
```

`isDemo: true` フラグ付きドキュメント（`run_history`, `agent_planning_queue`, `trainingPlan`, `goalContext`）を全削除する。

---

## デモビデオ収録チェックリスト

| # | 画面 | 確認内容 |
|---|------|---------|
| 1 | iOS シミュレータ | Pro ユーザーでランニング完了 → `onRunCompleted` CF が 1日1回 rate limit チェック → `agent_planning_queue` に enqueue → `onAgentPlanningQueue` CF が Vertex AI Agent セッションを自動起動 |
| 2 | ターミナル | `run_timeline` 実行 → Week 3 `★ PERTURBATION ★` 表示・`readinessAdjustment: reduce` |
| 3 | Firestore Console | `users/test_sub4.trainingPlan` の `evaluatorPassed: true` + `planVersion` 冪等キー |
| 4 | Firestore Console | `users/test_sub4.goalContext.currentFitness.readinessReason` にエージェント判断理由が到達していること |
| 5 | iOS ダッシュボード | `AgentActionBanner` に動的な `readinessReason`（エージェントの実際の判断理由）が表示されること |
| 6 | iOS ダッシュボード | `maintain` 状態時に `AgentMaintainBanner` が表示されてエージェントが常に動作していることを確認 |
| 7 | Vertex AI Console | Trace Viewer で Head Coach → Periodization → Readiness → Evaluator 呼び出しグラフ |
| 8 | ターミナル | `ab_summary.json` の SDT 94% pass rate + precision/recall 分離メトリクス |

Vertex AI Trace Viewer URL:
```
https://console.cloud.google.com/vertex-ai/agents?project=runroute-476505
```

---

## 既知の制限と今後のアクション

| 項目 | 状態 | 対応 |
|------|------|------|
| Agent Engine `write_training_plan` — Pro ユーザー対応 | ⚠️ **未対応** | `deploy_agent_engine.py` の `write_training_plan` は `test_` UID と `isDemo=true` のみ許可。`tools.py` の Pro 対応版（`_is_pro_user()` チェック）はローカル開発専用。CF 側（`onRunCompleted.ts`）は全 Pro ユーザーをキューに追加するが、Agent Engine 側が拒否する。**再デプロイ前に `deploy_agent_engine.py` の `write_training_plan` を `tools.py` と同期すること** |
| Injury Disclaimer — Agent Gate 実装 | ⚠️ **未実装** | 現在は eval rubric（`rubrics.py`）のみ。Evaluator の instruction（Steps 1-3）には含まれていない。本番適用するには EVALUATOR に Step 4 を追加後、Agent Engine を再デプロイ |
| eval サンプル — Injury Disclaimer 未実施 | ℹ️ 未実行 | `run_eval.py` の EVAL_DATASET に injury 関連サンプルを追加後、`--ab` で再計測が必要 |

---

## 実装ポイント（技術的判断記録）

### instructions.py と deploy_agent_engine.py の2コピー体制

| ファイル | 用途 | 特徴 |
|---------|------|------|
| `agent/instructions.py` | `agent/agents.py` がローカル実行時に参照 | CoT 7ステップのタイトル付き。Pydantic 出力スキーマあり |
| `deploy/deploy_agent_engine.py` | Agent Engine にデプロイされる実体 | cloudpickle 制約でインライン定義。instructions は簡略版（動作ループ形式） |

`instructions.py` を更新しても Agent Engine には反映されない。変更を本番に適用するには `deploy_agent_engine.py` も更新してから再デプロイが必要。

### cloudpickle による `No module named 'agent'` 問題と解決策 `No module named 'agent'` 問題と解決策

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
| `RouteRun_ios/GoalContextManager.swift` | `users/{uid}.goalContext` リアルタイムリスナー。`readinessReason` / `agentUpdatedAt` フィールドを含む |
| `RouteRun_ios/AgentActionBanner.swift` | `AgentActionBanner` (reduce/intensify)・`AgentMaintainBanner` (maintain)・`AgentReadinessTeaserBanner` (free) の3バリアント |
| `RouteRun_ios/DashboardCardsView.swift` | エージェント状態に応じたバナーのルーティング（readinessAdjustment 値で3バリアントを切り替え） |
| `functions/src/coaching/onRunCompleted.ts` | run完了トリガー。全 Pro ユーザー対象（DEMO_UIDS 廃止）。`if (isPro)` → 1日1回 transaction rate limit (`usage_counters/{uid}/daily/{dateKey}.agentPlanCount`) → `agent_planning_queue` に enqueue |
| `functions/src/coaching/onAgentPlanningQueue.ts` | `agent_planning_queue/{docId}` 作成で起動。GCP Metadata Server 認証（追加 npm パッケージ不要）→ Vertex AI Agent Engine セッション作成 → `streamQuery` 実行 → ドキュメントを `completed`/`failed` に更新 |
| `functions/src/coaching/updateGoalContext.ts` | `readinessReason` フィールド対応。`agentUpdatedAt` 存在時はエージェント判断を優先し ACWR ルールベースで上書きしない |
| `RouteRun_ios/TrainingPlanManager.swift` | `users/{uid}.trainingPlan` 読み取り・UI 表示 |

---

## エージェント出力 → iOS 表示フロー（P0-1 修正後）

```
Vertex AI Agent Engine
  └─ write_training_plan(uid, plan)
       ├─ users/{uid}.trainingPlan  ← 計画を永続化
       └─ users/{uid}.goalContext.currentFitness  ← readinessAdjustment + readinessReason + agentUpdatedAt を書き込み
                                                           ↓ (Firestore リアルタイムリスナー)
                                                    GoalContextManager (iOS)
                                                           ↓
                                                    DashboardCardsView
                                                      reduce/intensify → AgentActionBanner(reason: "HRV が...")
                                                      maintain        → AgentMaintainBanner(reason: "全指標正常...")
                                                      free user       → AgentReadinessTeaserBanner
```

> **修正前の問題**: `updateGoalContext.ts` の `acwrToReadinessAdjustment()` がルールベース値のみを書き込んでいたため、エージェントの HRV/睡眠マルチ変量判断は Firestore に届かず iOS に表示されなかった。`write_training_plan` が `goalContext` も更新することで初めてエンドツーエンドで接続された。
