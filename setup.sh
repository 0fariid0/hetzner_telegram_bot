#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="hetzner-telegram-bot"
SERVICE_NAME="hetznerbot"
APP_DIR="/opt/${APP_NAME}"
BOT_USER="hetznerbot"
REPO_OWNER="${REPO_OWNER:-0fariid0}"
REPO_NAME="${REPO_NAME:-hetzner_telegram_bot}"
REPO_BRANCH="${REPO_BRANCH:-main}"
RAW_BASE="https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/${REPO_BRANCH}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info() { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
fail() { echo -e "${RED}[x]${NC} $*" >&2; exit 1; }

if [[ "${EUID}" -eq 0 ]]; then
  SUDO=""
else
  command -v sudo >/dev/null 2>&1 || fail "sudo is required when not running as root."
  SUDO="sudo"
fi

if ! command -v apt-get >/dev/null 2>&1; then
  fail "This installer currently supports Debian/Ubuntu systems with apt."
fi

info "Installing system packages..."
$SUDO apt-get update -y
$SUDO apt-get install -y python3 python3-venv ca-certificates curl

info "Preparing application directory..."
$SUDO install -d -m 0755 "$APP_DIR"

if ! id "$BOT_USER" >/dev/null 2>&1; then
  $SUDO useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$BOT_USER"
fi

info "Downloading bot source..."
TMP_BOT="$(mktemp)"
trap 'rm -f "$TMP_BOT"' EXIT
curl -fsSL --ipv4 "${RAW_BASE}/bot.py" -o "$TMP_BOT" || fail "Could not download bot.py from ${RAW_BASE}"
$SUDO install -m 0644 "$TMP_BOT" "$APP_DIR/bot.py"

info "Creating Python virtual environment..."
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  $SUDO python3 -m venv "$APP_DIR/.venv"
fi
$SUDO "$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
$SUDO "$APP_DIR/.venv/bin/pip" install 'python-telegram-bot>=20,<23' 'hcloud>=2,<3' 'python-dotenv>=1,<2'

echo
info "Bot configuration"
read -rsp "Telegram Bot Token: " TELEGRAM_TOKEN
echo
read -rsp "Hetzner API Token (Read/Write): " HETZNER_TOKEN
echo
read -rp "Allowed Telegram chat/user ID (example: -1001234567890): " ALLOWED_CHAT_ID

[[ -n "$TELEGRAM_TOKEN" ]] || fail "Telegram token cannot be empty."
[[ -n "$HETZNER_TOKEN" ]] || fail "Hetzner token cannot be empty."
if [[ -n "$ALLOWED_CHAT_ID" && ! "$ALLOWED_CHAT_ID" =~ ^-?[0-9]+$ ]]; then
  fail "Allowed chat/user ID must be numeric."
fi
ALLOWED_CHAT_ID="${ALLOWED_CHAT_ID:-0}"

ENV_TMP="$(mktemp)"
cat > "$ENV_TMP" <<EOF
TELEGRAM_BOT_TOKEN=${TELEGRAM_TOKEN}
HETZNER_API_TOKEN=${HETZNER_TOKEN}
ALLOWED_CHAT_ID=${ALLOWED_CHAT_ID}
EOF
$SUDO install -m 0600 "$ENV_TMP" "$APP_DIR/.env"
rm -f "$ENV_TMP"

$SUDO chown -R "$BOT_USER:$BOT_USER" "$APP_DIR"

info "Creating systemd service..."
SERVICE_TMP="$(mktemp)"
cat > "$SERVICE_TMP" <<EOF
[Unit]
Description=Hetzner Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${BOT_USER}
Group=${BOT_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/bot.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
$SUDO install -m 0644 "$SERVICE_TMP" "/etc/systemd/system/${SERVICE_NAME}.service"
rm -f "$SERVICE_TMP"

info "Starting service..."
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now "$SERVICE_NAME"

sleep 2
if $SUDO systemctl is-active --quiet "$SERVICE_NAME"; then
  echo
  info "Installation completed successfully."
  echo "Service: ${SERVICE_NAME}"
  echo "Status : systemctl status ${SERVICE_NAME}"
  echo "Logs   : journalctl -u ${SERVICE_NAME} -f"
  echo "Restart: systemctl restart ${SERVICE_NAME}"
else
  warn "The service did not become active. Showing recent logs:"
  $SUDO journalctl -u "$SERVICE_NAME" -n 30 --no-pager || true
  exit 1
fi
