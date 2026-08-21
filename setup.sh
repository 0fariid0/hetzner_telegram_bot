#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="hetzner-telegram-bot"
SERVICE_NAME="hetznerbot"
APP_DIR="/opt/${APP_NAME}"
ENV_FILE="${APP_DIR}/.env"
BOT_USER="hetznerbot"
REPO_OWNER="${REPO_OWNER:-0fariid0}"
REPO_NAME="${REPO_NAME:-hetzner_telegram_bot}"
REPO_BRANCH="${REPO_BRANCH:-main}"
RAW_BASE="https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/${REPO_BRANCH}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

INSTALLER_VERSION="15.1"
TTY_FD=0
if [[ -r /dev/tty && -w /dev/tty ]]; then
  exec 3<>/dev/tty
  TTY_FD=3
fi

info() { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
fail() { echo -e "${RED}[x]${NC} $*" >&2; exit 1; }
pause() { echo; read -r -u "$TTY_FD" -p "Press Enter to continue..." _; }

if [[ "${EUID}" -eq 0 ]]; then
  SUDO=""
else
  command -v sudo >/dev/null 2>&1 || fail "sudo is required when not running as root."
  SUDO="sudo"
fi

is_installed() {
  [[ -f "$ENV_FILE" && -f "$APP_DIR/bot.py" ]]
}

require_installed() {
  is_installed || fail "Bot is not installed yet. Choose Install first."
}

ensure_system_packages() {
  command -v apt-get >/dev/null 2>&1 || fail "This installer supports Debian/Ubuntu systems with apt."
  info "Installing required system packages..."
  $SUDO apt-get update -y
  $SUDO apt-get install -y python3 python3-venv ca-certificates curl
}

ensure_user_and_dir() {
  $SUDO install -d -m 0755 "$APP_DIR"
  if ! id "$BOT_USER" >/dev/null 2>&1; then
    $SUDO useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$BOT_USER"
  fi
}

download_file() {
  local remote="$1" dest="$2" mode="${3:-0644}"
  local tmp
  tmp="$(mktemp)"
  if ! curl -fsSL --ipv4 "${RAW_BASE}/${remote}" -o "$tmp"; then
    rm -f "$tmp"
    fail "Could not download ${remote} from ${RAW_BASE}"
  fi
  $SUDO install -m "$mode" "$tmp" "$dest"
  rm -f "$tmp"
}

install_python_deps() {
  if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    $SUDO python3 -m venv "$APP_DIR/.venv"
  fi
  $SUDO "$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
  $SUDO "$APP_DIR/.venv/bin/pip" install --upgrade \
    'python-telegram-bot[job-queue]>=20,<23' \
    'hcloud>=2.22,<3' \
    'python-dotenv>=1,<2'
}

read_env_value() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 0
  $SUDO awk -v k="$key" 'index($0,k"=")==1 {sub("^[^=]*=",""); print; exit}' "$ENV_FILE"
}

set_env_value() {
  local key="$1" value="$2"
  require_installed
  local tmp
  tmp="$(mktemp)"
  $SUDO python3 - "$ENV_FILE" "$tmp" "$key" "$value" <<'PY'
import sys
src, dst, key, value = sys.argv[1:]
try:
    lines = open(src, encoding='utf-8').read().splitlines()
except FileNotFoundError:
    lines = []
out=[]
found=False
for line in lines:
    if line.startswith(key + '='):
        out.append(f'{key}={value}')
        found=True
    else:
        out.append(line)
if not found:
    out.append(f'{key}={value}')
open(dst, 'w', encoding='utf-8').write('\n'.join(out) + '\n')
PY
  $SUDO install -m 0600 "$tmp" "$ENV_FILE"
  rm -f "$tmp"
  $SUDO chown "$BOT_USER:$BOT_USER" "$ENV_FILE"
}

