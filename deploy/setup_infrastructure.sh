#!/usr/bin/env bash
# =============================================================================
# RouteRun GCP Infrastructure Setup Script
# =============================================================================
#
# 実行対象:
#   1. Pub/Sub DLQ — proactive-coach-trigger (新規作成 + サブスクリプション更新)
#   2. Firestore TTL ポリシー — api_counters / daily_usage / route_v2_cache
#   3. Cloud Monitoring アラート — 3ポリシーを個別に deploy
#
# 前提条件:
#   - gcloud auth login && gcloud config set project runroute-476505
#   - onProactiveCoachWorker Cloud Function がデプロイ済みであること
#   - 通知チャネル ID を確認済み (手順 3-A 参照)
#
# 使用方法:
#   chmod +x deploy/setup_infrastructure.sh
#   ./deploy/setup_infrastructure.sh
#
# 注意: DLQ 設定は Cloud Function の再デプロイ後にサブスクリプション名が
# 変わる場合があります。その際は本スクリプトを再実行してください。
# =============================================================================

set -euo pipefail

PROJECT_ID="runroute-476505"
DB_NAME="routerun-db"
TOPIC_PROACTIVE="proactive-coach-trigger"
TOPIC_RUN_ANALYSIS="run-analysis-ready"
DLQ_TOPIC_PROACTIVE="proactive-coach-trigger-dlq"
DLQ_SUB_PROACTIVE="proactive-coach-trigger-dlq-sub"
REGION="asia-northeast1"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*"; exit 1; }
step() { echo -e "\n${YELLOW}--- $* ---${NC}"; }

# ── 認証確認 ──────────────────────────────────────────────────────────────────
step "認証状態確認"
ACCOUNT=$(gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | head -1)
[ -z "$ACCOUNT" ] && err "gcloud にログインしていません。'gcloud auth login' を実行してください。"
ok "認証済みアカウント: $ACCOUNT"

gcloud config set project "$PROJECT_ID" --quiet
ok "プロジェクト設定: $PROJECT_ID"

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
PUBSUB_SA="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"
ok "Pub/Sub サービスアカウント: $PUBSUB_SA"

# =============================================================================
# 1. Pub/Sub DLQ — proactive-coach-trigger
# =============================================================================
step "1. Pub/Sub DLQ — $TOPIC_PROACTIVE"

# 1-A. DLQ トピック作成 (冪等)
if gcloud pubsub topics describe "$DLQ_TOPIC_PROACTIVE" --project="$PROJECT_ID" &>/dev/null; then
  ok "DLQ トピック既存: $DLQ_TOPIC_PROACTIVE"
else
  gcloud pubsub topics create "$DLQ_TOPIC_PROACTIVE" \
    --project="$PROJECT_ID" \
    --message-retention-duration=7d
  ok "DLQ トピック作成: $DLQ_TOPIC_PROACTIVE"
fi

# 1-B. DLQ サブスクリプション作成 (冪等)
if gcloud pubsub subscriptions describe "$DLQ_SUB_PROACTIVE" --project="$PROJECT_ID" &>/dev/null; then
  ok "DLQ サブスクリプション既存: $DLQ_SUB_PROACTIVE"
else
  gcloud pubsub subscriptions create "$DLQ_SUB_PROACTIVE" \
    --topic="$DLQ_TOPIC_PROACTIVE" \
    --project="$PROJECT_ID" \
    --ack-deadline=60 \
    --message-retention-duration=7d
  ok "DLQ サブスクリプション作成: $DLQ_SUB_PROACTIVE"
fi

# 1-C. IAM: Pub/Sub SA → DLQ トピックへ publish 権限付与
gcloud pubsub topics add-iam-policy-binding "$DLQ_TOPIC_PROACTIVE" \
  --project="$PROJECT_ID" \
  --member="serviceAccount:${PUBSUB_SA}" \
  --role="roles/pubsub.publisher" \
  --quiet
