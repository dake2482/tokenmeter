#!/bin/sh
set -eu

DEFAULT_REPO="dake2482/tokenmeter"
TOKENMETER_REPO="${TOKENMETER_REPO:-$DEFAULT_REPO}"
TOKENMETER_REF="${TOKENMETER_REF:-main}"
TOKENMETER_DOWNLOAD_URL="${TOKENMETER_DOWNLOAD_URL:-https://codeload.github.com/$TOKENMETER_REPO/tar.gz/$TOKENMETER_REF}"
TOKENMETER_AGENTS="${TOKENMETER_AGENTS:-hermes,openclaw,codex,zcode,workbuddy,claude}"
TOKENMETER_INTERVAL="${TOKENMETER_INTERVAL:-900}"
TOKENMETER_SINCE="${TOKENMETER_SINCE:-1d}"
TOKENMETER_BOOTSTRAP_SINCE="${TOKENMETER_BOOTSTRAP_SINCE:-30d}"
TOKENMETER_HOST="${TOKENMETER_HOST:-$(hostname)}"
TOKENMETER_MODE="${TOKENMETER_MODE:-}"

usage() {
  cat <<'EOF'
TokenMeter installer

Install the central dashboard:
  curl -fsSL https://raw.githubusercontent.com/dake2482/tokenmeter/main/scripts/install.sh | sudo sh -s -- server

Install an uploader on another machine:
  curl -fsSL https://raw.githubusercontent.com/dake2482/tokenmeter/main/scripts/install.sh \
    | TOKENMETER_SERVER="https://your-tokenmeter.example.com" TOKENMETER_TOKEN="your-token" sh -s -- agent

Environment:
  TOKENMETER_REPO              GitHub repo, default dake2482/tokenmeter
  TOKENMETER_REF               branch/tag/sha, default main
  TOKENMETER_DIR               install dir, default /opt/tokenmeter for root Linux
  TOKENMETER_BIND              server bind, default 0.0.0.0:18888
  TOKENMETER_TOKEN             API token; server mode generates one if omitted
  TOKENMETER_DISABLE_TOKEN=1   server mode without API token
  TOKENMETER_SERVER            dashboard URL for agent mode
  TOKENMETER_INTERVAL          timer interval seconds, default 900
EOF
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing command: $1" >&2
    exit 1
  }
}

normalize_mode() {
  mode="$1"
  case "$mode" in
    ""|server) echo "server" ;;
    agent|upload|uploader) echo "agent" ;;
    *) echo ""; return 1 ;;
  esac
}

MODE_ARG="${1:-}"
if [ "$MODE_ARG" = "-h" ] || [ "$MODE_ARG" = "--help" ]; then
  usage
  exit 0
fi
if [ "$MODE_ARG" = "--mode" ]; then
  shift || true
  MODE_ARG="${1:-}"
fi

if [ -z "$TOKENMETER_MODE" ]; then
  case "$MODE_ARG" in
    http://*|https://*)
      TOKENMETER_MODE="agent"
      TOKENMETER_SERVER="${TOKENMETER_SERVER:-$MODE_ARG}"
      ;;
    *)
      TOKENMETER_MODE="$(normalize_mode "$MODE_ARG")" || {
        usage >&2
        exit 2
      }
      ;;
  esac
else
  TOKENMETER_MODE="$(normalize_mode "$TOKENMETER_MODE")" || {
    usage >&2
    exit 2
  }
fi

need_cmd curl
need_cmd tar

PYTHON_BIN="${TOKENMETER_PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  else
    echo "Missing python3" >&2
    exit 1
  fi
fi

OS="$(uname -s)"
IS_ROOT=0
if [ "$(id -u)" = "0" ]; then
  IS_ROOT=1
fi

if [ "$IS_ROOT" = "1" ] && [ "$OS" = "Linux" ]; then
  INSTALL_DIR="${TOKENMETER_DIR:-/opt/tokenmeter}"
  DATA_DIR="${TOKENMETER_DATA_DIR:-/var/lib/tokenmeter}"
else
  INSTALL_DIR="${TOKENMETER_DIR:-$HOME/.local/share/tokenmeter}"
  DATA_DIR="${TOKENMETER_DATA_DIR:-$INSTALL_DIR/data}"
