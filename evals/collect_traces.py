"""Real-trace eval pipeline — P3-1 実装

用途:
  Firestore の coaching_conversations コレクションから実ユーザー対話を収集し、
  高品質サンプルを eval データセット形式に変換する。

  - ユーザー評価 ≥ 4 のターンのみ収集（高品質フィルタ）
  - GDPR: UID は HMAC-SHA256 で仮名化してから出力
  - 収集したサンプルは JSONL 形式で保存し、run_eval.py に追加可能

実行:
  python -m evals.collect_traces --output results/real_traces.jsonl --limit 200
  python -m evals.collect_traces --dry-run  # Firestore 接続なしで構造確認

その後、収集サンプルを EVAL_DATASET に組み込む:
  python -m evals.collect_traces --merge results/real_traces.jsonl --output evals/real_eval_dataset.py
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Firebase Admin SDK
try:
    from firebase_admin import credentials, firestore, get_app, initialize_app
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False
    print("[warn] firebase_admin not installed — run: pip install firebase-admin")

PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "runroute-476505")
DATABASE_ID = os.getenv("FIREBASE_DATABASE_ID", "routerun-db")
_raw_pseudonymization_key = os.getenv("PSEUDONYMIZATION_SECRET")
if not _raw_pseudonymization_key:
    raise RuntimeError(
        "PSEUDONYMIZATION_SECRET env var is required. "
        "Set it to a stable secret key — never rotate it, as GDPR Art.17 erasure "
        "relies on this key being constant to re-pseudonymize records before deletion."
    )
PSEUDONYMIZATION_KEY = _raw_pseudonymization_key

# 高品質サンプルの最低評価スコア（1-5）
MIN_RATING = 4

# 1コレクションから収集する最大サンプル数（コスト/レート制限対策）
MAX_SAMPLES_PER_RUN = 500


def _db() -> Any:
    """Firestore クライアントを返す。"""
    if not FIREBASE_AVAILABLE:
        raise RuntimeError("firebase_admin が利用できません。pip install firebase-admin を実行してください。")
    try:
        get_app()
    except ValueError:
        cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if cred_path:
            initialize_app(credentials.Certificate(cred_path))
        else:
            initialize_app()
    return firestore.client(database_id=DATABASE_ID)


def _pseudonymize_uid(uid: str) -> str:
    """HMAC-SHA256 でユーザー UID を仮名化する。"""
    return hmac.new(
        PSEUDONYMIZATION_KEY.encode(),
        f"uid:{uid}".encode(),
        hashlib.sha256,
    ).hexdigest()[:16]


def _extract_eval_sample(
    user_msg: str,
    assistant_msg: str,
    rating: float,
    uid_hash: str,
    coaching_tone: str,
    training_phase: str | None,
) -> dict[str, Any]:
    """会話ターンを eval サンプル形式に変換する。"""
    # athlete_state はプロキシとして空オブジェクト（実際のデータは goalContext にあるが
    # 仮名化後の eval では参照不可。コーチ応答の quality gate 評価に絞る）
    return {
        "athlete_state": json.dumps({
            "trainingPhase": training_phase or "unknown",
            "coachingTone": coaching_tone,
        }),
        "response": assistant_msg[:2000],  # 最大 2000 文字
        "known_violations": [],  # 高品質フィルタ済み → violations なし（precision 用）
        "label": f"REAL_TRACE_{uid_hash}_{rating:.0f}star",
        "source": "real_user",
        "rating": rating,
    }


def collect_traces(
    output_path: str,
    limit: int = 200,
    dry_run: bool = False,
    min_rating: int = MIN_RATING,
) -> list[dict[str, Any]]:
    """Firestore から実トレースを収集して JSONL に保存する。

    Args:
        output_path: 出力先 JSONL ファイルパス
        limit: 収集する最大サンプル数
        dry_run: True のとき Firestore に接続せず空リストを返す
        min_rating: 最低評価スコア（1-5）

    Returns:
        収集したサンプルリスト
    """
    if dry_run:
        print("[dry-run] Firestore に接続せずサンプル構造を表示します")
        sample = _extract_eval_sample(
            user_msg="今週のトレーニングはどうすればいいですか？",
            assistant_msg="📊 現状分析...",
            rating=4.5,
            uid_hash="dryrun0000000000",
            coaching_tone="caring",
            training_phase="base_building",
        )
        print(json.dumps(sample, ensure_ascii=False, indent=2))
        return []

    if not FIREBASE_AVAILABLE:
        print("[error] firebase_admin が利用できません")
        return []

    db = _db()
    samples: list[dict[str, Any]] = []
    collected = 0

    print(f"[collect] Firestore から実トレースを収集中... limit={limit}, min_rating={min_rating}")

    # coaching_conversations は users/{uid}/coaching_conversations/{convId} の構造
    # トップレベルの collection group クエリで全ユーザーのデータを取得
    try:
        conv_query = db.collection_group("coaching_conversations").limit(MAX_SAMPLES_PER_RUN)
        docs = list(conv_query.stream())
        print(f"[collect] {len(docs)} 件の会話ドキュメントを取得")
    except Exception as e:
        print(f"[error] collection_group クエリ失敗: {e}")
        return []

    for doc in docs:
        if collected >= limit:
            break
        try:
            data = doc.to_dict() or {}
            messages: list[dict] = data.get("messages", [])
            uid = doc.reference.parent.parent.id if doc.reference.parent.parent else "unknown"
            uid_hash = _pseudonymize_uid(uid)
            coaching_tone = data.get("coachingTone", "caring")
            training_phase = data.get("trainingPhase")

            # メッセージペアを走査してユーザー→アシスタントのターンを抽出
            for i in range(len(messages) - 1):
                if collected >= limit:
                    break
                user_m = messages[i]
                asst_m = messages[i + 1]
                if user_m.get("role") != "user" or asst_m.get("role") != "assistant":
                    continue

                # フィードバック評価（feedbackRating フィールドまたはターンレベルの rating）
                rating = asst_m.get("feedbackRating") or data.get("feedbackRating", 0)
                if not isinstance(rating, (int, float)) or rating < min_rating:
                    continue  # 低評価スキップ

                user_content = str(user_m.get("content", ""))[:500]
                asst_content = str(asst_m.get("content", ""))[:2000]
                if not user_content or not asst_content:
                    continue

                sample = _extract_eval_sample(
                    user_msg=user_content,
                    assistant_msg=asst_content,
                    rating=float(rating),
                    uid_hash=uid_hash,
                    coaching_tone=coaching_tone,
                    training_phase=training_phase,
                )
                samples.append(sample)
                collected += 1

        except Exception as e:
            print(f"[warn] doc={doc.id} スキップ: {e}")
            continue

    print(f"[collect] {len(samples)} 件の高品質サンプルを収集")

    # JSONL 形式で保存
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"[collect] 保存: {output_path}")

    return samples


def merge_into_eval_dataset(
    jsonl_path: str,
    output_py_path: str,
    max_samples: int = 100,
) -> None:
    """収集したトレースを EVAL_DATASET 形式の Python ファイルに変換する。

    run_eval.py の EVAL_DATASET に手動でマージするためのヘルパー。
    """
    samples = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                samples.append(json.loads(line))

    samples = samples[:max_samples]
    print(f"[merge] {len(samples)} サンプルを {output_py_path} に変換")

    lines = [
        "# Auto-generated real-trace eval samples",
        f"# Source: {jsonl_path}",
        f"# Generated: {datetime.now(timezone.utc).isoformat()}",
        f"# Samples: {len(samples)}",
        "",
        "REAL_EVAL_DATASET = [",
    ]
    for s in samples:
        lines.append(f"    {json.dumps(s, ensure_ascii=False)},")
    lines.append("]")

    with open(output_py_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[merge] 完了: {output_py_path}")
    print("  → run_eval.py の EVAL_DATASET に REAL_EVAL_DATASET を結合して使用してください")


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect real coaching traces for eval")
    parser.add_argument("--output", default="results/real_traces.jsonl", help="出力 JSONL パス")
    parser.add_argument("--limit", type=int, default=200, help="収集する最大サンプル数")
    parser.add_argument("--min-rating", type=int, default=MIN_RATING, help="最低評価スコア (1-5)")
    parser.add_argument("--dry-run", action="store_true", help="Firestore 接続なしで構造確認")
    parser.add_argument(
        "--merge", metavar="JSONL_PATH",
        help="収集済み JSONL を EVAL_DATASET 形式の Python ファイルに変換"
    )
    parser.add_argument("--merge-output", default="evals/real_eval_dataset.py")
    parser.add_argument("--merge-limit", type=int, default=100)
    args = parser.parse_args()

    if args.merge:
        merge_into_eval_dataset(args.merge, args.merge_output, args.merge_limit)
    else:
        collect_traces(
            output_path=args.output,
            limit=args.limit,
            dry_run=args.dry_run,
            min_rating=args.min_rating,
        )


if __name__ == "__main__":
    main()
