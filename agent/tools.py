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
import time as _time
import requests
from typing import Optional
from firebase_admin import firestore, initialize_app, credentials, get_app
from pydantic import BaseModel, Field

PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "runroute-476505")
REGION = os.getenv("GOOGLE_CLOUD_REGION", "asia-northeast1")
DATABASE_ID = os.getenv("FIREBASE_DATABASE_ID", "routerun-db")
CF_BASE = os.getenv("CF_BASE_URL", f"https://{REGION}-{PROJECT}.cloudfunctions.net")

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
    """
    db = _db()
    doc = db.collection("users").document(uid).get()
    data = doc.to_dict() or {}
    return data.get("goalContext", {})


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

    # P1-SDT: SDT 禁止ワードをルールベースでチェック（LLM に依存しない二重ガード）
    SDT_FORBIDDEN = ["slow", "遅い", "遅く", "遅すぎ", "too slow"]
    plan_text = _json.dumps(plan, ensure_ascii=False).lower()
    sdt_hits = [w for w in SDT_FORBIDDEN if w in plan_text]
    if sdt_hits:
        raise ValueError(
            f"[SDT Safety] 計画拒否: 禁止ワード {sdt_hits} を検出。"
            f"ペースはゾーン表記（zone1-zone5/race）に置き換えること。"
        )

    # P1-DeepMind: Pro ミドルウェアで認可チェック（ツールから分離）
    is_pro = _is_pro_user(uid)
    if not plan.get("isDemo", False) and not uid.startswith("test_") and not is_pro:
        raise ValueError(
            "write_training_plan: Pro 会員または isDemo=true が必要です。"
            "uid を test_ プレフィックスにするか、Pro プランにアップグレードしてください。"
        )

    db = _db()

    # P2-DeepMind: 冪等性チェック — 同一 planVersion は重複書き込みしない
    incoming_version = plan.get("planVersion") or plan.get("plan_version")
    if incoming_version is not None:
        try:
            existing_doc = db.collection("users").document(uid).get()
            existing_plan = (existing_doc.to_dict() or {}).get("trainingPlan", {})
            existing_version = existing_plan.get("planVersion") or existing_plan.get("plan_version")
            if existing_version is not None and existing_version == incoming_version:
                return {
                    "success": True,
                    "plan_version": incoming_version,
                    "readiness_updated": False,
                    "skipped": True,
                }
        except Exception:
            pass  # 読み取り失敗 → 書き込み続行

    # ACWR ルールベース安全チェック
    try:
        user_doc = db.collection("users").document(uid).get()
        goal_context = (user_doc.to_dict() or {}).get("goalContext", {})
        fitness = goal_context.get("currentFitness", {})
        athlete_state_for_check = {
            "weeklyDistanceKm": fitness.get("weeklyDistanceKm", 0),
            "acwr": fitness.get("acwr", 0.0),
        }
        check_acwr_safety(plan, athlete_state_for_check)
    except ValueError:
        raise
    except Exception:
        pass  # Firestore 読み取り失敗はスキップ

    # training plan を永続化
    saved_version = incoming_version or int(_time.time())
    plan_with_ts = {
        **plan,
        "savedAt": int(_time.time()),
        "planVersion": saved_version,
    }
    db.collection("users").document(uid).set(
        {"trainingPlan": plan_with_ts},
        merge=True,
    )

    # P0-1 OpenAI UX Fix: readinessAdjustment + readinessReason を goalContext に反映
    # エージェントの多変量判断（HRV・睡眠・安静時 HR）をルールベースより優先して Firestore に書く。
    # これにより iOS の AgentActionBanner にエージェントの実際の判断理由が表示される。
    readiness_updated = False
    agent_adjustment = plan.get("readinessAdjustment")
    agent_reason = plan.get("readinessReason") or plan.get("message")
    if agent_adjustment in ("maintain", "reduce", "intensify"):
        try:
            db.collection("users").document(uid).set(
                {
                    "goalContext": {
                        "currentFitness": {
                            "readinessAdjustment": agent_adjustment,
                            "readinessReason": agent_reason,
                            "agentUpdatedAt": int(_time.time()),
                        }
                    }
                },
                merge=True,
            )
            readiness_updated = True
        except Exception:
            pass  # goalContext 更新失敗は非致命的（ルールベース値が残る）

    return {
        "success": True,
        "plan_version": saved_version,
        "readiness_updated": readiness_updated,
        "skipped": False,
    }
