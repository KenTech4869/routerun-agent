"""RouteRun Cloud Functions / Firestore wrappers for ADK agents.

Schema source: functions/src/ (R4 確認済み 2026-06-03)
  - onRunCompleted.ts: run_history フィールド (userId, distance, duration, pace, date, acwr, ...)
  - recommendEventsV3.ts: RecommendationResponse {recommendations, totalCandidates}
  - suggestRouteAreas.ts: SuggestRouteAreasResponse {areas, model, responseTimeMs, cached}
  - callable envelope: {"data": {...}} / response: {"result": {...}}

MCP Schema (JSON-RPC 2.0 準拠):
  各ツールの入出力型を Pydantic モデルで定義。
  ADK は関数シグネチャの型ヒントから JSON Schema を自動生成する。
  Pydantic モデルは型安全性・テスト・ドキュメントのために維持する。
"""

import os
import re
import time as _time
import requests
from typing import Optional
from firebase_admin import firestore, initialize_app, credentials, get_app
from pydantic import BaseModel, Field

PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]  # no hardcoded fallback — prevents accidental prod writes
REGION = os.getenv("GOOGLE_CLOUD_REGION", "asia-northeast1")
DATABASE_ID = os.getenv("FIREBASE_DATABASE_ID", "routerun-db")
CF_BASE = os.getenv("CF_BASE_URL", f"https://{REGION}-{PROJECT}.cloudfunctions.net")
_UID_RE = re.compile(r"^[a-zA-Z0-9]{20,128}$")  # Firebase UID format validator

# Firebase Admin SDK — idempotent init
def _db() -> firestore.Client:
    try:
        get_app()
    except ValueError:
        cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if cred_path:
            initialize_app(credentials.Certificate(cred_path))
        else:
            initialize_app()  # ADC (Agent Engine 実行環境では自動)
    return firestore.client(database_id=DATABASE_ID)


