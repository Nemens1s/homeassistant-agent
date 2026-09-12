#!/usr/bin/with-contenv bashio

export HA_BASE_URL="http://supervisor/core"
# SUPERVISOR_TOKEN is injected automatically by the Supervisor when
# homeassistant_api access is granted in config.yaml

bashio::log.info "Starting Agent Gosling..."

export AUDIT_DB_PATH=/data/audit.db

cd /app || exit 1
exec uvicorn app.main:create_app --factory \
  --host 0.0.0.0 --port 8099 \
  --timeout-keep-alive 120