ok "IAM: $PUBSUB_SA → $DLQ_TOPIC_PROACTIVE (publisher)"

# 1-D. Cloud Function サブスクリプション名を動的探索
echo "  $TOPIC_PROACTIVE のサブスクリプションを検索中..."
SOURCE_SUB_FULL=$(gcloud pubsub subscriptions list \
  --project="$PROJECT_ID" \
  --filter="topic.path=projects/${PROJECT_ID}/topics/${TOPIC_PROACTIVE} AND NOT name~dlq" \
  --format="value(name)" | head -1)

if [ -z "$SOURCE_SUB_FULL" ]; then
  warn "サブスクリプションが見つかりません。"
  warn "onProactiveCoachWorker を先にデプロイして再実行してください:"
  warn "  cd /Users/suzukikenji/RouteRun/RouteRun_Final_コスト最適化 && firebase deploy --only functions:onProactiveCoachWorker"
  echo ""
  warn "DLQ セットアップをスキップしました (手順 1 は未完)。"
  SKIP_DLQ_UPDATE=true
else
  SOURCE_SUB_ID=$(basename "$SOURCE_SUB_FULL")
  ok "発見したサブスクリプション: $SOURCE_SUB_ID"
  SKIP_DLQ_UPDATE=false
fi

if [ "${SKIP_DLQ_UPDATE:-false}" = false ]; then
  # 1-E. IAM: Pub/Sub SA → ソースサブスクリプションへ subscriber 権限付与
  gcloud pubsub subscriptions add-iam-policy-binding "$SOURCE_SUB_ID" \
    --project="$PROJECT_ID" \
    --member="serviceAccount:${PUBSUB_SA}" \
    --role="roles/pubsub.subscriber" \
    --quiet
  ok "IAM: $PUBSUB_SA → $SOURCE_SUB_ID (subscriber)"

  # 1-F. DLQ 設定をサブスクリプションに適用
  gcloud pubsub subscriptions update "$SOURCE_SUB_ID" \
    --project="$PROJECT_ID" \
    --dead-letter-topic="projects/${PROJECT_ID}/topics/${DLQ_TOPIC_PROACTIVE}" \
    --max-delivery-attempts=5
  ok "DLQ 設定完了: $SOURCE_SUB_ID → $DLQ_TOPIC_PROACTIVE (max-attempts=5)"
fi

# 1-G. run-analysis DLQ 確認
echo ""
RUN_ANALYSIS_SUB=$(gcloud pubsub subscriptions list \
  --project="$PROJECT_ID" \
  --filter="topic.path=projects/${PROJECT_ID}/topics/${TOPIC_RUN_ANALYSIS} AND NOT name~dlq" \
  --format="value(name)" 2>/dev/null | head -1)

if [ -n "$RUN_ANALYSIS_SUB" ]; then
  DLQ_CHECK=$(gcloud pubsub subscriptions describe "$(basename "$RUN_ANALYSIS_SUB")" \
    --project="$PROJECT_ID" \
    --format="value(deadLetterPolicy.deadLetterTopic)" 2>/dev/null || echo "")
  if [ -n "$DLQ_CHECK" ]; then
    ok "run-analysis DLQ 設定済み: $DLQ_CHECK"
  else
    warn "run-analysis DLQ 未設定。monitoring_alert.yaml のコメントを参照して設定してください。"
  fi
fi

# =============================================================================
# 2. Firestore TTL ポリシー
# =============================================================================
step "2. Firestore TTL ポリシー — database: $DB_NAME"

# 注意: gcloud firestore fields ttls update は非同期。完了まで数分かかる場合があります。
# Firestore は expiresAt フィールドを参照して TTL 削除を自動実行します。
# (Cloud Function cleanExpiredRouteCache も日次でバックアップ削除を実行中)

