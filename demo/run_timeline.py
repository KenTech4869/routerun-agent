"""Compressed timeline demo — RouteRun Agentic Coaching.

8週分の走行データを時系列で replay し、Head Coach が自律的に計画を
修正する瞬間（Week3 perturbation → re-plan）を録画・ログ出力する。

実行方法:
  # デプロイ済み Agent Engine を使用 (本番デモ)
  AGENT_ENGINE_RESOURCE_NAME=<resource_name> python -m demo.run_timeline

  # ローカル Agent (Agent Engine 不要、開発用)
  python -m demo.run_timeline --local

  # テストユーザー指定
  python -m demo.run_timeline --uid test_sub4

出力:
  - コンソール: 各週の計画 + Evaluator 判定
  - logs/timeline_<uid>_<timestamp>.json: 全サイクルの記録
  - Week3 到達時に ★ PERTURBATION → RE-PLAN ★ を強調表示
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import firebase_admin
from firebase_admin import credentials, firestore

PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "runroute-476505")
REGION = os.getenv("GOOGLE_CLOUD_REGION", "asia-northeast1")
DATABASE_ID = os.getenv("FIREBASE_DATABASE_ID", "routerun-db")
AGENT_ENGINE_RESOURCE_NAME = os.getenv("AGENT_ENGINE_RESOURCE_NAME", "")


def _init_firebase():
    try:
        firebase_admin.get_app()
    except ValueError:
        cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        options = {"projectId": PROJECT}
        if cred_path:
            firebase_admin.initialize_app(credentials.Certificate(cred_path), options)
        else:
            firebase_admin.initialize_app(options=options)


def _invoke_local(uid: str, week: int) -> dict[str, Any]:
    """ローカル ADK agent を直接呼び出す（Agent Engine 不要）。"""
    from agent.agents import head_coach

    prompt = (
        f"uid: {uid}\n"
        f"Week {week} の走行データを受け取った。"
        f"get_athlete_state で現在の fitness state を確認し、"
        f"Week {week+1} に向けたトレーニング計画を立てよ。"
        f"必ず evaluator に通してから最終出力すること。"
    )
    result = head_coach.invoke(prompt)
    return {"raw": str(result), "week": week, "uid": uid}


def _invoke_remote(uid: str, week: int) -> dict[str, Any]:
    """デプロイ済み Agent Engine を呼び出す。"""
    import asyncio
    import vertexai
    from vertexai import agent_engines

    vertexai.init(project=PROJECT, location=REGION)
    remote_agent = agent_engines.get(AGENT_ENGINE_RESOURCE_NAME)

    prompt = (
        f"uid: {uid}\n"
        f"Week {week} の走行データを受け取った。"
        f"get_athlete_state で現在の fitness state を確認し、"
        f"Week {week+1} に向けたトレーニング計画を立てよ。"
        f"必ず evaluator に通してから最終出力すること。"
    )

    async def _run():
        session = await remote_agent.async_create_session(user_id=uid)
        session_id = session.get("id") or session.get("name", "").split("/")[-1]
        events = []
        async for event in remote_agent.async_stream_query(
            message=prompt,
            user_id=uid,
            session_id=session_id,
        ):
            events.append(event)
        raw = "\n".join(str(e) for e in events)
        return {"raw": raw, "week": week, "uid": uid, "session_id": session_id}

    return asyncio.run(_run())


_WEEK_GOAL_CONTEXT_PATCHES: dict[int, dict] = {
    # Week 3: HRV 低下 / 睡眠不足 / 安静時 HR 上昇 → Readiness が "reduce" を返すはず
    3: {
        "readinessSignals": {
            "hrv": 28.0,          # 通常 55+ → 低下
            "sleepHours": 5.2,    # <6h 睡眠不足
            "restingHR": 68.0,    # 通常 58 → +10bpm 上昇
        },
        "acwr": 1.28,             # ACWR ほぼ上限 (1.3)
        "weeklyVolumeTrend": "decreasing",
    },
    # Week 4: 回復後の正常化
    4: {
        "readinessSignals": {
            "hrv": 50.0,
            "sleepHours": 7.2,
            "restingHR": 59.0,
        },
        "acwr": 1.05,
        "weeklyVolumeTrend": "increasing",
    },
}


def _patch_goal_context(uid: str, week: int) -> None:
    """週ごとの体調シグナルを Firestore goalContext に反映する。
    Agent Engine が get_athlete_state で読む際に正確な readiness データを参照できる。
    """
    patch = _WEEK_GOAL_CONTEXT_PATCHES.get(week)
    if not patch:
        return
    db = firestore.client(database_id=DATABASE_ID)
    db.collection("users").document(uid).set({"goalContext": patch}, merge=True)


def _read_training_plan(uid: str) -> dict | None:
    """Firestore から最新の trainingPlan を読む。"""
    db = firestore.client(database_id=DATABASE_ID)
    doc = db.collection("users").document(uid).get()
    data = doc.to_dict() or {}
    return data.get("trainingPlan")


def _print_plan_summary(week: int, result: dict, is_perturbation: bool) -> None:
    """計画サイクルの結果をコンソールに整形出力。"""
    sep = "=" * 60
    if is_perturbation:
        print(f"\n{sep}")
        print("★ PERTURBATION DETECTED — RE-PLAN IN PROGRESS ★")
        print(f"  Week {week}: HRV 低下 / HR上昇 / ロング走スキップ → Readiness: reduce")
        print(sep)
    else:
        print(f"\n── Week {week} Planning Cycle ──")

    raw = result.get("raw", "")
    # trainingPlan から最新値を表示
    plan = _read_training_plan(result["uid"])
    if plan:
        print(f"  readinessAdjustment : {plan.get('readinessAdjustment', 'N/A')}")
        print(f"  weeklyTargetKm      : {plan.get('cycle', {}).get('weeklyTargetKm', 'N/A')}")
        print(f"  evaluatorPassed     : {plan.get('evaluatorPassed', 'N/A')}")
        msg = plan.get("message", "")[:120]
        print(f"  message             : {msg}...")
    else:
        print(f"  [trainingPlan not yet written] raw={raw[:200]}")


def run_timeline(uid: str, use_local: bool = False) -> None:
    """8週分の圧縮タイムラインを実行。"""
    _init_firebase()
    log_records = []
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = Path(f"logs/timeline_{uid}_{timestamp}.json")
    log_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\nRouteRun — Compressed Timeline Demo")
    print(f"  uid      : {uid}")
    print(f"  mode     : {'local ADK' if use_local else 'Agent Engine'}")
    print(f"  log      : {log_path}")
    print(f"  weeks    : 1–8 (Week3 = perturbation)\n")

    for week in range(1, 9):
        is_perturbation = (week == 3)
        t0 = time.time()

        # 週ごとの体調シグナルを Firestore に書き込む (Week3: HRV低下/睡眠不足/HR上昇)
        _patch_goal_context(uid, week)

        try:
            if use_local:
                result = _invoke_local(uid, week)
            else:
                if not AGENT_ENGINE_RESOURCE_NAME:
                    print("ERROR: AGENT_ENGINE_RESOURCE_NAME 未設定。--local を使うか .env を設定してください。")
                    sys.exit(1)
                result = _invoke_remote(uid, week)

            elapsed = time.time() - t0
            result["elapsed_sec"] = round(elapsed, 1)
            result["is_perturbation"] = is_perturbation
            log_records.append(result)

            _print_plan_summary(week, result, is_perturbation)
            print(f"  [elapsed: {elapsed:.1f}s]")

        except Exception as e:
            print(f"  ERROR week {week}: {e}")
            log_records.append({"week": week, "error": str(e), "uid": uid})

        # 各週の間に少し待機（レート制限対策）
        if week < 8:
            time.sleep(2)

    # ── サマリー ─────────────────────────────────────────────────────────
    final_plan = _read_training_plan(uid)
    print(f"\n{'='*60}")
    print("TIMELINE COMPLETE")
    print(f"{'='*60}")
    if final_plan:
        print(f"  Final trainingPlan written to Firestore ✅")
        print(f"  phase            : {final_plan.get('cycle', {}).get('phase', 'N/A')}")
        print(f"  weeklyTargetKm   : {final_plan.get('cycle', {}).get('weeklyTargetKm', 'N/A')}")
        print(f"  evaluatorPassed  : {final_plan.get('evaluatorPassed', 'N/A')}")
    else:
        print("  ⚠️  trainingPlan not found in Firestore. Check agent logs.")

    with open(log_path, "w", encoding="utf-8") as f:
        json.dump({"uid": uid, "records": log_records, "final_plan": final_plan},
                  f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  Full log: {log_path}")


def main():
    parser = argparse.ArgumentParser(description="RouteRun compressed timeline demo")
    parser.add_argument("--uid", default="test_sub4", choices=["test_sub4", "test_firstfull"])
    parser.add_argument("--local", action="store_true", help="Use local ADK (no Agent Engine)")
    args = parser.parse_args()

    run_timeline(uid=args.uid, use_local=args.local)


if __name__ == "__main__":
    main()
