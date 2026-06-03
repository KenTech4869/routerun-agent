"""RouteRun Cloud Functions / Firestore wrappers for ADK agents.

Schema source: functions/src/ (R4 確認済み 2026-06-03)
  - onRunCompleted.ts: run_history フィールド (userId, distance, duration, pace, date, acwr, ...)
  - recommendEventsV3.ts: RecommendationResponse {recommendations, totalCandidates}
  - suggestRouteAreas.ts: SuggestRouteAreasResponse {areas, model, responseTimeMs, cached}
  - callable envelope: {"data": {...}} / response: {"result": {...}}
"""

import os
import requests
from typing import Optional
from firebase_admin import firestore, initialize_app, credentials, get_app

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


# ── Tool 1: Athlete State ──────────────────────────────────────────────────

def get_athlete_state(uid: str) -> dict:
    """走者の現在の fitness state を返す。計画サイクルの最初に必ず呼ぶ。

    Returns:
        goalContext dict with keys:
          acwr (float): Acute:Chronic Workload Ratio (目安: 0.8-1.3 が安全域)
          trainingPhase (str): "base_building" | "build" | "peak" | "taper" | "recovery"
          weeklyVolumeTrend (str): "increasing" | "stable" | "decreasing"
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
        route_type: "loop" | "outAndBack" | "scenic"
        pace_zone: "zone1" | "zone2" | "zone3" | "zone4" | "zone5" | "race"
        training_phase: "base_building" | "build" | "peak" | "taper" | "recovery"

    Returns:
        SuggestRouteAreasResponse:
          areas (list): [{name, description, waypointCoordinates, estimatedQuality, tags}]
          model (str): 使用モデル ID
          responseTimeMs (int)
          cached (bool)
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

    Returns:
        RecommendationResponse:
          recommendations (list): [{eventId, score, reasons, reasonCodes}]
          totalCandidates (int)
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


# ── Tool 5: Write Training Plan (Memory Bank) ─────────────────────────────

def write_training_plan(uid: str, plan: dict) -> bool:
    """評価済み計画を users/{uid}.trainingPlan に永続化 (Memory Bank)。

    Agent Engine から直接 Firestore に書く（callable 不要）。
    isDemo=true が付いていない計画は拒否する（安全ガード）。
    """
    if not plan.get("isDemo", False) and not uid.startswith("test_"):
        raise ValueError(
            "write_training_plan: 本番ユーザーへの書き込みはデモ期間中禁止。"
            "uid を test_ プレフィックスにするか isDemo=true を付けること。"
        )
    db = _db()
    db.collection("users").document(uid).set(
        {"trainingPlan": plan},
        merge=True,
    )
    return True
