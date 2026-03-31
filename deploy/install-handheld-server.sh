#!/usr/bin/env bash

set -euo pipefail

REPO_URL="${NANOBOT_REPO_URL:-https://github.com/HKUDS/nanobot.git}"
INSTALL_DIR="${NANOBOT_INSTALL_DIR:-$HOME/personal-cloud/nanobot}"
BRANCH="${NANOBOT_BRANCH:-main}"
HOST="${NANOBOT_HANDHELD_HOST:-127.0.0.1}"
PORT="${NANOBOT_HANDHELD_PORT:-18789}"
SERVICE_NAME="${NANOBOT_SERVICE_NAME:-nanobot-handheld}"
CONFIG_PATH="${NANOBOT_CONFIG_PATH:-$HOME/.nanobot/config.json}"
WORKSPACE_DIR="${NANOBOT_WORKSPACE_DIR:-$HOME/.nanobot/workspace}"
ENV_FILE="${NANOBOT_ENV_FILE:-$HOME/.config/nanobot/handheld.env}"
SYSTEMD_MODE="${NANOBOT_SYSTEMD_MODE:-user}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_TEMPLATE="$SCRIPT_DIR/systemd/nanobot-handheld.service"

log() {
  printf '[install] %s\n' "$*"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'Missing required command: %s\n' "$1" >&2
    exit 1
  }
}

ensure_base_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    log "Installing base packages with apt"
    sudo apt-get update
    sudo apt-get install -y curl git ca-certificates
  elif command -v dnf >/dev/null 2>&1; then
    log "Installing base packages with dnf"
    sudo dnf install -y curl git ca-certificates
  elif command -v yum >/dev/null 2>&1; then
    log "Installing base packages with yum"
    sudo yum install -y curl git ca-certificates
  elif command -v apk >/dev/null 2>&1; then
    log "Installing base packages with apk"
    sudo apk add --no-cache curl git ca-certificates
  else
    log "No supported package manager found; assuming curl/git are already installed"
  fi
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    log "uv already installed: $(command -v uv)"
    return
  fi

  log "Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh

  export PATH="$HOME/.local/bin:$PATH"
  require_cmd uv
}

sync_repo() {
  mkdir -p "$(dirname "$INSTALL_DIR")"

  if [[ -d "$INSTALL_DIR/.git" ]]; then
    log "Updating existing repo in $INSTALL_DIR"
    git -C "$INSTALL_DIR" fetch --depth=1 origin "$BRANCH"
    git -C "$INSTALL_DIR" checkout "$BRANCH"
    git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH"
  else
    log "Cloning repo into $INSTALL_DIR"
    rm -rf "$INSTALL_DIR"
    git clone --depth=1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  fi
}

install_python_deps() {
  log "Syncing runtime dependencies with uv"
  cd "$INSTALL_DIR"
  export PATH="$HOME/.local/bin:$PATH"
  uv sync --extra api
}

prepare_dirs() {
  log "Preparing runtime directories"
  mkdir -p "$(dirname "$CONFIG_PATH")" "$WORKSPACE_DIR" "$(dirname "$ENV_FILE")"
}

ensure_config() {
  if [[ -f "$CONFIG_PATH" ]]; then
    log "Config already exists: $CONFIG_PATH"
    return
  fi

  log "Creating default nanobot config"
  cd "$INSTALL_DIR"
  export PATH="$HOME/.local/bin:$PATH"
  uv run nanobot onboard --config "$CONFIG_PATH" --workspace "$WORKSPACE_DIR"
  log "Default config created; remember to edit provider/model settings before production use"
}

write_env_file() {
  if [[ -z "${NANOBOT_HANDHELD_TOKEN:-}" ]]; then
    printf 'NANOBOT_HANDHELD_TOKEN is required\n' >&2
    exit 1
  fi

  log "Writing environment file: $ENV_FILE"
  cat >"$ENV_FILE" <<EOF
NANOBOT_HANDHELD_TOKEN=${NANOBOT_HANDHELD_TOKEN}
NANOBOT_CONFIG_PATH=${CONFIG_PATH}
EOF
  chmod 600 "$ENV_FILE"
}

install_systemd_service() {
  if [[ ! -f "$SERVICE_TEMPLATE" ]]; then
    printf 'Service template not found: %s\n' "$SERVICE_TEMPLATE" >&2
    exit 1
  fi

  local service_dir service_path systemctl_cmd
  if [[ "$SYSTEMD_MODE" == "system" ]]; then
    service_dir="/etc/systemd/system"
    service_path="$service_dir/$SERVICE_NAME.service"
    systemctl_cmd="sudo systemctl"
  else
    service_dir="$HOME/.config/systemd/user"
    service_path="$service_dir/$SERVICE_NAME.service"
    systemctl_cmd="systemctl --user"
  fi

  log "Installing systemd service: $service_path"
  mkdir -p "$service_dir"

  sed \
    -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
    -e "s|__ENV_FILE__|$ENV_FILE|g" \
    -e "s|__HOST__|$HOST|g" \
    -e "s|__PORT__|$PORT|g" \
    -e "s|__CONFIG_PATH__|$CONFIG_PATH|g" \
    -e "s|__WORKSPACE_DIR__|$WORKSPACE_DIR|g" \
    "$SERVICE_TEMPLATE" | {
      if [[ "$SYSTEMD_MODE" == "system" ]]; then
        sudo tee "$service_path" >/dev/null
      else
        cat >"$service_path"
      fi
    }

  log "Reloading and enabling service"
  eval "$systemctl_cmd daemon-reload"
  eval "$systemctl_cmd enable --now $SERVICE_NAME"

  if [[ "$SYSTEMD_MODE" == "user" ]]; then
    log "If this VM should survive logout, consider: sudo loginctl enable-linger $USER"
  fi
}

print_summary() {
  cat <<EOF

Install complete.

- Repo: $INSTALL_DIR
- Config: $CONFIG_PATH
- Workspace: $WORKSPACE_DIR
- Host: $HOST
- Port: $PORT
- Env file: $ENV_FILE

Manual start:
  cd $INSTALL_DIR
  export PATH="\$HOME/.local/bin:\$PATH"
  source $ENV_FILE
  uv run nanobot handheld-serve --host $HOST --port $PORT --config $CONFIG_PATH --workspace $WORKSPACE_DIR --token "\$NANOBOT_HANDHELD_TOKEN"

EOF
}

main() {
  ensure_base_packages
  ensure_uv
  require_cmd git
  sync_repo
  install_python_deps
  prepare_dirs
  ensure_config
  write_env_file
  install_systemd_service
  print_summary
}

main "$@"
