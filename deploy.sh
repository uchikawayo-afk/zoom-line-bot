#!/usr/bin/env bash
# Cloud Run へデプロイする。.env.render に値が入っていれば Secret Manager に取り込んでから流す。
set -euo pipefail

PROJECT=shopexport-393814
REGION=asia-northeast1
SERVICE=zoom-line-bot
SA="27531714336-compute@developer.gserviceaccount.com"
HERE="$(cd "$(dirname "$0")" && pwd)"
CLIENT_ID=27531714336-fleivpi82rmk44326kiqqdrpd9eirbea.apps.googleusercontent.com

put_secret() {  # put_secret <secret名> <値>
  local name="$1" value="$2"
  if gcloud secrets describe "$name" --project="$PROJECT" >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- --project="$PROJECT" >/dev/null
    echo "  更新: $name"
  else
    printf '%s' "$value" | gcloud secrets create "$name" --data-file=- \
      --replication-policy=automatic --project="$PROJECT" >/dev/null
    echo "  作成: $name"
  fi
  gcloud secrets add-iam-policy-binding "$name" --member="serviceAccount:$SA" \
    --role="roles/secretmanager.secretAccessor" --project="$PROJECT" >/dev/null 2>&1 || true
}

# .env.render に値が入っていれば Secret Manager に取り込む
if [ -f "$HERE/.env.render" ]; then
  echo "== .env.render を取り込みます =="
  while IFS='=' read -r key value; do
    key="${key%%[[:space:]]}"
    [ -z "$key" ] && continue
    case "$key" in \#*) continue ;; esac
    [ -z "$value" ] && { echo "  空のためスキップ: $key"; continue; }
    case "$key" in
      LINE_CHANNEL_SECRET)     put_secret ZOOM_BOT_LINE_SECRET "$value" ;;
      ZOOM_ACCOUNT_ID)         put_secret ZOOM_ACCOUNT_ID "$value" ;;
      ZOOM_CLIENT_ID)          put_secret ZOOM_CLIENT_ID "$value" ;;
      ZOOM_CLIENT_SECRET)      put_secret ZOOM_CLIENT_SECRET "$value" ;;
      USER_ZOOM_CREDENTIALS)   put_secret USER_ZOOM_CREDENTIALS "$value" ;;
      DAILY_REPORT_SECRET)     put_secret DAILY_REPORT_SECRET "$value" ;;
      *) echo "  未知のキーのためスキップ: $key" ;;
    esac
  done < "$HERE/.env.render"
fi

# 存在するシークレットだけを --set-secrets に載せる（未投入でもデプロイは通す）
SECRETS="LINE_CHANNEL_ACCESS_TOKEN=ZOOM_BOT_LINE_TOKEN:latest"
SECRETS="$SECRETS,GOOGLE_OAUTH_CLIENT_SECRET=GOOGLE_OAUTH_CLIENT_SECRET:latest"
SECRETS="$SECRETS,GOOGLE_CALENDAR_REFRESH_TOKEN=GOOGLE_CALENDAR_REFRESH_TOKEN:latest"
add_if_exists() {  # add_if_exists <env名> <secret名>
  if gcloud secrets describe "$2" --project="$PROJECT" >/dev/null 2>&1; then
    SECRETS="$SECRETS,$1=$2:latest"
  else
    echo "!! 未設定のため今回は載せません: $2"
  fi
}
add_if_exists LINE_CHANNEL_SECRET   ZOOM_BOT_LINE_SECRET
add_if_exists ZOOM_ACCOUNT_ID       ZOOM_ACCOUNT_ID
add_if_exists ZOOM_CLIENT_ID        ZOOM_CLIENT_ID
add_if_exists ZOOM_CLIENT_SECRET    ZOOM_CLIENT_SECRET
add_if_exists USER_ZOOM_CREDENTIALS USER_ZOOM_CREDENTIALS
add_if_exists DAILY_REPORT_SECRET   DAILY_REPORT_SECRET

echo "== デプロイ =="
gcloud run deploy "$SERVICE" \
  --source "$HERE" \
  --region "$REGION" --project "$PROJECT" \
  --allow-unauthenticated \
  --min-instances=0 --max-instances=3 \
  --cpu=1 --memory=512Mi --cpu-boost \
  --set-env-vars="GOOGLE_OAUTH_CLIENT_ID=$CLIENT_ID" \
  --set-secrets="$SECRETS"

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" --format="value(status.url)")
echo
echo "Webhook URL: $URL/webhook"
echo "ヘルスチェック: $(curl -s -m 60 "$URL/health" || echo "応答なし")"
