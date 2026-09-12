#!/usr/bin/env bash
# Deploy the custom component and/or local add-on to a Home Assistant host.
#
# Usage:
#   ./scripts/deploy.sh [--component] [--addon] [--restart]
#
# At least one of --component or --addon is required.
# --restart calls the HA REST API to restart HA after deployment (needed for
# component changes to take effect; add-on changes need a manual Update in the UI).
#
# Config via env vars (or .env in repo root):
#   HA_SSH_HOST    — HA host for SSH (default: homeassistant.local)
#   HA_SSH_USER    — SSH user (default: root)
#   HA_SSH_KEY     — path to SSH private key (default: ~/.ssh/id_rsa)
#   HA_BASE_URL    — HA REST API base URL (default: http://homeassistant.local:8123)
#   HA_TOKEN       — long-lived access token (required for --restart)
#   HA_CONFIG_DIR  — HA config dir on host (default: /homeassistant)
#   HA_ADDONS_DIR  — local add-ons dir on host (default: /addons/local)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load .env if present
if [[ -f "$REPO_ROOT/.env" ]]; then
    set -o allexport
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.env"
    set +o allexport
fi

HA_SSH_HOST="${HA_SSH_HOST:-homeassistant.local}"
HA_SSH_USER="${HA_SSH_USER:-root}"
HA_SSH_KEY="${HA_SSH_KEY:-$HOME/.ssh/id_rsa}"
HA_BASE_URL="${HA_BASE_URL:-http://homeassistant.local:8123}"
HA_TOKEN="${HA_TOKEN:-}"
HA_CONFIG_DIR="${HA_CONFIG_DIR:-/homeassistant}"
HA_ADDONS_DIR="${HA_ADDONS_DIR:-/addons/local}"
ADDON_SLUG="local_ha_agent"      # add-on directory name (matches config.yaml slug)
COMPONENT_SLUG="agent_gosling"   # custom component domain (matches manifest.json)

DEPLOY_COMPONENT=false
DEPLOY_ADDON=false
DO_RESTART=false

for arg in "$@"; do
    case "$arg" in
        --component) DEPLOY_COMPONENT=true ;;
        --addon)     DEPLOY_ADDON=true ;;
        --restart)   DO_RESTART=true ;;
        --help|-h)
            sed -n '2,20p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "Unknown flag: $arg" >&2; exit 1 ;;
    esac
done

if ! $DEPLOY_COMPONENT && ! $DEPLOY_ADDON; then
    echo "Error: specify at least --component or --addon" >&2
    exit 1
fi

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o BatchMode=yes)
if [[ -f "$HA_SSH_KEY" ]]; then
    SSH_OPTS+=(-i "$HA_SSH_KEY")
fi

ssh_run() {
    ssh "${SSH_OPTS[@]}" "$HA_SSH_USER@$HA_SSH_HOST" "$@"
}

rsync_to_ha() {
    local src="$1" dest="$2"
    rsync -az --delete \
        "${SSH_OPTS[@]/#/-e ssh }" \
        -e "ssh ${SSH_OPTS[*]}" \
        "$src" \
        "$HA_SSH_USER@$HA_SSH_HOST:$dest"
}

if $DEPLOY_COMPONENT; then
    echo "==> Deploying custom component..."
    DEST="$HA_CONFIG_DIR/custom_components/$COMPONENT_SLUG"
    ssh_run "mkdir -p $DEST"
    rsync -az --delete \
        -e "ssh ${SSH_OPTS[*]}" \
        "$REPO_ROOT/custom_components/$COMPONENT_SLUG/" \
        "$HA_SSH_USER@$HA_SSH_HOST:$DEST/"
    echo "    Done: $DEST"
fi

if $DEPLOY_ADDON; then
    echo "==> Deploying add-on files..."
    DEST="$HA_ADDONS_DIR/$ADDON_SLUG"
    ssh_run "mkdir -p $DEST"
    rsync -az --delete \
        -e "ssh ${SSH_OPTS[*]}" \
        --exclude='venv/' \
        --exclude='.git/' \
        --exclude='.env' \
        --exclude='tests/' \
        --exclude='docs/' \
        --exclude='.claude/' \
        --exclude='.superpowers/' \
        --exclude='.conversations/' \
        --exclude='.evals/' \
        --exclude='analysis/' \
        --exclude='*.pyc' \
        --exclude='__pycache__/' \
        "$REPO_ROOT/" \
        "$HA_SSH_USER@$HA_SSH_HOST:$DEST/"
    echo "    Done: $DEST"
    echo
    echo "NOTE: To apply add-on changes, go to Settings → Add-ons → Local Add-ons →"
    echo "      ⋮ → Check for updates, then Update (Supervisor rebuilds the image)."
fi

if $DO_RESTART; then
    if [[ -z "$HA_TOKEN" ]]; then
        echo "Error: --restart requires HA_TOKEN to be set" >&2
        exit 1
    fi
    echo "==> Restarting Home Assistant via REST API..."
    HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST "$HA_BASE_URL/api/services/homeassistant/restart" \
        -H "Authorization: Bearer $HA_TOKEN" \
        -H "Content-Type: application/json")
    if [[ "$HTTP_STATUS" == "200" ]]; then
        echo "    Restart triggered. HA will be unavailable for ~30s."
    else
        echo "    Warning: API returned HTTP $HTTP_STATUS — check HA_BASE_URL and HA_TOKEN" >&2
    fi
fi

echo "==> Deploy complete."
