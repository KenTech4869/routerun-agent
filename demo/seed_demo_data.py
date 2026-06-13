"""Seed demo data into Firestore for the compressed timeline demo.

デモユーザー:
  test_sub4     — サブ4目標・intermediate・週3-4回・Week3にHRV低下 perturbation
  test_firstfull — 初フル・beginner・週2-3回・Week3にロング走スキップ perturbation

実行方法:
  # dry-run (書き込みなし・出力確認のみ)
  python -m demo.seed_demo_data --dry-run

  # 実際に書き込む
  python -m demo.seed_demo_data

  # クリーンアップ (isDemo=true ドキュメントを全削除)
  python -m demo.seed_demo_data --cleanup

Pro custom claim: { tier: "pro" } — suggestRouteAreas 等の Pro 限定 CF を呼ぶために必要

ID トークン取得 (callable 用):
  python -m demo.seed_demo_data --get-tokens
"""

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import firebase_admin
import requests
from firebase_admin import auth, credentials, firestore
from google.cloud.firestore_v1 import SERVER_TIMESTAMP

DATABASE_ID = os.getenv("FIREBASE_DATABASE_ID", "routerun-db")
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "runroute-476505")
WEB_API_KEY = os.getenv("FIREBASE_WEB_API_KEY", "")  # Firebase コンソール > プロジェクト設定 > ウェブ API キー

# デモユーザー (demo@routerun.jp のみ)
DEMO_USERS = [
    {
        "uid": "d2sEnjl1tlWN7BVJXAYkB7eBOys2",
        "display_name": "Demo User",
        "email": "demo@routerun.jp",
        "locale": "ja",
        "goal": "fullMarathon",
        "level": "intermediate",
        "target_pace_min_per_km": 5.67,   # 5:40/km × 42.195km = 3:59:50 → サブ4レースペース
        "weekly_distance_km": 52.0,
        "vo2max": 48.0,
        "race_name": "東京マラソン 2027",
        "race_distance_km": 42.195,
        "race_date": "2027-03-07",
        "race_event_id": "tokyo-marathon-2027",
    },
]


def _init_firebase():
    try:
        firebase_admin.get_app()
    except ValueError:
        cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if cred_path:
            firebase_admin.initialize_app(credentials.Certificate(cred_path))
        else:
            firebase_admin.initialize_app()


def _db():
    return firestore.client(database_id=DATABASE_ID)


# ── run_history schema (R4 confirmed) ─────────────────────────────────────

def _make_run_doc(
    uid: str,
    week: int,
    day_offset: int,
    distance_km: float,
    pace_min_per_km: float,
    heart_rate: float,
    is_perturbation: bool = False,
    skip: bool = False,
) -> dict[str, Any] | None:
    """1回分の run_history ドキュメントを生成。skip=True なら None を返す（ロング走スキップ）。"""
    if skip:
        return None

    base_date = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(weeks=8)
    run_date = base_date + timedelta(weeks=week - 1, days=day_offset)
    duration_sec = int(distance_km * pace_min_per_km * 60)

    # ACWR: Week1=0.80, Week2=0.90, Week3 perturbation=1.25, Week4+=1.00〜1.10
    acwr_map = {1: 0.80, 2: 0.90, 3: 1.25 if is_perturbation else 1.0, 4: 1.05,
                5: 1.10, 6: 1.08, 7: 1.05, 8: 0.90}
    acwr = acwr_map.get(week, 1.0)

    return {
        "userId": uid,
        "distance": distance_km,
        "duration": duration_sec,
        "pace": pace_min_per_km,
        "date": run_date,
        "acwr": acwr,
        "heartRate": heart_rate,
        "isPB": week >= 5 and day_offset == 6,  # ロング走で自己ベスト
        "source": "appleWatch",
        "isDemo": True,
    }