for COLLECTION in api_counters daily_usage route_v2_cache; do
  if gcloud firestore fields ttls update expiresAt \
    --collection-group="$COLLECTION" \
    --project="$PROJECT_ID" \
    --database="$DB_NAME" \
    --async \
    --quiet 2>/dev/null; then
    ok "TTL スケジュール済み: $COLLECTION.expiresAt"
  else
    warn "TTL 設定失敗: $COLLECTION — Firebase Console から手動設定が必要な場合があります"
    warn "  Console: https://console.firebase.google.com/project/$PROJECT_ID/firestore/databases/$DB_NAME/ttl"
  fi
done

# =============================================================================
# 3. Cloud Monitoring アラートポリシー
# =============================================================================
step "3. Cloud Monitoring アラートポリシー"

# 3-A. 通知チャネル確認 (REPLACE_WITH_CHANNEL_ID が残っている場合は先に置換)
CHANNEL_PLACEHOLDER="REPLACE_WITH_CHANNEL_ID"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "  通知チャネル一覧を確認:"
echo "    gcloud alpha monitoring channels list --project=$PROJECT_ID"
echo ""

if grep -q "$CHANNEL_PLACEHOLDER" "$SCRIPT_DIR/alert_01_run_analysis.yaml" 2>/dev/null || \
   grep -q "$CHANNEL_PLACEHOLDER" "$SCRIPT_DIR/alert_02_proactive_coach.yaml" 2>/dev/null || \
   grep -q "$CHANNEL_PLACEHOLDER" "$SCRIPT_DIR/alert_03_gemini_429.yaml" 2>/dev/null; then
  warn "$CHANNEL_PLACEHOLDER が残っています。"
  warn "各 alert_*.yaml の REPLACE_WITH_CHANNEL_ID を実際のチャネル ID に置換後、"
  warn "以下を実行してください:"
  echo ""
  echo "  CHANNEL_ID=\$(gcloud alpha monitoring channels list \\"
  echo "    --project=$PROJECT_ID \\"
  echo "    --format='value(name)' | head -1)"
  echo ""
  echo "  for f in $SCRIPT_DIR/alert_0*.yaml; do"
  echo "    sed -i '' \"s|REPLACE_WITH_CHANNEL_ID|\$CHANNEL_ID|g\" \"\$f\""
  echo "  done"
  echo ""
  echo "  # 既存ポリシーがある場合は create の代わりに update を使用"
  echo "  for f in $SCRIPT_DIR/alert_0*.yaml; do"
  echo "    gcloud alpha monitoring policies create \\"
  echo "      --policy-from-file=\"\$f\" \\"
  echo "      --project=$PROJECT_ID"
  echo "  done"
else
  # チャネル ID が設定済みの場合は自動デプロイ
  for ALERT_FILE in "$SCRIPT_DIR"/alert_0*.yaml; do
    if [ -f "$ALERT_FILE" ]; then
      ALERT_NAME=$(basename "$ALERT_FILE")
      gcloud alpha monitoring policies create \
        --policy-from-file="$ALERT_FILE" \
        --project="$PROJECT_ID" \
        --quiet && ok "アラート作成: $ALERT_NAME" || warn "アラート作成失敗: $ALERT_NAME (既存の場合は update コマンドを使用)"
    fi
  done
fi

# =============================================================================
# 完了サマリー
# =============================================================================
echo ""
echo "======================================================"
ok "セットアップ完了"
echo "======================================================"
echo ""
echo "残作業 (手動):"
echo "  - Firebase Console: App Check debug token 更新"
echo "    https://console.firebase.google.com/project/$PROJECT_ID/appcheck"
echo "  - 通知チャネル ID の設定後、アラートポリシーを deploy"
echo "  - Legal L-1: Google Routes API on MapKit の書面確認"
echo ""
echo "定期実行が必要な場合 (Cloud Function 再デプロイ後):"
echo "  ./deploy/setup_infrastructure.sh  # DLQ 設定を再適用"
echo ""