fi

case "$INSTALL_DIR" in
  ""|"/")
    echo "Refusing unsafe TOKENMETER_DIR=$INSTALL_DIR" >&2
    exit 1
    ;;
esac

generate_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
    return
  fi
  "$PYTHON_BIN" - <<'PY'
import secrets
print(secrets.token_hex(24))
PY
}

download_source() {
  TMP_DIR="$(mktemp -d)"
  cleanup() { rm -rf "$TMP_DIR"; }
  trap cleanup EXIT
  echo "▸ Downloading TokenMeter from $TOKENMETER_REPO@$TOKENMETER_REF..."
  curl -fsSL "$TOKENMETER_DOWNLOAD_URL" -o "$TMP_DIR/tokenmeter.tar.gz"
  tar -xzf "$TMP_DIR/tokenmeter.tar.gz" -C "$TMP_DIR"
  SRC_DIR="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d | sed -n '1p')"
  if [ -z "$SRC_DIR" ]; then
    echo "Downloaded archive did not contain a source directory" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$INSTALL_DIR")"
  rm -rf "$INSTALL_DIR"
  mv "$SRC_DIR" "$INSTALL_DIR"
  PYTHONPATH="$INSTALL_DIR/src" "$PYTHON_BIN" -m tokenmeter --help >/dev/null
}

write_server_runner() {
  TOKENMETER_BIND="${TOKENMETER_BIND:-0.0.0.0:18888}"
  TOKENMETER_DB="${TOKENMETER_DB:-$DATA_DIR/tokenmeter.sqlite}"
  TOKENMETER_AUTO_IMPORT_INTERVAL="${TOKENMETER_AUTO_IMPORT_INTERVAL:-15m}"
  TOKENMETER_AUTO_IMPORT_SINCE="${TOKENMETER_AUTO_IMPORT_SINCE:-1d}"
  TOKENMETER_HOME="${TOKENMETER_HOME:-$HOME}"
  if [ "${TOKENMETER_DISABLE_TOKEN:-0}" = "1" ]; then
    TOKENMETER_TOKEN=""
  elif [ -z "${TOKENMETER_TOKEN+x}" ]; then
    TOKENMETER_TOKEN="$(generate_token)"
  fi
  mkdir -p "$DATA_DIR"
  RUNNER="$INSTALL_DIR/tokenmeter-server.sh"
  cat > "$RUNNER" <<EOF
#!/bin/sh
set -eu
cd "$INSTALL_DIR"
PYTHONPATH="$INSTALL_DIR/src" exec "$PYTHON_BIN" -m tokenmeter serve \\
  --bind "\${TOKENMETER_BIND:-$TOKENMETER_BIND}" \\
  --db "\${TOKENMETER_DB:-$TOKENMETER_DB}" \\
  --token "\${TOKENMETER_TOKEN:-$TOKENMETER_TOKEN}" \\
  --auto-import-interval "\${TOKENMETER_AUTO_IMPORT_INTERVAL:-$TOKENMETER_AUTO_IMPORT_INTERVAL}" \\
  --auto-import-since "\${TOKENMETER_AUTO_IMPORT_SINCE:-$TOKENMETER_AUTO_IMPORT_SINCE}" \\
  --auto-import-home "\${TOKENMETER_HOME:-$TOKENMETER_HOME}" \\
  --auto-import-host "\${TOKENMETER_HOST:-$TOKENMETER_HOST}" \\
  --auto-import-agents "\${TOKENMETER_AGENTS:-$TOKENMETER_AGENTS}"
EOF
  chmod +x "$RUNNER"
}