encode_projects_tsv() {
  local tsv="$1"
  python3 - "$tsv" <<'PY'
import base64, json, sys
items=[]
with open(sys.argv[1], encoding='utf-8') as f:
    for raw in f:
        raw=raw.rstrip('\n')
        if not raw:
            continue
        name, token = raw.split('\t', 1)
        items.append({'name': name, 'token': token})
raw=json.dumps(items, ensure_ascii=False, separators=(',', ':')).encode()
print(base64.b64encode(raw).decode())
PY
}

decode_projects_tsv() {
  local b64="$1" out="$2"
  python3 - "$b64" "$out" <<'PY'
import base64, json, sys
b64, out = sys.argv[1:]
items=[]
if b64:
    items=json.loads(base64.b64decode(b64).decode('utf-8'))
    if isinstance(items, dict):
        items=[{'name':k,'token':v} for k,v in items.items()]
with open(out, 'w', encoding='utf-8') as f:
    for item in items:
        name=str(item.get('name','')).replace('\t',' ').replace('\n',' ').strip()
        token=str(item.get('token','')).strip()
        if name and token:
            f.write(f'{name}\t{token}\n')
PY
}

mask_token() {
  local token="$1"
  local len=${#token}
  if (( len <= 12 )); then
    printf '%s' '********'
  else
    printf '%s...%s' "${token:0:6}" "${token: -4}"
  fi
}

write_service() {
  local tmp
  tmp="$(mktemp)"
  cat > "$tmp" <<EOF
[Unit]
Description=Hetzner Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${BOT_USER}
Group=${BOT_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/bot.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
  $SUDO install -m 0644 "$tmp" "/etc/systemd/system/${SERVICE_NAME}.service"
  rm -f "$tmp"
  $SUDO systemctl daemon-reload
}

restart_service() {
  $SUDO systemctl restart "$SERVICE_NAME"
  sleep 1
  if $SUDO systemctl is-active --quiet "$SERVICE_NAME"; then
    info "Service restarted successfully."
  else
    warn "Service is not active. Recent logs:"
    $SUDO journalctl -u "$SERVICE_NAME" -n 30 --no-pager || true
  fi
}

configure_projects_interactive() {
  local count name token tsv b64
  read -r -u "$TTY_FD" -p "Number of Hetzner projects [1]: " count
  count="${count:-1}"
  [[ "$count" =~ ^[1-9][0-9]*$ ]] || fail "Project count must be a positive integer."

  tsv="$(mktemp)"
  chmod 600 "$tsv"
  for ((i=1; i<=count; i++)); do
    echo >&2
    read -r -u "$TTY_FD" -p "Project ${i} display name [Project ${i}]: " name
    name="${name:-Project ${i}}"
    name="${name//$'\t'/ }"
    name="${name//$'\n'/ }"
    read -r -s -u "$TTY_FD" -p "Hetzner API Token for ${name} (Read/Write): " token
    echo >&2
    [[ -n "$token" ]] || { rm -f "$tsv"; fail "Hetzner token cannot be empty."; }
    printf '%s\t%s\n' "$name" "$token" >> "$tsv"
  done
  b64="$(encode_projects_tsv "$tsv")"
  rm -f "$tsv"
  printf '%s' "$b64"
}

install_flow() {
  echo
  if is_installed; then
    warn "An existing installation was detected."
    read -r -u "$TTY_FD" -p "Reinstall and replace configuration? [y/N]: " answer
    [[ "${answer,,}" == "y" || "${answer,,}" == "yes" ]] || return
  fi

  ensure_system_packages
  ensure_user_and_dir

  info "Downloading latest bot files..."
  download_file "bot.py" "$APP_DIR/bot.py" 0644
  download_file "setup.sh" "$APP_DIR/setup.sh" 0755
  download_file "README.md" "$APP_DIR/README.md" 0644 || true
  download_file "VERSION" "$APP_DIR/VERSION" 0644
  install_python_deps

  echo
  echo -e "${BOLD}Bot configuration${NC}"
  local telegram_token allowed_user projects_b64 env_tmp
  read -r -s -u "$TTY_FD" -p "Telegram Bot Token: " telegram_token
  echo
  read -r -u "$TTY_FD" -p "Allowed Telegram numeric User ID: " allowed_user
  [[ -n "$telegram_token" ]] || fail "Telegram token cannot be empty."
  [[ "$allowed_user" =~ ^[0-9]+$ ]] || fail "Allowed Telegram User ID must be numeric."

  projects_b64="$(configure_projects_interactive)"
  [[ -n "$projects_b64" ]] || fail "At least one Hetzner project is required."

  env_tmp="$(mktemp)"
  cat > "$env_tmp" <<EOF
TELEGRAM_BOT_TOKEN=${telegram_token}
ALLOWED_USER_ID=${allowed_user}
HETZNER_PROJECTS_B64=${projects_b64}
BOT_TIMEZONE=Asia/Tehran
TRAFFIC_CHECK_TIME=23:30
CHEAP_CHECK_HOURS=1
HETZNER_PRICE_KIND=gross
PRICE_CACHE_TTL_SECONDS=600
STATE_FILE=${APP_DIR}/.traffic_alert_state.json
AVAILABILITY_STATE_FILE=${APP_DIR}/.cost_optimized_state.json
EOF
  $SUDO install -m 0600 "$env_tmp" "$ENV_FILE"
  rm -f "$env_tmp"
  $SUDO chown -R "$BOT_USER:$BOT_USER" "$APP_DIR"

  write_service
  $SUDO systemctl enable "$SERVICE_NAME" >/dev/null
  restart_service

  echo
  info "Installation completed."
  echo "Service: ${SERVICE_NAME}"
  echo "Config : ${ENV_FILE}"
  echo "Logs   : journalctl -u ${SERVICE_NAME} -f"
}

update_flow() {
  require_installed
  ensure_system_packages
  ensure_user_and_dir
  info "Downloading latest version..."
  download_file "bot.py" "$APP_DIR/bot.py" 0644
  download_file "setup.sh" "$APP_DIR/setup.sh" 0755
  download_file "README.md" "$APP_DIR/README.md" 0644 || true
  download_file "VERSION" "$APP_DIR/VERSION" 0644
  install_python_deps
  if [[ -z "$(read_env_value HETZNER_PRICE_KIND)" ]]; then
    set_env_value "HETZNER_PRICE_KIND" "gross"
  fi
  if [[ -z "$(read_env_value PRICE_CACHE_TTL_SECONDS)" ]]; then
    set_env_value "PRICE_CACHE_TTL_SECONDS" "600"
  fi
  write_service
  $SUDO chown -R "$BOT_USER:$BOT_USER" "$APP_DIR"
  restart_service
  info "Update completed. Existing tokens and settings were preserved."
}

load_projects_file() {
  require_installed
  PROJECTS_WORK="$(mktemp)"
  chmod 600 "$PROJECTS_WORK"
  local b64
  b64="$(read_env_value HETZNER_PROJECTS_B64)"
  decode_projects_tsv "$b64" "$PROJECTS_WORK"
}

save_projects_file() {
  local b64
  b64="$(encode_projects_tsv "$PROJECTS_WORK")"
  set_env_value "HETZNER_PROJECTS_B64" "$b64"
  rm -f "$PROJECTS_WORK"
  unset PROJECTS_WORK
  restart_service
}

list_projects_tokens() {
  load_projects_file
  echo
  echo -e "${BOLD}Configured Hetzner projects:${NC}"
  local n=0 name token
  while IFS=$'\t' read -r name token; do
    [[ -n "$name" ]] || continue
    ((n+=1))
    printf '  %d) %s  [%s]\n' "$n" "$name" "$(mask_token "$token")"
  done < "$PROJECTS_WORK"
  if (( n == 0 )); then
    echo "  No Hetzner projects configured."
  fi
  rm -f "$PROJECTS_WORK"
  unset PROJECTS_WORK
}

select_project() {
  local file="$1"
  mapfile -t PROJECT_LINES < "$file"
  local count=${#PROJECT_LINES[@]}
  (( count > 0 )) || return 1
  echo
  for i in "${!PROJECT_LINES[@]}"; do
    IFS=$'\t' read -r name token <<< "${PROJECT_LINES[$i]}"
    printf '  %d) %s\n' "$((i+1))" "$name"
  done
  local choice
  read -r -u "$TTY_FD" -p "Select project [1-${count}]: " choice
  [[ "$choice" =~ ^[0-9]+$ ]] || return 1
  (( choice >= 1 && choice <= count )) || return 1
  SELECTED_INDEX=$((choice-1))
  return 0
}

add_project_token() {
  load_projects_file
  local name token
  echo
  read -r -u "$TTY_FD" -p "New project display name: " name
  name="${name//$'\t'/ }"
  name="${name//$'\n'/ }"
  [[ -n "$name" ]] || { rm -f "$PROJECTS_WORK"; fail "Project name cannot be empty."; }
  if awk -F '\t' -v n="$name" '$1==n {found=1} END{exit !found}' "$PROJECTS_WORK"; then
    rm -f "$PROJECTS_WORK"
    fail "A project with this name already exists. Use Change Token."
  fi
  read -r -s -u "$TTY_FD" -p "Hetzner API Token (Read/Write): " token
  echo
  [[ -n "$token" ]] || { rm -f "$PROJECTS_WORK"; fail "Token cannot be empty."; }
  printf '%s\t%s\n' "$name" "$token" >> "$PROJECTS_WORK"
  save_projects_file
  info "Project token added."
}

change_project_token() {
  load_projects_file
  if ! select_project "$PROJECTS_WORK"; then
    rm -f "$PROJECTS_WORK"
    warn "Invalid selection."
    return
  fi
  local selected="${PROJECT_LINES[$SELECTED_INDEX]}" name old_token new_token new_name
  IFS=$'\t' read -r name old_token <<< "$selected"
  echo
  read -r -u "$TTY_FD" -p "Project display name [${name}]: " new_name
  new_name="${new_name:-$name}"
  new_name="${new_name//$'\t'/ }"
  new_name="${new_name//$'\n'/ }"
  read -r -s -u "$TTY_FD" -p "New Hetzner API Token for ${new_name}: " new_token
  echo
  [[ -n "$new_token" ]] || { rm -f "$PROJECTS_WORK"; fail "Token cannot be empty."; }

  local tmp
  tmp="$(mktemp)"
  for i in "${!PROJECT_LINES[@]}"; do
    if (( i == SELECTED_INDEX )); then
      printf '%s\t%s\n' "$new_name" "$new_token" >> "$tmp"
    else
      printf '%s\n' "${PROJECT_LINES[$i]}" >> "$tmp"
    fi
  done
  mv "$tmp" "$PROJECTS_WORK"
  save_projects_file
  info "Project token updated."
}

remove_project_token() {
  load_projects_file
  mapfile -t CURRENT_LINES < "$PROJECTS_WORK"
  if (( ${#CURRENT_LINES[@]} <= 1 )); then
    rm -f "$PROJECTS_WORK"
    warn "The last Hetzner token cannot be removed. Add another project first."
    return
  fi
  if ! select_project "$PROJECTS_WORK"; then
    rm -f "$PROJECTS_WORK"
    warn "Invalid selection."
    return
  fi
  local selected="${PROJECT_LINES[$SELECTED_INDEX]}" name token answer
  IFS=$'\t' read -r name token <<< "$selected"
  read -r -u "$TTY_FD" -p "Remove project '${name}'? [y/N]: " answer
  if [[ "${answer,,}" != "y" && "${answer,,}" != "yes" ]]; then
    rm -f "$PROJECTS_WORK"
    return
  fi
  local tmp
  tmp="$(mktemp)"
  for i in "${!PROJECT_LINES[@]}"; do
    (( i == SELECTED_INDEX )) || printf '%s\n' "${PROJECT_LINES[$i]}" >> "$tmp"
  done
  mv "$tmp" "$PROJECTS_WORK"
  save_projects_file
  info "Project token removed."
}

change_telegram_token() {
  require_installed
  local token
  echo
  read -r -s -u "$TTY_FD" -p "New Telegram Bot Token: " token
  echo
  [[ -n "$token" ]] || fail "Telegram token cannot be empty."
  set_env_value "TELEGRAM_BOT_TOKEN" "$token"
  restart_service
  info "Telegram Bot Token updated."
}

change_allowed_user() {
  require_installed
  local user_id
  echo
  read -r -u "$TTY_FD" -p "New allowed Telegram numeric User ID: " user_id
  [[ "$user_id" =~ ^[0-9]+$ ]] || fail "User ID must be numeric."
  set_env_value "ALLOWED_USER_ID" "$user_id"
  restart_service
  info "Allowed User ID updated."
}

token_menu() {
  require_installed
  while true; do
    clear 2>/dev/null || true
    echo -e "${CYAN}${BOLD}Hetzner Telegram Bot - Token Management${NC}"
    echo
    echo "  1) List Hetzner project tokens"
    echo "  2) Add Hetzner project token"
    echo "  3) Change Hetzner project token"
    echo "  4) Remove Hetzner project token"
    echo "  5) Change Telegram Bot Token"
    echo "  6) Change Allowed Telegram User ID"
    echo "  0) Back"
    echo
    read -r -u "$TTY_FD" -p "Select an option: " choice
    case "$choice" in
      1) list_projects_tokens; pause ;;
      2) add_project_token; pause ;;
      3) change_project_token; pause ;;
      4) remove_project_token; pause ;;
      5) change_telegram_token; pause ;;
      6) change_allowed_user; pause ;;
      0) return ;;
      *) warn "Invalid option."; sleep 1 ;;
    esac
  done
}

