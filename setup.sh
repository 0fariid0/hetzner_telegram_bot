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

command -v apt-get >/dev/null 2>&1 || fail "This installer supports Debian/Ubuntu systems with apt."

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
trap 'rm -f "$TMP_BOT" "${PROJECTS_TMP:-}" "${ENV_TMP:-}" "${SERVICE_TMP:-}"' EXIT
curl -fsSL --ipv4 "${RAW_BASE}/bot.py" -o "$TMP_BOT" || fail "Could not download bot.py from ${RAW_BASE}"
$SUDO install -m 0644 "$TMP_BOT" "$APP_DIR/bot.py"

info "Creating Python virtual environment..."
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  $SUDO python3 -m venv "$APP_DIR/.venv"
fi
$SUDO "$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
$SUDO "$APP_DIR/.venv/bin/pip" install 'python-telegram-bot[job-queue]>=20,<23' 'hcloud>=2.23,<3' 'python-dotenv>=1,<2'

echo
info "Bot configuration"
read -rsp "Telegram Bot Token: " TELEGRAM_TOKEN
echo
read -rp "Allowed Telegram numeric User ID: " ALLOWED_USER_ID

[[ -n "$TELEGRAM_TOKEN" ]] || fail "Telegram token cannot be empty."
[[ "$ALLOWED_USER_ID" =~ ^[0-9]+$ ]] || fail "Allowed Telegram User ID must be numeric."

read -rp "Number of Hetzner projects [1]: " PROJECT_COUNT
PROJECT_COUNT="${PROJECT_COUNT:-1}"
[[ "$PROJECT_COUNT" =~ ^[1-9][0-9]*$ ]] || fail "Project count must be a positive integer."

PROJECTS_TMP="$(mktemp)"
chmod 600 "$PROJECTS_TMP"
for ((i=1; i<=PROJECT_COUNT; i++)); do
  echo
  read -rp "Project ${i} display name [Project ${i}]: " PROJECT_NAME
  PROJECT_NAME="${PROJECT_NAME:-Project ${i}}"
  PROJECT_NAME="${PROJECT_NAME//$'\t'/ }"
  PROJECT_NAME="${PROJECT_NAME//$'\n'/ }"
  read -rsp "Hetzner API Token for ${PROJECT_NAME} (Read/Write): " PROJECT_TOKEN
  echo
  [[ -n "$PROJECT_TOKEN" ]] || fail "Hetzner token cannot be empty."
  printf '%s\t%s\n' "$PROJECT_NAME" "$PROJECT_TOKEN" >> "$PROJECTS_TMP"
done

PROJECTS_B64="$($APP_DIR/.venv/bin/python - "$PROJECTS_TMP" <<'PY'
import base64, json, sys
items=[]
with open(sys.argv[1], encoding='utf-8') as f:
    for line in f:
        name, token = line.rstrip('\n').split('\t', 1)
        items.append({'name': name, 'token': token})
raw=json.dumps(items, ensure_ascii=False, separators=(',', ':')).encode()
print(base64.b64encode(raw).decode())
PY
)"

ENV_TMP="$(mktemp)"
cat > "$ENV_TMP" <<EOF
TELEGRAM_BOT_TOKEN=${TELEGRAM_TOKEN}
ALLOWED_USER_ID=${ALLOWED_USER_ID}
HETZNER_PROJECTS_B64=${PROJECTS_B64}
BOT_TIMEZONE=Asia/Tehran
TRAFFIC_CHECK_TIME=23:30
STATE_FILE=${APP_DIR}/.traffic_alert_state.json
EOF
$SUDO install -m 0600 "$ENV_TMP" "$APP_DIR/.env"

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

info "Starting service..."
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now "$SERVICE_NAME"

sleep 2
if $SUDO systemctl is-active --quiet "$SERVICE_NAME"; then
  echo
  info "Installation completed successfully."
  echo "Access is restricted to Telegram User ID: ${ALLOWED_USER_ID}"
  echo "Projects: ${PROJECT_COUNT}"
  echo "Traffic check: 23:30 Asia/Tehran"
  echo "Service: ${SERVICE_NAME}"
  echo "Status : systemctl status ${SERVICE_NAME}"
  echo "Logs   : journalctl -u ${SERVICE_NAME} -f"
  echo "Restart: systemctl restart ${SERVICE_NAME}"
else
  warn "The service did not become active. Showing recent logs:"
  $SUDO journalctl -u "$SERVICE_NAME" -n 40 --no-pager || true
  exit 1
fi
