#!/usr/bin/with-contenv bashio

export OLLAMA_URL=$(bashio::config 'ollama_url')
export OLLAMA_MODEL=$(bashio::config 'ollama_model')
export SYSTEM_PROMPT=$(bashio::config 'system_prompt')
export HA_BASE_URL="http://supervisor/core"
# SUPERVISOR_TOKEN is injected automatically by the Supervisor when
# hassio_api / homeassistant_api access is granted in config.yaml

bashio::log.info "Starting Local HA Agent..."
bashio::log.info "Ollama URL: ${OLLAMA_URL}"
bashio::log.info "Model: ${OLLAMA_MODEL}"

cd /app
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8099