write_agent_runner() {
  TOKENMETER_SERVER="${TOKENMETER_SERVER:-}"
  TOKENMETER_HOME="${TOKENMETER_HOME:-$HOME}"
  TOKENMETER_TOKEN="${TOKENMETER_TOKEN:-}"
  if [ -z "$TOKENMETER_SERVER" ]; then
    echo "TOKENMETER_SERVER is required in agent mode" >&2
    echo "Example: curl -fsSL .../scripts/install.sh | TOKENMETER_SERVER=\"https://your-server\" sh -s -- agent" >&2
    exit 2
  fi
  RUNNER="$INSTALL_DIR/tokenmeter-upload.sh"
  cat > "$RUNNER" <<EOF
#!/bin/sh
set -eu
cd "$INSTALL_DIR"
set -- "$PYTHON_BIN" -m tokenmeter upload \\
  --server "\${TOKENMETER_SERVER:-$TOKENMETER_SERVER}" \\
  --host "\${TOKENMETER_HOST:-$TOKENMETER_HOST}" \\
  --since "\${TOKENMETER_SINCE:-$TOKENMETER_SINCE}" \\
  --home "\${TOKENMETER_HOME:-$TOKENMETER_HOME}" \\
  --agents "\${TOKENMETER_AGENTS:-$TOKENMETER_AGENTS}"
if [ -n "\${TOKENMETER_TOKEN:-$TOKENMETER_TOKEN}" ]; then
  set -- "\$@" --token "\${TOKENMETER_TOKEN:-$TOKENMETER_TOKEN}"
fi
PYTHONPATH="$INSTALL_DIR/src" exec "\$@"
EOF
  chmod +x "$RUNNER"
}

