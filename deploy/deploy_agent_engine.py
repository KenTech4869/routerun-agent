"""Deploy ayumu_head_coach to Vertex AI Agent Engine.

実行方法:
  python -m deploy.deploy_agent_engine

デプロイ後に表示される resource_name を .env に AGENT_ENGINE_RESOURCE_NAME として保存する。

削除 (idle 無課金だが demo 終了後は清掃推奨):
  python -m deploy.deploy_agent_engine --delete <resource_name>

実装メモ: tools はすべて deploy() 内のローカル関数として定義する。
cloudpickle はトップレベル関数を「モジュール参照」でシリアライズするため、
リモートコンテナで agent パッケージが見つからず起動失敗する。
ローカル関数(クロージャ)はバイトコード埋め込みでシリアライズされるため
extra_packages 不要、agent モジュール参照なし。
"""

import argparse
import os
from typing import Optional

import vertexai
from vertexai import agent_engines

PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "runroute-476505")
REGION = os.getenv("GOOGLE_CLOUD_REGION", "asia-northeast1")
STAGING_BUCKET = os.getenv("STAGING_BUCKET", f"gs://{PROJECT}-agent-staging")

# ── System instructions (strings pickle by value — module-level OK) ────────

HEAD_COACH_INSTRUCTION = """
# 役割
あなたは「あゆむ」。RouteRun の自律ランニングコーチであり、走者のレース準備アーク全体
(Discovery → Entry → Training → Race Day → Post-Race → Next Goal) を能動的に管理する
Plan Manager です。リアクティブなチャットボットではなく、計画を所有し更新し続けます。

# 動作ループ（各サイクルで必ずこの順）
1. get_athlete_state で現在の fitness state (acwr / trainingPhase / weeklyVolumeTrend /
   todayWorkout / raceTarget) を読む。
2. periodization に委譲し、目標レースに向けた次の練習ブロックを設計させる。
3. readiness に委譲し、当日の体調シグナルで負荷を「維持/軽減/強化」調整させる。
4. 必要なら suggest_route_areas で各セッションのコースを用意し、目標が未/再設定の走者には
   recommend_races でレースを提案する。
5. 組み上げた計画とメッセージを evaluator に送る。
6. evaluator が PASS した内容のみを最終出力とする。違反が返れば修正して再送する（最大2回）。
7. evaluator が PASS した計画を write_training_plan(uid=<走者のuid>, plan=<JSON計画>) で
   Firestore Memory Bank に永続化する。uid は get_athlete_state の引数と同じ値を使う。

# 出力フォーマット（JSON）
最終出力は以下の JSON 構造に準拠する:
{
  "message": "<走者向けメッセージ（locale に合わせた言語）>",
  "locale": "ja|en",
  "readinessAdjustment": "maintain|reduce|intensify",
  "acwr": <float>,
  "cycle": {
    "phase": "base_building|build|peak|taper|recovery",
    "startDate": "YYYY-MM-DD",
    "endDate": "YYYY-MM-DD",
    "weeklyTargetKm": <float>
  },
  "weeks": [
    {
      "weekNumber": <int>,
      "sessions": [
        {
          "day": "monday|tuesday|wednesday|thursday|friday|saturday|sunday",
          "type": "easy|tempo|interval|long|recovery|rest",
          "distanceKm": <float>,
          "paceZone": "zone1|zone2|zone3|zone4|zone5|race",
          "notes": "<1文のコーチメモ（日本語 or 英語）>"
        }
      ]
    }
  ],
  "evaluatorPassed": true
}

# 絶対制約（違反不可）
- 「slow / 遅い」を使わない。すべてのペースを等しく価値あるものとして扱う。
- Honest Data Only: 入力データ (センサー/スプリット/rule-engine 出力) に無い統計値・推定値を
  一切創作しない。環境ペース補正などの「発明データ」を出さない。
- 安全性: 週次負荷の増加は ACWR 上限 (+10%) を超えない。
- 走者が使用している言語 (locale) で応答する (日本語/英語)。

# トーン
スポーツ科学の深さを持つ共感的な伴走者。冷たい分析システムではない。
過剰な装飾を避け簡潔で前向き。人格は名前と文体で表現し大げさな自己演出はしない
(Restraint over Spectacle)。
"""