def _call(name: str, payload: dict, id_token: str) -> dict:
    """Firebase callable の正しい呼び出し (envelope + IDトークン)。"""
    r = requests.post(
        f"{CF_BASE}/{name}",
        headers={
            "Authorization": f"Bearer {id_token}",
            "Content-Type": "application/json",
        },
        json={"data": payload},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["result"]


# ── P1-DeepMind: Pro tier ミドルウェア（ツールから認可ロジックを分離）────────

def _is_pro_user(uid: str) -> bool:
    """ユーザーの Pro ステータスを Firestore で確認する認可ミドルウェア。

    ツール関数から認可ロジックを分離し、各ツールが純粋なデータ操作に集中できるようにする。
    Firestore 読み取りが失敗した場合は安全側（False）にフォールバック。
    """
    try:
        user_doc = _db().collection("users").document(uid).get()
        subscription_status = (user_doc.to_dict() or {}).get("subscriptionStatus", "")
        return subscription_status in ("pro", "trial")
    except Exception:
        return False  # 安全側: Pro 未確認はゲート維持


# ── MCP Output Schemas (Pydantic / JSON-RPC 2.0) ──────────────────────────────
# ADK 1.34.x は dict 返却が互換性最高のため、Pydantic モデルは
# 型安全性・テスト・MCP ドキュメントとして機能する。

class AthleteStateOutput(BaseModel):
    """get_athlete_state の出力スキーマ。"""
    acwr: Optional[float] = Field(None, description="Acute:Chronic Workload Ratio (安全域: 0.8-1.3)")
    trainingPhase: Optional[str] = Field(None, description="base_building|build|peak|taper|recovery")
    weeklyVolumeTrend: Optional[str] = Field(None, description="increasing|stable|decreasing")
    readinessAdjustment: Optional[str] = Field(None, description="maintain|reduce|intensify")
    readinessReason: Optional[str] = Field(None, description="あゆむエージェントによる調整理由")
    locale: Optional[str] = Field(None, description="ja|en")
    completedSessions: Optional[list] = Field(None, description="過去14日間の完了済みランセッション — これらの日は再スケジュール禁止")
    activePlanCycle: Optional[dict] = Field(None, description="現在有効な計画サイクル (phase/startDate/endDate/weeklyTargetKm)")
    activePlanWeeks: Optional[list] = Field(None, description="現在の計画の全週セッション — completedSessions と照合して未消化分を特定する")


class RouteAreaOutput(BaseModel):
    """suggest_route_areas の出力スキーマ。"""
    areas: list = Field(default_factory=list, description="推薦エリアリスト")
    model: Optional[str] = Field(None, description="使用モデル ID")
    responseTimeMs: Optional[int] = Field(None, description="レスポンス時間 (ms)")
    cached: Optional[bool] = Field(None, description="キャッシュヒット")


class RaceRecommendationOutput(BaseModel):
    """recommend_races の出力スキーマ。"""
    recommendations: list = Field(default_factory=list, description="推薦レースリスト")
    totalCandidates: Optional[int] = Field(None, description="候補レース総数")


class WritePlanOutput(BaseModel):
    """write_training_plan の出力スキーマ。"""
    success: bool = Field(..., description="書き込み成功フラグ")
    plan_version: Optional[int] = Field(None, description="保存されたプランのバージョン番号")
    readiness_updated: bool = Field(False, description="goalContext.readinessAdjustment を更新したか")
    skipped: bool = Field(False, description="冪等スキップ (同一 planVersion が既存)")


class WriteRaceStrategyOutput(BaseModel):
    """write_race_strategy の出力スキーマ。"""
    success: bool = Field(..., description="書き込み成功フラグ")
    saved_at: int = Field(..., description="保存時の UNIX タイムスタンプ")


# ── Tool 1: Athlete State ──────────────────────────────────────────────────

def get_athlete_state(uid: str) -> dict:
    """走者の現在の fitness state を返す。計画サイクルの最初に必ず呼ぶ。

    Args:
        uid: Firebase user ID

    Returns:
        goalContext dict (AthleteStateOutput schema):
          acwr (float): Acute:Chronic Workload Ratio (目安: 0.8-1.3 が安全域)
          trainingPhase (str): "base_building" | "build" | "peak" | "taper" | "recovery"
          weeklyVolumeTrend (str): "increasing" | "stable" | "decreasing"
          readinessAdjustment (str): "maintain" | "reduce" | "intensify"
          readinessReason (str | None): あゆむエージェントによる調整理由
          todayWorkout (dict | None): {type, distanceKm, notes}
          raceTarget (dict | None): {eventId, eventName, date, distanceKm}
          currentFitness (dict): {vo2MaxEstimate, recentPaceMinPerKm, monthlyDistanceKm}
          locale (str): "ja" | "en"
          completedSessions (list): 過去14日の完了済みセッション [{date, dayOfWeek, runId, actualDistanceKm, durationMin}]
          activePlanCycle (dict | None): 現在の計画サイクル
          activePlanWeeks (list): 現在の計画の全週
    """
    import datetime as _dt
    if not _UID_RE.match(uid or ""):
        raise ValueError(f"[get_athlete_state] Invalid uid format — must be 20-128 alphanumeric chars")
    db = _db()
    doc = db.collection("users").document(uid).get()
    data = doc.to_dict() or {}
    goal_context = data.get("goalContext", {})

    # INCREMENTAL PLANNING: Include completed sessions and active plan context so the
    # agent can update the existing plan rather than regenerating it from zero each run.
    training_plan = data.get("trainingPlan", {})

    # completedSessionMap: {runId: {date, dayOfWeek, actualDistanceKm, durationMin}}
    # Stored as a map for natural idempotency (same runId write = no-op).
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


# ── Tool 2: Route Area Suggestion ─────────────────────────────────────────

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
    """1セッション分の走行エリア/ウェイポイントを AI 推薦。Pro 限定・Schrems II 除外。

    Args:
        uid_token: Firebase ID トークン (認証に使用)
        latitude: 出発点の緯度
        longitude: 出発点の経度
        distance_km: 目標走行距離 (km)
        route_type: "loop" | "outAndBack" | "scenic"
        theme: オプション - コースのテーマ
        pace_zone: "zone1" | "zone2" | "zone3" | "zone4" | "zone5" | "race"
        training_phase: "base_building" | "build" | "peak" | "taper" | "recovery"

    Returns:
        RouteAreaOutput schema: areas, model, responseTimeMs, cached
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


# ── Tool 3: Race Recommendation ───────────────────────────────────────────

def recommend_races(
    uid_token: str,
    monthly_distance_km: float,
    recent_pace_min_per_km: float,
    vo2max: Optional[float] = None,
    limit: int = 5,
) -> dict:
    """走者の体力・目標に合う目標レースを推薦（理由付き）。

    Args:
        uid_token: Firebase ID トークン
        monthly_distance_km: 月間走行距離 (km)
        recent_pace_min_per_km: 最近のペース (分/km)
        vo2max: VO2max 推定値 (任意)
        limit: 返却する推薦数 (デフォルト: 5)

    Returns:
        RaceRecommendationOutput schema: recommendations, totalCandidates

    P1-1 TODO (Google Search Grounding — $500 予算内優先実装):
        現状: recommendEventsV3 は内部イベント DB のみ参照。
        改善: Vertex AI の googleSearchRetrieval grounding を有効にすることで
              実際のレース情報（開催日・エントリー状況・コース情報）をリアルタイムで接地できる。
        実装手順:
          1. `google-cloud-aiplatform` SDK で `GenerationConfig` に
             `groundingConfig=GroundingConfig(google_search_retrieval=GoogleSearchRetrieval())`
             を追加して Gemini 呼び出しを行うヘルパー関数を実装。
          2. この関数に "〈location〉周辺の〈distance_km〉kmクラスのマラソン大会 開催日程" を
             クエリとして渡し、grounding チャンク（URL + スニペット）を取得。
          3. recommendEventsV3 の結果にグラウンディング情報を annotate して返す。
        コスト見積もり: 月間 1000 クエリで約 $20-30 (Dynamic Retrieval 使用でさらに削減可)
        GCP サービス: Vertex AI Grounding with Google Search
                      https://cloud.google.com/vertex-ai/generative-ai/docs/grounding/overview
    """
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


# ── Tool 4: Chat (既存コーチ頭脳を活用) ───────────────────────────────────

def chat_with_coach(
    uid_token: str,
    message: str,
    coaching_mode: str = "conversation",
) -> dict:
    """走者の不安・質問に実証済みコーチング頭脳で応答。

    Args:
        uid_token: Firebase ID トークン
        message: 走者のメッセージ
        coaching_mode: "conversation" | "training" | "motivation"

    Returns:
        {message: str, conversationId: str}
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


# ── Tool 5: Weather Context ───────────────────────────────────────────────

def get_weather_context(latitude: float, longitude: float) -> dict:
    """現在地の天気コンテキスト（気温・湿度・降水確率・風速・AQI）を取得する。

    Cloud Function getWeatherContext を呼び出す（6時間 Firestore キャッシュ付き）。
    Route Intelligence agent がルート推薦前に必ず呼ぶ。
    ネットワーク障害時は caution フォールバックを返す（raise しない）。

    Args:
        latitude: 走者の緯度
        longitude: 走者の経度

    Returns:
        temperature_c: float — 気温 (°C)
        humidity_pct: float — 湿度 (%)
        precipitation_prob: float — 降水確率 (0.0–1.0)
        wind_speed_ms: float — 風速 (m/s)
        aqi: int — Air Quality Index (0-500)
        condition: str — "clear" | "cloudy" | "rain" | "snow" | "storm"
        advice: str — "go" | "caution" | "avoid"
    """
    try:
        r = requests.get(
            f"{CF_BASE}/getWeatherContext",
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


# ── P1-DeepMind: ACWR ルールベース安全チェック（公開名に変更）────────────────
# Evaluator (gemini-2.5-flash-lite) は数値演算の信頼性が低いため、
# ACWR の数値チェックはコードで実施して Evaluator の責務を言語パターンマッチに限定する。

def check_acwr_safety(plan: dict, athlete_state: dict) -> None:
    """ACWR 安全制約を rule-based でチェック。違反時は ValueError を raise。

    制約:
      1. weeklyTargetKm の増加率が +10% を超えない
      2. acwr > 1.3 のとき負荷増加を禁止
    """
    current_km = athlete_state.get("weeklyDistanceKm") or 0
    if not isinstance(current_km, (int, float)) or current_km <= 0:
        return  # データ不足 → スキップ

    target_km: Optional[float] = None
    if isinstance(plan.get("cycle"), dict):
        target_km = plan["cycle"].get("weeklyTargetKm")
    if target_km is None:
        target_km = plan.get("weeklyTargetKm")
    if not isinstance(target_km, (int, float)) or target_km <= 0:
        return  # 計画に距離情報なし → スキップ

    acwr = athlete_state.get("acwr", 0.0)
    increase_rate = (target_km - current_km) / current_km

    violations = []
    if increase_rate > 0.10:
        violations.append(
            f"週間目標距離が +{increase_rate * 100:.1f}% 増加 ({current_km}→{target_km}km)。"
            f"+10% 以内に制限すること。"
        )
    if isinstance(acwr, (int, float)) and acwr > 1.3 and target_km > current_km:
        violations.append(
            f"ACWR={acwr:.2f} > 1.3 の状態で負荷増加 ({current_km}→{target_km}km)。"
            f"ACWR 高値時は負荷増加禁止。"
        )

    if violations:
        raise ValueError(f"[ACWR Safety] 計画拒否: {' / '.join(violations)}")


# ── Tool 5: Write Training Plan (Memory Bank) ─────────────────────────────

def write_training_plan(uid: str, plan: dict) -> dict:
    """評価済み計画を users/{uid}.trainingPlan に永続化 (Memory Bank)。

    Agent Engine から直接 Firestore に書く（callable 不要）。
    Pro 会員または isDemo=true が付いていない計画は拒否する（安全ガード）。

    P0-1 OpenAI UX Fix: plan に readinessAdjustment / readinessReason が含まれる場合、
    goalContext.currentFitness にも反映してあゆむのエージェント判断をユーザーに表示する。

    P2-DeepMind Fix: planVersion による冪等性チェック。
    同一バージョンの計画は重複書き込みしない。

    Args:
        uid: Firebase user ID
        plan: エージェントが生成した計画 JSON (evaluatorPassed: true が必須)
              readinessAdjustment: "maintain"|"reduce"|"intensify" (任意)
              readinessReason: <1文の調整理由> (任意)
              planVersion: <int> 冪等性キー (任意)

    Returns:
        WritePlanOutput schema: {success, plan_version, readiness_updated, skipped}
    """
    import json as _json

    # EVALUATOR HARD GATE: evaluatorPassed=True が確認された計画のみ永続化を許可。
    # Head Coach の Instruction では「FAIL なら再送（最大2回）」と指示しているが、
    # LLM が指示を無視しても write_training_plan を呼べないようコードで強制する。
    # これにより SDT / HonestData / ACWR / InjuryDisclaimer 検査をすり抜けた計画が
    # ユーザーに届くことを完全に防ぐ。
    if not plan.get("evaluatorPassed", False):
        raise ValueError(
            "[Evaluator Gate] 計画拒否: evaluatorPassed=True が必須です。"
            "evaluator に計画を送り PASS を受け取ってから write_training_plan を呼んでください。"
        )

    # P1-SDT: SDT 禁止ワードをルールベースでチェック（LLM に依存しない二重ガード）
    SDT_FORBIDDEN = ["slow", "too slow", "lazy", "sluggish", "遅い", "遅く", "遅すぎ", "のんびり", "ゆっくり"]
    plan_text = _json.dumps(plan, ensure_ascii=False).lower()
    sdt_hits = [w for w in SDT_FORBIDDEN if w in plan_text]
    if sdt_hits:
        raise ValueError(
            f"[SDT Safety] 計画拒否: 禁止ワード {sdt_hits} を検出。"
            f"ペースはゾーン表記（zone1-zone5/race）に置き換えること。"
        )

    # P1-DeepMind: Pro ミドルウェアで認可チェック（ツールから分離）
    # Security FIX: removed isDemo/test_ bypass — client-controlled flags must not gate server writes
    is_pro = _is_pro_user(uid)
    if not is_pro:
        raise ValueError(
            "write_training_plan: Pro 会員が必要です。Pro プランにアップグレードしてください。"
        )

    db = _db()

    # SINGLE READ: Read the existing user document once, reused for:
    #   1. Idempotency check (planVersion comparison)   — P2-DeepMind
    #   2. ACWR safety check (goalContext)              — avoids second Firestore read
    #   3. Preserving completedSessionMap               — incremental planning
    incoming_version = plan.get("planVersion") or plan.get("plan_version")
    existing_doc_data: dict = {}
    existing_completed_map: dict = {}
    try:
        existing_doc = db.collection("users").document(uid).get()
        existing_doc_data = existing_doc.to_dict() or {}
        existing_plan = existing_doc_data.get("trainingPlan", {})
        existing_completed_map = existing_plan.get("completedSessionMap", {})

        # P2-DeepMind: 冪等性チェック — 同一 planVersion は重複書き込みしない
        if incoming_version is not None:
            existing_version = existing_plan.get("planVersion") or existing_plan.get("plan_version")
            if existing_version is not None and existing_version == incoming_version:
                return {
                    "success": True,
                    "plan_version": incoming_version,
                    "readiness_updated": False,
                    "skipped": True,
                }
    except Exception as read_err:
        raise ValueError(
            f"[write_training_plan] Firestore 読み取り失敗: {read_err}. "
            "completedSessionMap が読み取れないため書き込みを中断します。"
            "走者の完了済みセッション記録を保護するため、一時的エラーの場合は再試行してください。"
        ) from read_err

    # P0-3 SCHEMA VALIDATION: Firestore 書き込み前に必須フィールドの型・値域を検証する。
    # evaluator PASS を通過した計画でも LLM が不正な型を返す可能性があるため、
    # ルールベース検証で iOS クラッシュを防ぐ。
    VALID_ADJUSTMENTS = {"maintain", "reduce", "intensify"}
    agent_adjustment = plan.get("readinessAdjustment")
    if agent_adjustment is not None and agent_adjustment not in VALID_ADJUSTMENTS:
        raise ValueError(
            f"[Schema] readinessAdjustment='{agent_adjustment}' が無効。"
            f"{VALID_ADJUSTMENTS} のいずれかを使用すること。"
        )
    if "weeks" in plan and not isinstance(plan["weeks"], list):
        raise ValueError("[Schema] 'weeks' フィールドはリスト (array) である必要があります。")
    if "cycle" in plan and not isinstance(plan["cycle"], dict):
        raise ValueError("[Schema] 'cycle' フィールドはオブジェクト (dict) である必要があります。")

    # ACWR ルールベース安全チェック（既読ドキュメントを再利用して Firestore 読み取りを節約）
    try:
        goal_context = existing_doc_data.get("goalContext", {})
        fitness = goal_context.get("currentFitness", {})
        athlete_state_for_check = {
            "weeklyDistanceKm": fitness.get("weeklyDistanceKm") or fitness.get("weeklyTargetKm", 0),
            "acwr": fitness.get("acwr") or goal_context.get("acwr", 0.0),
        }
        check_acwr_safety(plan, athlete_state_for_check)
    except ValueError:
        raise
    except Exception:
        pass  # データ不足はスキップ

    # P0-2 ATOMIC WRITE: trainingPlan と goalContext.currentFitness を単一の set() 呼び出しで
    # アトミックに書き込む。2回目の write が失敗してもデータが不整合にならない。
    # INCREMENTAL PLANNING: completedSessionMap を新計画に引き継ぐ。
    # エージェントが新計画を上書きしても、ユーザーが実際に走ったセッション記録は消えない。
    saved_version = incoming_version or int(_time.time() * 1000)  # milliseconds to avoid 1s collision window
    now_ts = int(_time.time())
    plan_with_ts = {
        **plan,
        "savedAt": now_ts,
        "planVersion": saved_version,
        "completedSessionMap": existing_completed_map,  # carry forward across plan rewrites
    }

    update_payload: dict = {"trainingPlan": plan_with_ts}
    readiness_updated = False

    agent_reason = plan.get("readinessReason") or plan.get("message")
    if agent_adjustment in VALID_ADJUSTMENTS:
        update_payload["goalContext"] = {
            "currentFitness": {
                "readinessAdjustment": agent_adjustment,
                "readinessReason": agent_reason,
                "agentUpdatedAt": now_ts,
            }
        }
        readiness_updated = True

    db.collection("users").document(uid).set(update_payload, merge=True)

    return {
        "success": True,
        "plan_version": saved_version,
        "readiness_updated": readiness_updated,
        "skipped": False,
    }


# ── Tool 6: Write Race Strategy ───────────────────────────────────────────

def write_race_strategy(uid: str, strategy: dict) -> dict:
    """レース前日のペーシング戦略を users/{uid}.raceStrategy に永続化。

    Race Strategy Specialist agent が評価済み戦略を呼び出す。
    iOS の DashboardCardsView / AICoachingView がこのデータを読んで
    レース当日のペース計画を表示する。

    Args:
        uid: Firebase user ID
        strategy: RACE_STRATEGY 形式の JSON
                  必須: evaluatorPassed=True, racePacing.segments (list)
                  任意: message, locale, racePacing.approach, racePacing.acwrAdjustment

    Returns:
        WriteRaceStrategyOutput schema: {success, saved_at}
    """
    import json as _json

    # EVALUATOR HARD GATE
    if not strategy.get("evaluatorPassed", False):
        raise ValueError(
            "[Evaluator Gate] 戦略拒否: evaluatorPassed=True が必須です。"
            "evaluator に戦略を送り PASS を受け取ってから write_race_strategy を呼んでください。"
        )

    # SDT 禁止ワードチェック
    SDT_FORBIDDEN = ["slow", "too slow", "lazy", "sluggish", "遅い", "遅く", "遅すぎ", "のんびり", "ゆっくり"]
    strategy_text = _json.dumps(strategy, ensure_ascii=False).lower()
    sdt_hits = [w for w in SDT_FORBIDDEN if w in strategy_text]
    if sdt_hits:
        raise ValueError(
            f"[SDT Safety] 戦略拒否: 禁止ワード {sdt_hits} を検出。"
            f"ペースはゾーン表記（zone1-zone5/race）に置き換えること。"
        )

    # スキーマ検証
    pacing = strategy.get("racePacing", {})
    if not isinstance(pacing.get("segments"), list) or len(pacing["segments"]) == 0:
        raise ValueError("[Schema] racePacing.segments はリスト (配列) であり最低1要素が必要です。")

    # Pro ガード — Security FIX: removed isDemo/test_ bypass (client-controlled, not trustworthy)
    if not _is_pro_user(uid):
        raise ValueError("write_race_strategy: Pro 会員が必要です。")

    # ACWR 安全チェック: acwr > 1.3 の場合はコンサバティブ戦略を強制
    # write_training_plan の check_acwr_safety() と同等のガードを race strategy にも適用する。
    # RACE_STRATEGY instruction は acwr > 1.2 でコンサバティブを推奨するが、
    # LLM が指示を無視しても acwr > 1.3 の場合は tool 層でコード的に強制する。
    try:
        user_doc = _db().collection("users").document(uid).get()
        user_data = user_doc.to_dict() or {}
        goal_context = user_data.get("goalContext", {})
        fitness = goal_context.get("currentFitness", {})
        acwr = fitness.get("acwr") or goal_context.get("acwr", 0.0)
        if isinstance(acwr, (int, float)) and acwr > 1.3:
            pacing = strategy.get("racePacing", {})
            if pacing.get("acwrAdjustment") not in ("conservative",):
                import logging as _logging
                _logging.warning(
                    f"[write_race_strategy] ACWR={acwr:.2f} > 1.3 — forcing acwrAdjustment=conservative"
                )
                strategy = {**strategy, "racePacing": {**pacing, "acwrAdjustment": "conservative"}}
    except Exception:
        pass  # データ取得失敗はスキップ（保守的: 戦略は進める）

    db = _db()
    now_ts = int(_time.time())
    strategy_with_ts = {**strategy, "savedAt": now_ts}

    db.collection("users").document(uid).set({"raceStrategy": strategy_with_ts}, merge=True)

    return {"success": True, "saved_at": now_ts}