install_systemd_root() {
  name="$1"
  runner="$2"
  env_file="/etc/$name.env"
  cat > "$env_file" <<EOF
TOKENMETER_BIND=${TOKENMETER_BIND:-}
TOKENMETER_DB=${TOKENMETER_DB:-}
TOKENMETER_TOKEN=${TOKENMETER_TOKEN:-}
TOKENMETER_SERVER=${TOKENMETER_SERVER:-}
TOKENMETER_HOST=$TOKENMETER_HOST
TOKENMETER_HOME=${TOKENMETER_HOME:-$HOME}
TOKENMETER_SINCE=$TOKENMETER_SINCE
TOKENMETER_AGENTS=$TOKENMETER_AGENTS
TOKENMETER_AUTO_IMPORT_INTERVAL=${TOKENMETER_AUTO_IMPORT_INTERVAL:-}
TOKENMETER_AUTO_IMPORT_SINCE=${TOKENMETER_AUTO_IMPORT_SINCE:-}
EOF
  chmod 600 "$env_file"
  if [ "$name" = "tokenmeter" ]; then
    cat > "/etc/systemd/system/$name.service" <<EOF
[Unit]
Description=TokenMeter dashboard and local collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$env_file
ExecStart=$runner
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now "$name.service"
  else
    cat > "/etc/systemd/system/$name.service" <<EOF
[Unit]
Description=Upload local token usage to TokenMeter
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=$env_file
ExecStart=$runner
EOF
    cat > "/etc/systemd/system/$name.timer" <<EOF
[Unit]
Description=Upload local token usage to TokenMeter every $TOKENMETER_INTERVAL seconds

[Timer]
OnBootSec=1min
OnUnitActiveSec=${TOKENMETER_INTERVAL}s
Persistent=true
Unit=$name.service

[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
    systemctl enable --now "$name.timer"
    systemctl start "$name.service" || true
  fi
}

install_systemd_user() {
  name="$1"
  runner="$2"
  user_dir="$HOME/.config/systemd/user"
  mkdir -p "$user_dir"
  if [ "$name" = "tokenmeter" ]; then
    cat > "$user_dir/$name.service" <<EOF
[Unit]
Description=TokenMeter dashboard and local collector
After=network-online.target

[Service]
Type=simple
ExecStart=$runner
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "$name.service"
  else
    cat > "$user_dir/$name.service" <<EOF
[Unit]
Description=Upload local token usage to TokenMeter
After=network-online.target

[Service]
Type=oneshot
Environment=TOKENMETER_SERVER=${TOKENMETER_SERVER:-}
Environment=TOKENMETER_TOKEN=${TOKENMETER_TOKEN:-}
Environment=TOKENMETER_HOST=$TOKENMETER_HOST
Environment=TOKENMETER_HOME=${TOKENMETER_HOME:-$HOME}
Environment=TOKENMETER_SINCE=$TOKENMETER_SINCE
Environment=TOKENMETER_AGENTS=$TOKENMETER_AGENTS
ExecStart=$runner
EOF
    cat > "$user_dir/$name.timer" <<EOF
[Unit]
Description=Upload local token usage to TokenMeter every $TOKENMETER_INTERVAL seconds

[Timer]
OnBootSec=1min
OnUnitActiveSec=${TOKENMETER_INTERVAL}s
Persistent=true
Unit=$name.service

[Install]
WantedBy=timers.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "$name.timer"
    systemctl --user start "$name.service" || true
  fi
}

install_launchd() {
  label="$1"
  runner="$2"
  interval="$3"
  plist="$HOME/Library/LaunchAgents/$label.plist"
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  if [ "$interval" = "0" ]; then
    schedule_block="  <key>KeepAlive</key><true/>"
  else
    schedule_block="  <key>StartInterval</key><integer>$interval</integer>"
  fi
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>$runner</string></array>
$schedule_block
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/$label.out.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/$label.err.log</string>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  launchctl kickstart -k "gui/$(id -u)/$label" || true
}

install_cron_agent() {
  runner="$1"
  if ! command -v crontab >/dev/null 2>&1; then
    return 1
  fi
  minute="*/15"
  current="$(crontab -l 2>/dev/null | grep -v 'tokenmeter-upload.sh' || true)"
  printf "%s\n%s\n" "$current" "$minute * * * * $runner >> $HOME/tokenmeter-upload.log 2>&1" | crontab -
  return 0
}

install_service() {
  name="$1"
  runner="$2"
  label="$3"
  if [ "$OS" = "Linux" ] && command -v systemctl >/dev/null 2>&1; then
    if [ "$IS_ROOT" = "1" ]; then
      install_systemd_root "$name" "$runner"
      return
    fi
    if systemctl --user show-environment >/dev/null 2>&1; then
      install_systemd_user "$name" "$runner"
      return
    fi
  fi
  if [ "$OS" = "Darwin" ] && command -v launchctl >/dev/null 2>&1; then
    interval="$TOKENMETER_INTERVAL"
    if [ "$name" = "tokenmeter" ]; then
      interval=0
    fi
    install_launchd "$label" "$runner" "$interval"
    return
  fi
  if [ "$name" = "tokenmeter-upload" ] && install_cron_agent "$runner"; then
    echo "✓ Installed crontab entry for uploads"
    return
  fi
  echo "No supported service manager found. Run manually: $runner" >&2
}

download_source

if [ "$TOKENMETER_MODE" = "server" ]; then
  write_server_runner
  install_service "tokenmeter" "$RUNNER" "io.tokenmeter.server"
  echo ""
  echo "✓ TokenMeter server installed"
  echo "Install dir: $INSTALL_DIR"
  echo "Database: ${TOKENMETER_DB:-$DATA_DIR/tokenmeter.sqlite}"
  echo "Bind: ${TOKENMETER_BIND:-0.0.0.0:18888}"
  if [ -n "${TOKENMETER_TOKEN:-}" ]; then
    echo "API token: $TOKENMETER_TOKEN"
    echo "Paste this token into the web page when prompted."
  else
    echo "API token: disabled"
  fi
  echo "Dashboard: http://<this-server>:18888/"
  echo ""
  echo "Uploader install example:"
  echo "curl -fsSL https://raw.githubusercontent.com/$TOKENMETER_REPO/$TOKENMETER_REF/scripts/install.sh | TOKENMETER_SERVER=\"http://<this-server>:18888\" TOKENMETER_TOKEN=\"$TOKENMETER_TOKEN\" sh -s -- agent"
else
  write_agent_runner
  echo "▸ First upload: last $TOKENMETER_BOOTSTRAP_SINCE..."
  TOKENMETER_SINCE="$TOKENMETER_BOOTSTRAP_SINCE" "$RUNNER" || true
  install_service "tokenmeter-upload" "$RUNNER" "io.tokenmeter.upload"
  echo ""
  echo "✓ TokenMeter uploader installed"
  echo "Install dir: $INSTALL_DIR"
  echo "Server: $TOKENMETER_SERVER"
  echo "Upload command: $RUNNER"
fi