service_menu() {
  require_installed
  while true; do
    clear 2>/dev/null || true
    echo -e "${CYAN}${BOLD}Service Management${NC}"
    echo
    echo "  1) Status"
    echo "  2) Restart"
    echo "  3) Start"
    echo "  4) Stop"
    echo "  5) Recent logs"
    echo "  0) Back"
    echo
    read -r -u "$TTY_FD" -p "Select an option: " choice
    case "$choice" in
      1) $SUDO systemctl status "$SERVICE_NAME" --no-pager || true; pause ;;
      2) restart_service; pause ;;
      3) $SUDO systemctl start "$SERVICE_NAME"; info "Service started."; pause ;;
      4) $SUDO systemctl stop "$SERVICE_NAME"; info "Service stopped."; pause ;;
      5) $SUDO journalctl -u "$SERVICE_NAME" -n 60 --no-pager || true; pause ;;
      0) return ;;
      *) warn "Invalid option."; sleep 1 ;;
    esac
  done
}

main_menu() {
  while true; do
    clear 2>/dev/null || true
    echo -e "${CYAN}${BOLD}========================================${NC}"
    echo -e "${CYAN}${BOLD}       Hetzner Telegram Bot Manager     ${NC}"
    echo -e "${CYAN}              Installer v${INSTALLER_VERSION}${NC}"
    echo -e "${CYAN}${BOLD}========================================${NC}"
    echo
    if is_installed; then
      echo -e "Status: ${GREEN}Installed${NC}"
    else
      echo -e "Status: ${YELLOW}Not installed${NC}"
    fi
    echo
    echo "  1) Install"
    echo "  2) Update"
    echo "  3) Token Management"
    echo "  4) Service Management"
    echo "  0) Exit"
    echo
    read -r -u "$TTY_FD" -p "Select an option: " choice
    case "$choice" in
      1) install_flow; pause ;;
      2) update_flow; pause ;;
      3) token_menu ;;
      4) service_menu ;;
      0) exit 0 ;;
      *) warn "Invalid option."; sleep 1 ;;
    esac
  done
}

# Always show the interactive manager. Never auto-install when this script is
# fetched with curl/process substitution. All prompts read from /dev/tty when
# available, so both `bash <(curl ...)` and `curl ... | bash` keep the menu.
if [[ "$TTY_FD" -eq 0 && ! -t 0 ]]; then
  fail "Interactive terminal required. Run this command from a terminal: bash <(curl -fsSL --ipv4 ${RAW_BASE}/setup.sh)"
fi

main_menu