def _build_run_history(user: dict) -> list[dict[str, Any]]:
    """8週分の run_history を生成（Week3 に perturbation）。"""
    uid = user["uid"]
    base_pace = user["target_pace_min_per_km"]
    base_weekly = user["weekly_distance_km"]
    level = user["level"]

    runs = []
    for week in range(1, 9):
        # 週間距離: +5〜8% 漸進（Week3 は perturbation で乱れ）
        weekly_multiplier = 1.0 + (week - 1) * 0.06
        if week == 3:
            weekly_multiplier = 0.7  # perturbation: 体調不良/疲労蓄積で距離減

        weekly_km = base_weekly * weekly_multiplier

        if level == "beginner":
            # 週2-3回: 短め Easy + ロング走
            sessions = [
                (1, weekly_km * 0.30, base_pace + 0.5, 140.0, False),   # Mon: Easy
                (4, weekly_km * 0.30, base_pace + 0.5, 138.0, False),   # Thu: Easy
                (6, weekly_km * 0.40, base_pace + 0.8, 135.0, week == 3),  # Sat: Long (skip on week3)
            ]
        else:
            # 週3-4回: Easy (zone2) + Tempo (LT) + ロング走
            # base_pace = レースペース(5:40/km)。イージーは +0.8 = 6:28/km でゾーン2に収まる
            sessions = [
                (1, weekly_km * 0.25, base_pace + 0.8, 138.0, False),  # Mon: Easy zone2
                (3, weekly_km * 0.20, base_pace - 0.3, 158.0, False),  # Wed: Tempo LT
                (5, weekly_km * 0.20, base_pace + 0.8, 135.0, False),  # Fri: Easy zone2
                (6, weekly_km * 0.35, base_pace + 0.7, 136.0, False),  # Sat: Long easy-mod
            ]

        for day_offset, dist, pace, hr, skip in sessions:
            doc = _make_run_doc(
                uid=uid,
                week=week,
                day_offset=day_offset,
                distance_km=round(dist, 2),
                pace_min_per_km=round(pace, 2),
                heart_rate=hr + (10.0 if week == 3 else 0),  # Week3: HR上昇
                is_perturbation=(week == 3),
                skip=skip,
            )
            if doc is not None:
                runs.append(doc)

    return runs


def _build_goal_context(user: dict) -> dict[str, Any]:
    """users/{uid}.goalContext を生成。

    デモシナリオ: Week3 perturbation 直後の状態（ACWR=1.25 の疲労蓄積ピーク）から
    Week4 の計画を立てる場面。エージェントが ACWR 高値を検知して安全な負荷調整を
    提案する動きを見せるため、Week8 最終値ではなく Week3 直後の値を使う。
    """
    weekly_km = user["weekly_distance_km"]
    return {
        "acwr": 1.25,  # Week3 perturbation 直後 — エージェントに ACWR 警戒を見せる
        "trainingPhase": "base_building",
        "weeklyVolumeTrend": "increasing",
        "todayWorkout": None,
        "raceTarget": {
            "eventId": user["race_event_id"],
            "eventName": user["race_name"],
            "date": user["race_date"],
            "distanceKm": user["race_distance_km"],
        },
        "currentFitness": {
            "vo2MaxEstimate": user["vo2max"],
            "recentPaceMinPerKm": user["target_pace_min_per_km"],
            "monthlyDistanceKm": weekly_km * 4,
            # write_training_plan の _check_acwr_safety が読む 2 フィールド
            # weeklyDistanceKm: Week3 の reduced 値ではなく通常ベース値を使う
            #   → +10% cap = sub4: 38.5km / firstfull: 27.5km（適切な計画が通る）
            # ACWR=1.25: code safety は 1.3 超で block → LLM 推論で警告を出しつつ計画が通る
            "weeklyDistanceKm": weekly_km,
            "acwr": 1.25,
        },
        "locale": user["locale"],
        "isDemo": True,
    }


# ── Firebase Auth user creation ────────────────────────────────────────────

def _ensure_auth_user(user: dict, dry_run: bool) -> None:
    """Firebase Auth ユーザーを作成し Pro custom claim を付与。"""
    uid = user["uid"]
    if dry_run:
        print(f"  [dry-run] Would create Auth user: uid={uid}, email={user['email']}")
        print(f"  [dry-run] Would set customClaims: {{tier: 'pro'}}")
        return

    try:
        existing = auth.get_user(uid)
        print(f"  Auth user exists: uid={existing.uid}")
    except auth.UserNotFoundError:
        auth.create_user(
            uid=uid,
            email=user["email"],
            display_name=user["display_name"],
            email_verified=True,
        )
        print(f"  Created Auth user: uid={uid}")

    # Pro custom claim 付与 (suggestRouteAreas などの Pro 限定 CF 用)
    auth.set_custom_user_claims(uid, {"tier": "pro"})
    print(f"  Set customClaims {{tier: 'pro'}} for uid={uid}")