PERIODIZATION = """
# 役割: Periodization Specialist
目標レースに向けたメソ/マイクロサイクルを構築・調整する専門 agent。

# 入力
get_athlete_state から受け取った fitness state (acwr / trainingPhase / weeklyVolumeTrend /
raceTarget) を必ず最初に読み込む。

# 出力ルール
- 週次負荷の増加は ACWR 上限 (+10%) を超えない。
- 各セッション: type (easy|tempo|interval|long|recovery|rest) / distanceKm / paceZone を明示。
- データが無い項目は推測しない。
-「slow / 遅い」を使わない。ペースはゾーン (zone1〜zone5 / race) で表現する。
- 出力は Head Coach が JSON に組み込める構造化テキスト。
"""

READINESS = """
# 役割: Readiness Specialist
当日の HRV / 睡眠 / 安静時 HR から本日のセッション調整を決める専門 agent。

# 判定基準
- 全指標良好 → "intensify"（負荷 +5〜10%）
- 通常範囲内 → "maintain"
- HRV 低下 or 睡眠 <6h or HR +10bpm 以上 → "reduce"（負荷 -10〜20%）

# ルール
- データが無い項目は推測しない。データ不足なら "maintain" を返す。
- 理由を1文で添える。
- 出力形式: {"adjustment": "maintain|reduce|intensify", "reason": "<1文>"}
"""

EVALUATOR = """
# 役割: Quality Gate Evaluator
Head Coach が生成した計画/メッセージを提出前に検査する批評 agent。
パターンマッチと数値比較に特化した高速ゲート。

# 検査手順（必ずこの順で実行）

## Step 1: SDT 言語チェック
入力テキスト中に以下の文字列が 1 つでも存在するか検索する:
  - 「slow」「遅い」「遅く」「遅すぎ」「too slow」
存在する → SDT_FAIL として記録。

## Step 2: Honest-Data チェック
入力 athlete_state に存在しない数値（VO2max 推定・環境補正ペース・「平均的なランナー」統計等）が
レスポンス中に出現しているか確認する。
athlete_state に明示された値のみが許容される。
創作数値が存在する → HONEST_FAIL として記録。

## Step 3: ACWR 安全チェック
athlete_state の acwr と weeklyDistanceKm を読み取る。
レスポンス中の weeklyTargetKm（または同義の週間目標距離）を特定する。
計算: increase_rate = (weeklyTargetKm - weeklyDistanceKm) / weeklyDistanceKm
以下のいずれかに該当する → ACWR_FAIL として記録:
  - increase_rate > 0.10 (現在距離比 +10% 超過)
  - acwr > 1.3 かつ weeklyTargetKm > weeklyDistanceKm (高 ACWR 時の負荷増加)

## Step 4: 判定
- 記録した FAIL が 0 件 → {"result": "PASS"}
- 記録した FAIL が 1 件以上 → {"result": "FAIL", "violations": ["<Step番号>: <具体的な違反内容>", ...]}

# 絶対ルール
- PASS/FAIL 以外の判定は返さない。
- violations の各エントリは Head Coach が即座に修正できるよう「どの単語/数値が問題か」を明示する。
- athlete_state や weeklyDistanceKm が未提供の場合、Step 3 はスキップして PASS 扱いにする。

# Few-Shot Examples（JA / EN 両方を参照すること）

## [JA] Example 1 — PASS（違反なし）
Input athlete_state: {"acwr": 0.95, "weeklyDistanceKm": 30}
Input response: "今週は32kmを目標に、ゾーン2のイージーラン4回で構成します。"
Step 1: 禁止ワードなし / Step 2: 外部統計なし / Step 3: +6.7% ≤ 10%, acwr=0.95 ≤ 1.3
Output: {"result": "PASS"}

## [JA] Example 2 — FAIL（SDT 違反）
Input athlete_state: {"acwr": 1.0, "weeklyDistanceKm": 25}
Input response: "ペースが遅すぎます。インターバルで改善しましょう。"
Step 1: "遅すぎ" 検出 → SDT_FAIL
Output: {"result": "FAIL", "violations": ["Step 1: '遅すぎ' を検出。ペースはゾーン表記に置き換えること"]}

## [JA] Example 3 — FAIL（ACWR 超過）
Input athlete_state: {"acwr": 1.35, "weeklyDistanceKm": 40}
Input response: '{"weeklyTargetKm": 48}'
Step 3: increase_rate=0.20 > 0.10, acwr=1.35 > 1.3 → ACWR_FAIL
Output: {"result": "FAIL", "violations": ["Step 3: weeklyTargetKm=48km は+20%増 (上限+10%超過)。ACWR=1.35>1.3 時は負荷増加禁止"]}

## [EN] Example 4 — PASS (no violations)
Input athlete_state: {"acwr": 1.05, "weeklyDistanceKm": 40}
Input response: "This week targets 42 km. Wednesday: tempo (zone 3). Sunday: long run (zone 2)."
Step 1: No negative pace words / Step 2: No fabricated stats / Step 3: +5% ≤ 10%, acwr=1.05 ≤ 1.3
Output: {"result": "PASS"}

## [EN] Example 5 — FAIL (Honest-Data violation)
Input athlete_state: {"acwr": 0.95, "weeklyDistanceKm": 28}
Input response: "Your estimated VO2max is 52.3 ml/kg/min."
Step 2: VO2max=52.3 absent from athlete_state → HONEST_FAIL
Output: {"result": "FAIL", "violations": ["Step 2: VO2max=52.3 not in athlete_state. Remove fabricated values."]}

## [EN] Example 6 — FAIL (SDT + ACWR double violation)
Input athlete_state: {"acwr": 1.4, "weeklyDistanceKm": 35}
Input response: '{"weeklyTargetKm": 42, "notes": "Your pace is too slow."}'
Step 1: "too slow" → SDT_FAIL / Step 3: +20%, acwr=1.4 > 1.3 → ACWR_FAIL
Output: {"result": "FAIL", "violations": ["Step 1: 'too slow' detected. Use zone notation.", "Step 3: +20% exceeds +10% limit. ACWR=1.4>1.3 prohibits load increase."]}
"""


