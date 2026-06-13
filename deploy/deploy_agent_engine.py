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

# ── System instructions (strings pickle by value — module-level OK) ────────
# Single source of truth: import from agent.instructions to eliminate drift.
from agent.instructions import (  # noqa: E402
    HEAD_COACH_INSTRUCTION,
    PERIODIZATION,
    READINESS,
    EVALUATOR,
    RACE_STRATEGY,
    ROUTE_INTELLIGENCE,
)

PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "runroute-476505")
REGION = os.getenv("GOOGLE_CLOUD_REGION", "asia-northeast1")
STAGING_BUCKET = os.getenv("STAGING_BUCKET", f"gs://{PROJECT}-agent-staging")


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
              currentFitness, locale,
              completedSessions (過去14日の完了済みセッション),
              activePlanCycle (現在の計画サイクル),
              activePlanWeeks (現在の計画の全週)
        """
        import datetime as _dt
        db = _db()
        doc = db.collection("users").document(uid).get()
        data = doc.to_dict() or {}
        goal_context = data.get("goalContext", {})

        # INCREMENTAL PLANNING: 完了済みセッションと現在の計画を含める
        training_plan = data.get("trainingPlan", {})
        completed_map = training_plan.get("completedSessionMap", {})
        cutoff_date = (_dt.date.today() - _dt.timedelta(days=14)).isoformat()
        recent_completed = [
            {"runId": run_id, **session}
            for run_id, session in completed_map.items()
            if isinstance(session, dict) and session.get("date", "") >= cutoff_date
        ]

        return {
            **goal_context,
            "completedSessions": recent_completed,
            "activePlanCycle": training_plan.get("cycle"),
            "activePlanWeeks": training_plan.get("weeks", []),
        }

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

    def get_weather_context(latitude: float, longitude: float) -> dict:
        """現在地の天気コンテキスト（気温・湿度・降水確率・風速・AQI）を取得する。

        Cloud Function getWeatherContext を呼び出す（6時間 Firestore キャッシュ付き）。
        ネットワーク障害時は caution フォールバックを返す（raise しない）。

        Args:
            latitude: 走者の緯度
            longitude: 走者の経度

        Returns:
            temperature_c, humidity_pct, precipitation_prob, wind_speed_ms,
            aqi, condition, advice ("go"|"caution"|"avoid")
        """
        import requests as _requests
        try:
            r = _requests.get(
                f"{_cf_base}/getWeatherContext",
                params={"lat": latitude, "lng": longitude},
                timeout=5,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            return {
                "temperature_c": None,
                "humidity_pct": None,
                "precipitation_prob": None,
                "wind_speed_ms": None,
                "aqi": None,
                "condition": "unknown",
                "advice": "caution",
                "error": str(e),
            }

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

        # SINGLE READ: idempotency check + ACWR safety + completedSessionMap carry-forward.
        # Raising on Firestore read failure prevents overwriting completed session history with {}.
        incoming_version = plan.get("planVersion") or plan.get("plan_version")
        existing_completed_map: dict = {}
        try:
            existing_doc_data = (db.collection("users").document(uid).get().to_dict() or {})
            existing_plan = existing_doc_data.get("trainingPlan", {})
            existing_completed_map = existing_plan.get("completedSessionMap", {})
            # P2-DeepMind: 冪等性チェック — 同一 planVersion は重複書き込みしない
            if incoming_version is not None:
                existing_version = existing_plan.get("planVersion") or existing_plan.get("plan_version")
                if existing_version is not None and existing_version == incoming_version:
                    return {"success": True, "plan_version": incoming_version, "readiness_updated": False, "skipped": True}
            # ACWR ルールベース安全チェック
            goal_context = existing_doc_data.get("goalContext", {})
            fitness = goal_context.get("currentFitness", {})
            current_km = fitness.get("weeklyDistanceKm", 0) or 0
            acwr_val = fitness.get("acwr", 0.0) or 0.0
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
                if acwr_val > 1.3 and target_km > current_km:
                    violations.append(f"ACWR={acwr_val:.2f}>1.3 時の負荷増加禁止 ({current_km}→{target_km}km)")
                if violations:
                    raise ValueError(f"[ACWR Safety] 計画拒否: {' / '.join(violations)}")
        except ValueError:
            raise
        except Exception as read_err:
            raise ValueError(
                f"[write_training_plan] Firestore 読み取り失敗: {read_err}. "
                "completedSessionMap が読み取れないため書き込みを中断します。"
                "走者の完了済みセッション記録を保護するため、一時的エラーの場合は再試行してください。"
            ) from read_err

        saved_version = incoming_version or int(_t.time())
        db.collection("users").document(uid).set(
            {"trainingPlan": {**plan, "savedAt": int(_t.time()), "planVersion": saved_version, "completedSessionMap": existing_completed_map}},
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

    def write_race_strategy(uid: str, strategy: dict) -> dict:
        """レース前日のペーシング戦略を users/{uid}.raceStrategy に永続化。

        Race Strategy Specialist agent が評価済み戦略を呼び出す。

        Args:
            uid: Firebase user ID
            strategy: RACE_STRATEGY 形式の JSON
                      必須: evaluatorPassed=True, racePacing.segments (list)
        """
        import json as _json

        if not strategy.get("evaluatorPassed", False):
            raise ValueError(
                "[Evaluator Gate] 戦略拒否: evaluatorPassed=True が必須です。"
                "evaluator に戦略を送り PASS を受け取ってから write_race_strategy を呼んでください。"
            )

        SDT_FORBIDDEN = ["slow", "遅い", "遅く", "遅すぎ", "too slow"]
        strategy_text = _json.dumps(strategy, ensure_ascii=False).lower()
        sdt_hits = [w for w in SDT_FORBIDDEN if w in strategy_text]
        if sdt_hits:
            raise ValueError(
                f"[SDT Safety] 戦略拒否: 禁止ワード {sdt_hits} を検出。"
                "ペースはゾーン表記（zone1-zone5/race）に置き換えること。"
            )

        pacing = strategy.get("racePacing", {})
        if not isinstance(pacing.get("segments"), list) or len(pacing["segments"]) == 0:
            raise ValueError("[Schema] racePacing.segments はリスト (配列) であり最低1要素が必要です。")

        if not strategy.get("isDemo", False) and not uid.startswith("test_") and not _is_pro_user(uid):
            raise ValueError("write_race_strategy: Pro 会員が必要です。")

        import time as _t
        db = _db()
        now_ts = int(_t.time())
        db.collection("users").document(uid).set(
            {"raceStrategy": {**strategy, "savedAt": now_ts}},
            merge=True,
        )
        return {"success": True, "saved_at": now_ts}

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
    race_strategy = Agent(
        name="race_strategy",
        model=FAST,
        instruction=RACE_STRATEGY,
        tools=[get_athlete_state, AgentTool(agent=evaluator), write_race_strategy],
        generate_content_config=_thinking_medium,
    )
    route_intelligence = Agent(
        name="route_intelligence",
        model=FAST,
        instruction=ROUTE_INTELLIGENCE,
        tools=[get_athlete_state, get_weather_context],
        generate_content_config=_thinking_medium,
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
            AgentTool(agent=race_strategy),
            AgentTool(agent=route_intelligence),
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
            "Race Strategy (gemini-2.5-flash, thinking=1024) + "
            "Route Intelligence (gemini-2.5-flash, thinking=1024) + "
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