# ── ID トークン取得 (callable 呼び出し用) ──────────────────────────────────

def get_id_token(uid: str) -> str:
    """Custom Token → ID Token 変換 (Firebase REST API)。"""
    if not WEB_API_KEY:
        raise ValueError(
            "FIREBASE_WEB_API_KEY が未設定。.env に追加してください。\n"
            "Firebase Console > プロジェクト設定 > ウェブアプリ > API キー"
        )
    custom_token = auth.create_custom_token(uid).decode("utf-8")
    resp = requests.post(
        f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={WEB_API_KEY}",
        json={"token": custom_token, "returnSecureToken": True},
        timeout=10,
    )
    resp.raise_for_status()
    id_token = resp.json()["idToken"]
    print(f"  ID token obtained for uid={uid} (expires ~1h)")
    return id_token


# ── Main seed / cleanup ───────────────────────────────────────────────────

def seed(dry_run: bool = False) -> None:
    """全テストデータを Firestore に投入。dry_run=True なら出力のみ。"""
    _init_firebase()
    db = _db()

    for user in DEMO_USERS:
        uid = user["uid"]
        print(f"\n── {uid} ({user['display_name']}) ──")

        # 1. Auth user + Pro claim
        _ensure_auth_user(user, dry_run)

        # 2. run_history
        runs = _build_run_history(user)
        print(f"  run_history: {len(runs)} documents to write")
        for run_doc in runs:
            if dry_run:
                print(f"    [dry-run] run_history/{uid}_{run_doc['date'].isoformat()[:10]}: "
                      f"distance={run_doc['distance']}km, pace={run_doc['pace']} min/km")
            else:
                doc_ref = db.collection("run_history").document()
                doc_ref.set(run_doc)
                time.sleep(0.05)  # Firestore rate limit 対策

        # 3. goalContext
        goal_ctx = _build_goal_context(user)
        if dry_run:
            print(f"  [dry-run] users/{uid}.goalContext: {json.dumps(goal_ctx, ensure_ascii=False, default=str)[:120]}...")
        else:
            db.collection("users").document(uid).set(
                {"goalContext": goal_ctx},
                merge=True,
            )
            print(f"  users/{uid}.goalContext written")

    if not dry_run:
        print("\n✅ Seed complete. Verify at: Firebase Console > Firestore > routerun-db")
    else:
        print("\n✅ Dry-run complete. Run without --dry-run to write.")


def cleanup() -> None:
    """isDemo=true のドキュメントを全コレクションから削除。"""
    _init_firebase()
    db = _db()

    for collection in ["run_history", "agent_planning_queue"]:
        docs = db.collection(collection).where("isDemo", "==", True).stream()
        deleted = 0
        for doc in docs:
            doc.reference.delete()
            deleted += 1
        print(f"  Deleted {deleted} docs from {collection}")

    for user in DEMO_USERS:
        uid = user["uid"]
        db.collection("users").document(uid).update({"goalContext": firestore.DELETE_FIELD})
        db.collection("users").document(uid).update({"trainingPlan": firestore.DELETE_FIELD})
        print(f"  Cleaned users/{uid}.goalContext + trainingPlan")

    print("✅ Cleanup complete.")


def main():
    parser = argparse.ArgumentParser(description="RouteRun demo data seeder")
    parser.add_argument("--dry-run", action="store_true", help="Print without writing")
    parser.add_argument("--cleanup", action="store_true", help="Delete all isDemo=true documents")
    parser.add_argument("--get-tokens", action="store_true", help="Generate ID tokens for callable demo")
    args = parser.parse_args()

    _init_firebase()

    if args.cleanup:
        cleanup()
    elif args.get_tokens:
        for user in DEMO_USERS:
            uid = user["uid"]
            print(f"\nGenerating ID token for {uid}...")
            token = get_id_token(uid)
            env_var = f"TEST_ID_TOKEN_{uid.upper().replace('-', '_')}"
            print(f"  {env_var}={token[:40]}...")
    else:
        seed(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