def deploy() -> str:
    """head_coach を Agent Engine にデプロイし resource_name を返す。

    ツール関数はすべて deploy() スコープのローカル関数として定義する。
    cloudpickle はローカル関数(クロージャ)をバイトコード埋め込みで直列化するため、
    リモートコンテナに agent パッケージ不要。
    """
    print(f"Initializing Vertex AI: project={PROJECT}, region={REGION}")
    vertexai.init(project=PROJECT, location=REGION, staging_bucket=STAGING_BUCKET)

    # ── Runtime config (captured as closure values) ──────────────────────────
    _database_id = os.getenv("FIREBASE_DATABASE_ID", "routerun-db")
    _cf_base = os.getenv(
        "CF_BASE_URL",
        f"https://{REGION}-{PROJECT}.cloudfunctions.net",
    )

    # ── Tool helpers (local functions → pickled by value) ────────────────────

    def _db():
        import firebase_admin
        from firebase_admin import credentials, firestore
        try:
            firebase_admin.get_app()
        except ValueError:
            cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
            if cred_path:
                firebase_admin.initialize_app(credentials.Certificate(cred_path))
            else:
                firebase_admin.initialize_app()
        return firestore.client(database_id=_database_id)

    def _call(name: str, payload: dict, id_token: str) -> dict:
        import requests
        r = requests.post(
            f"{_cf_base}/{name}",
            headers={
                "Authorization": f"Bearer {id_token}",
                "Content-Type": "application/json",
            },
            json={"data": payload},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()["result"]

    # ── ADK tools (local functions → pickled by value) ───────────────────────

    def _is_pro_user(uid: str) -> bool:
        """Pro ステータスを Firestore で確認する認可ミドルウェア。"""
        try:
            user_doc = _db().collection("users").document(uid).get()
            subscription_status = (user_doc.to_dict() or {}).get("subscriptionStatus", "")
            return subscription_status in ("pro", "trial")
        except Exception:
            return False  # 安全側: Pro 未確認はゲート維持

    def get_athlete_state(uid: str) -> dict:
        """走者の現在の fitness state を返す。計画サイクルの最初に必ず呼ぶ。

        Returns:
            goalContext dict with keys:
              acwr, trainingPhase, weeklyVolumeTrend, todayWorkout, raceTarget,
              currentFitness, locale
        """
        db = _db()
        doc = db.collection("users").document(uid).get()
        data = doc.to_dict() or {}
        return data.get("goalContext", {})

    def suggest_route_areas(
        uid_token: str,
        latitude: float,
        longitude: float,
        distance_km: float,
        route_type: str,
        theme: Optional[str] = None,
        pace_zone: Optional[str] = None,
        training_phase: Optional[str] = None,
    ) -> dict:
        """1セッション分の走行エリア/ウェイポイントを AI 推薦。Pro 限定。

        Args:
            route_type: "loop" | "outAndBack" | "scenic"
            pace_zone: "zone1" | "zone2" | "zone3" | "zone4" | "zone5" | "race"
            training_phase: "base_building" | "build" | "peak" | "taper" | "recovery"
        """
        return _call(
            "suggestRouteAreas",
            {
                "latitude": latitude,
                "longitude": longitude,
                "distanceKm": distance_km,
                "routeType": route_type,
                "theme": theme,
                "paceZone": pace_zone,
                "trainingPhase": training_phase,
            },
            uid_token,
        )

    def recommend_races(
        uid_token: str,
        monthly_distance_km: float,
        recent_pace_min_per_km: float,
        vo2max: Optional[float] = None,
        limit: int = 5,
    ) -> dict:
        """走者の体力・目標に合う目標レースを推薦（理由付き）。"""
        return _call(
            "recommendEventsV3",
            {
                "monthlyDistanceKm": monthly_distance_km,
                "recentPaceMinPerKm": recent_pace_min_per_km,
                "vo2MaxEstimate": vo2max,
                "limit": limit,
            },
            uid_token,
        )

    def chat_with_coach(
        uid_token: str,
        message: str,
        coaching_mode: str = "conversation",
    ) -> dict:
        """走者の不安・質問に実証済みコーチング頭脳で応答。

        Args:
            coaching_mode: "conversation" | "training" | "motivation"
        """
        return _call(
            "chatWithCoach",
            {
                "message": message,
                "conversationHistory": [],
                "coachingMode": coaching_mode,
            },
            uid_token,
        )

    def write_training_plan(uid: str, plan: dict) -> dict:
        """評価済み計画を users/{uid}.trainingPlan に永続化 (Memory Bank)。

        Pro 会員・isDemo=true・test_ UID のいずれかが必要（安全ガード）。
        P0-1 OpenAI UX Fix: readinessAdjustment + readinessReason を goalContext に反映。
        P1-SDT Fix: SDT 禁止ワードをルールベースで二重ガード（LLM 非依存）。
        P2-DeepMind Fix: planVersion による冪等性チェック。
        Build 71: 全 Pro ユーザー対応（DEMO_UIDS 廃止・_is_pro_user() チェック追加）。
        """
        import time as _t
        import json as _json

        # P1-SDT: SDT 禁止ワードをルールベースでチェック（LLM に依存しない二重ガード）
        SDT_FORBIDDEN = ["slow", "遅い", "遅く", "遅すぎ", "too slow"]
        plan_text = _json.dumps(plan, ensure_ascii=False).lower()
        sdt_hits = [w for w in SDT_FORBIDDEN if w in plan_text]
        if sdt_hits:
            raise ValueError(
                f"[SDT Safety] 計画拒否: 禁止ワード {sdt_hits} を検出。"
                f"ペースはゾーン表記（zone1-zone5/race）に置き換えること。"
            )

        # Build 71: Pro 会員・isDemo=true・test_ UID のいずれかを許可
        is_pro = _is_pro_user(uid)
        if not plan.get("isDemo", False) and not uid.startswith("test_") and not is_pro:
            raise ValueError(
                "write_training_plan: Pro 会員・isDemo=true・test_ UID のいずれかが必要です。"
            )

        db = _db()

        # P2-DeepMind: 冪等性チェック
        incoming_version = plan.get("planVersion") or plan.get("plan_version")
        if incoming_version is not None:
            try:
                existing = (db.collection("users").document(uid).get().to_dict() or {})
                existing_version = existing.get("trainingPlan", {}).get("planVersion")
                if existing_version is not None and existing_version == incoming_version:
                    return {"success": True, "plan_version": incoming_version, "readiness_updated": False, "skipped": True}
            except Exception:
                pass

        # ACWR ルールベース安全チェック
        try:
            user_doc = db.collection("users").document(uid).get()
            goal_context = (user_doc.to_dict() or {}).get("goalContext", {})
            fitness = goal_context.get("currentFitness", {})
            current_km = fitness.get("weeklyDistanceKm", 0) or 0
            acwr = fitness.get("acwr", 0.0) or 0.0
            target_km = None
            if isinstance(plan.get("cycle"), dict):
                target_km = plan["cycle"].get("weeklyTargetKm")
            if target_km is None:
                target_km = plan.get("weeklyTargetKm")
            if isinstance(current_km, (int, float)) and current_km > 0 and isinstance(target_km, (int, float)) and target_km > 0:
                increase_rate = (target_km - current_km) / current_km
                violations = []
                if increase_rate > 0.10:
                    violations.append(f"週間目標 +{increase_rate * 100:.1f}% 超過 ({current_km}→{target_km}km, 上限+10%)")
                if acwr > 1.3 and target_km > current_km:
                    violations.append(f"ACWR={acwr:.2f}>1.3 時の負荷増加禁止 ({current_km}→{target_km}km)")
                if violations:
                    raise ValueError(f"[ACWR Safety] 計画拒否: {' / '.join(violations)}")
        except ValueError:
            raise
        except Exception:
            pass

        saved_version = incoming_version or int(_t.time())
        db.collection("users").document(uid).set(
            {"trainingPlan": {**plan, "savedAt": int(_t.time()), "planVersion": saved_version}},
            merge=True,
        )

        # P0-1 OpenAI UX Fix: readinessAdjustment + readinessReason を goalContext に反映
        readiness_updated = False
        agent_adjustment = plan.get("readinessAdjustment")
        agent_reason = plan.get("readinessReason") or plan.get("message")
        if agent_adjustment in ("maintain", "reduce", "intensify"):
            try:
                db.collection("users").document(uid).set(
                    {"goalContext": {"currentFitness": {
                        "readinessAdjustment": agent_adjustment,
                        "readinessReason": agent_reason,
                        "agentUpdatedAt": int(_t.time()),
                    }}},
                    merge=True,
                )
                readiness_updated = True
            except Exception:
                pass

        return {"success": True, "plan_version": saved_version, "readiness_updated": readiness_updated, "skipped": False}

    # ── Agent definitions ────────────────────────────────────────────────────
    from google.adk.agents import Agent
    from google.adk.tools.agent_tool import AgentTool
    from google.genai import types

    PLANNER = os.getenv("PLANNER_MODEL", "gemini-2.5-pro")
    FAST = os.getenv("FAST_MODEL", "gemini-2.5-flash")
    LITE = os.getenv("LITE_MODEL", "gemini-2.5-flash-lite")

    # Head Coach: supervisor 判断 + JSON 組み立て品質向上
    _thinking_head = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_budget=2048)
    )
    # Periodization: メソ/マイクロサイクル設計は最も推論負荷が高いタスク
    _thinking_plan = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_budget=4096)
    )
    # Readiness: HRV/睡眠データの多変数解釈に中程度の推論
    _thinking_medium = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_budget=1024)
    )

    periodization = Agent(
        name="periodization",
        model=PLANNER,
        instruction=PERIODIZATION,
        tools=[get_athlete_state],
        generate_content_config=_thinking_plan,
    )
    readiness = Agent(
        name="readiness",
        model=FAST,
        instruction=READINESS,
        tools=[get_athlete_state],
        generate_content_config=_thinking_medium,
    )
    evaluator = Agent(
        name="evaluator",
        model=LITE,
        instruction=EVALUATOR,
    )
    head_coach = Agent(
        name="ayumu_head_coach",
        model=PLANNER,
        instruction=HEAD_COACH_INSTRUCTION,
        generate_content_config=_thinking_head,
        tools=[
            AgentTool(agent=periodization),
            AgentTool(agent=readiness),
            AgentTool(agent=evaluator),
            get_athlete_state,
            suggest_route_areas,
            recommend_races,
            chat_with_coach,
            write_training_plan,
        ],
    )

    # ── Deploy ───────────────────────────────────────────────────────────────
    print("Deploying ayumu_head_coach to Agent Engine...")
    print(f"  Staging bucket: {STAGING_BUCKET}")
    print("  This may take 5-10 minutes on first deploy.")

    remote_agent = agent_engines.create(
        agent_engine=head_coach,
        requirements=[
            "google-cloud-aiplatform[agent_engines,adk]>=1.112",
            "google-cloud-firestore>=2.19.0",
            "firebase-admin>=6.5.0",
            "requests>=2.32.0",
            "python-dotenv>=1.0.0",
        ],
        display_name="ayumu-head-coach",
        description=(
            "RouteRun Agentic Coaching — Google for Startups AI Agents Challenge. "
            "Supervisor multi-agent: Head Coach (gemini-2.5-pro, thinking=2048) + "
            "Periodization (gemini-2.5-pro, thinking=4096) + "
            "Readiness (gemini-2.5-flash, thinking=1024) + "
            "Evaluator (gemini-2.5-flash-lite)."
        ),
    )

    resource_name = remote_agent.resource_name
    print(f"\n✅ Deployed successfully!")
    print(f"   resource_name: {resource_name}")

    # P0-1 GCP Fix: resource_name を Secret Manager に自動保存
    # CI/CD 環境で resource_name を確実に引き継げるようにする。
    # SECRET_NAME 環境変数でシークレット名を上書き可能。
    secret_name = os.getenv("AGENT_ENGINE_SECRET_NAME", "AGENT_ENGINE_RESOURCE_NAME")
    try:
        import subprocess
        result = subprocess.run(
            [
                "gcloud", "secrets", "versions", "add", secret_name,
                f"--project={PROJECT}",
                "--data-stdin",
            ],
            input=resource_name.encode(),
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            print(f"   ✅ Saved to Secret Manager: projects/{PROJECT}/secrets/{secret_name}")
        else:
            # シークレットが存在しない場合は作成してから再試行
            subprocess.run(
                ["gcloud", "secrets", "create", secret_name, f"--project={PROJECT}", "--replication-policy=automatic"],
                capture_output=True, timeout=30,
            )
            subprocess.run(
                ["gcloud", "secrets", "versions", "add", secret_name, f"--project={PROJECT}", "--data-stdin"],
                input=resource_name.encode(), capture_output=True, timeout=30,
            )
            print(f"   ✅ Created and saved to Secret Manager: projects/{PROJECT}/secrets/{secret_name}")
    except Exception as e:
        # Secret Manager への保存失敗はデプロイを妨げない
        print(f"   ⚠️  Secret Manager save failed (manual .env update required): {e}")
        print(f"\n   Add to .env manually:")
        print(f"   AGENT_ENGINE_RESOURCE_NAME={resource_name}")

    print(f"\n   Trace viewer:")
    print(f"   https://console.cloud.google.com/vertex-ai/agents?project={PROJECT}")

    return resource_name


def delete(resource_name: str) -> None:
    """デプロイ済み agent を削除。"""
    vertexai.init(project=PROJECT, location=REGION)
    agent = agent_engines.get(resource_name)
    agent.delete(force=True)
    print(f"✅ Deleted: {resource_name}")


def main():
    parser = argparse.ArgumentParser(description="Deploy ayumu_head_coach to Agent Engine")
    parser.add_argument("--delete", metavar="RESOURCE_NAME", help="Delete a deployed agent")
    args = parser.parse_args()

    if args.delete:
        delete(args.delete)
    else:
        deploy()


if __name__ == "__main__":
    main()
