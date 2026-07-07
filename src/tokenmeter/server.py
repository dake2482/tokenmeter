from __future__ import annotations

import json
import socket
import io
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .collectors import collect_all, parse_since
from .records import UsageRecord
from .storage import (
    daily_summary_db,
    delete_legacy_codex_records,
    delete_legacy_openclaw_records,
    delete_zero_token_records,
    summarize_db,
    upsert_records,
)

APP_PREFIX = "/tokenmeter"
ASSET_DIR = Path(__file__).resolve().parents[2] / "assets"
ASSET_CONTENT_TYPES = {
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".svg": "image/svg+xml; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
}


def run_server(
    host: str,
    port: int,
    db_path: Path,
    token: str | None = None,
    auto_import_interval_seconds: float | None = None,
    auto_import_since: str = "1d",
    auto_import_home: Path | None = None,
    auto_import_host: str | None = None,
    auto_import_agents: tuple[str, ...] | None = None,
) -> None:
    class Handler(TokenMeterHandler):
        server_token = token
        database_path = db_path

    httpd = ThreadingHTTPServer((host, port), Handler)
    stop_event: threading.Event | None = None
    worker: threading.Thread | None = None
    if auto_import_interval_seconds and auto_import_interval_seconds > 0:
        stop_event = threading.Event()
        worker = threading.Thread(
            target=_auto_import_loop,
            args=(
                stop_event,
                db_path,
                auto_import_interval_seconds,
                auto_import_since,
                auto_import_home,
                auto_import_host or socket.gethostname(),
                auto_import_agents,
            ),
            name="tokenmeter-auto-import",
            daemon=True,
        )
        worker.start()
        print(f"auto import enabled every {auto_import_interval_seconds:g}s since={auto_import_since}")
    print(f"tokenmeter listening on http://{host}:{port}")
    try:
        httpd.serve_forever()
    finally:
        if stop_event:
            stop_event.set()
        if worker:
            worker.join(timeout=2)


def _auto_import_loop(
    stop_event: threading.Event,
    db_path: Path,
    interval_seconds: float,
    since_text: str,
    home: Path | None,
    host: str,
    agents: tuple[str, ...] | None,
) -> None:
    while not stop_event.is_set():
        started = time.time()
        try:
            since = parse_since(since_text)
            records = collect_all(home=home, host=host, since=since, agents=agents)
            cleaned = delete_zero_token_records(db_path)
            agent_names = {record.agent for record in records}
            if "codex" in agent_names:
                cleaned += delete_legacy_codex_records(db_path)
            if "openclaw" in agent_names:
                cleaned += delete_legacy_openclaw_records(db_path)
            changed = upsert_records(db_path, records)
            elapsed = time.time() - started
            print(
                f"auto import stored {len(records)} records "
                f"({changed + cleaned} changed, {cleaned} cleaned) in {elapsed:.1f}s"
            )
        except Exception as exc:
            print(f"auto import failed: {exc}")
        stop_event.wait(interval_seconds)


class TokenMeterHandler(BaseHTTPRequestHandler):
    server_token: str | None = None
    database_path: Path

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = _strip_app_prefix(parsed.path)
        if path in {"/", "/index.html"}:
            self._html(DASHBOARD_HTML)
            return
        if path == "/site.webmanifest":
            self._manifest(_manifest_payload())
            return
        asset_name = _asset_name_for_path(path)
        if asset_name:
            self._asset(asset_name)
            return
        if path == "/install.sh":
            self._text(_install_script(self._public_base_url()))
            return
        if path == "/tokenmeter.tar.gz":
            self._bytes(_source_tarball(), "application/gzip")
            return
        if path == "/health":
            self._json({"ok": True})
            return
        if path == "/api/v1/summary":
            if not self._authorized():
                return
            query = parse_qs(parsed.query)
            since = query.get("since", [None])[0]
            group_by_text = query.get("group_by", ["host,agent,profile,source,provider,model"])[0]
            group_by = tuple(part.strip() for part in group_by_text.split(",") if part.strip())
            rows = summarize_db(self.database_path, since=since, group_by=group_by)
            self._json({"rows": rows})
            return
        if path == "/api/v1/daily":
            if not self._authorized():
                return
            query = parse_qs(parsed.query)
            since = query.get("since", ["30d"])[0]
            group_by_text = query.get("group_by", ["agent"])[0]
            group_by = tuple(part.strip() for part in group_by_text.split(",") if part.strip())
            rows = daily_summary_db(self.database_path, since=since, group_by=group_by)
            self._json({"rows": rows})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = _strip_app_prefix(parsed.path)
        if path != "/api/v1/usage":
            self.send_error(404)
            return
        if not self._authorized():
            return
        try:
            payload = self._read_json()
            records = [UsageRecord.from_dict(item) for item in payload.get("records", [])]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            self.send_error(400, f"invalid payload: {exc}")
            return
        changed = upsert_records(self.database_path, records)
        self._json({"ok": True, "records": len(records), "changed": changed})

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _authorized(self) -> bool:
        if not self.server_token:
            return True
        expected = f"Bearer {self.server_token}"
        if self.headers.get("Authorization") == expected:
            return True
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":"unauthorized"}')
        return False

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            return

    def _html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            return

    def _text(self, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            return

    def _manifest(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ASSET_CONTENT_TYPES[".webmanifest"])
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            return

    def _asset(self, name: str) -> None:
        path = ASSET_DIR / name
        if not path.is_file():
            self.send_error(404)
            return
        self._bytes(path.read_bytes(), _content_type(path.name), cache_seconds=300)

    def _bytes(self, body: bytes, content_type: str, cache_seconds: int | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if cache_seconds is not None:
            self.send_header("Cache-Control", f"public, max-age={cache_seconds}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            return

    def _public_base_url(self) -> str:
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "127.0.0.1:18888"
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        return f"{proto}://{host}".rstrip("/")


def _strip_app_prefix(path: str) -> str:
    if path == APP_PREFIX:
        return "/"
    if path.startswith(f"{APP_PREFIX}/"):
        stripped = path[len(APP_PREFIX):]
        return stripped or "/"
    return path


def _asset_name_for_path(path: str) -> str | None:
    aliases = {
        "/favicon.ico": "favicon-plain-t.ico",
        "/favicon.svg": "tokenmeter-plain-t-icon.svg",
        "/apple-touch-icon.png": "apple-touch-icon-plain-t.png",
    }
    if path in aliases:
        return aliases[path]
    if not path.startswith("/assets/"):
        return None
    name = path.removeprefix("/assets/")
    if not name or "/" in name or name.startswith("."):
        return None
    return name


def _content_type(name: str) -> str:
    return ASSET_CONTENT_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def _manifest_payload() -> dict:
    return {
        "name": "TokenMeter",
        "short_name": "TokenMeter",
        "description": "多服务器 Agent token 用量统计看板",
        "start_url": APP_PREFIX,
        "scope": f"{APP_PREFIX}/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#0f172a",
        "icons": [
            {
                "src": f"{APP_PREFIX}/assets/tokenmeter-plain-t-icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable",
            },
            {
                "src": f"{APP_PREFIX}/assets/tokenmeter-plain-t-icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
        ],
    }


def _source_tarball() -> bytes:
    root = Path(__file__).resolve().parents[2]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in ("pyproject.toml", "README.md", "docs/tokenmeter.md", "assets", "src", "tests"):
            path = root / rel
            if path.exists():
                tar.add(path, arcname=f"tokenmeter/{rel}", filter=_tar_filter)
    return buf.getvalue()


def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = Path(info.name).parts
    ignored = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
    if any(part in ignored for part in parts):
        return None
    if any(part.startswith("._") or part.startswith(".__") for part in parts):
        return None
    if info.name.endswith((".pyc", ".pyo")):
        return None
    return info


def _install_script(public_base_url: str) -> str:
    upload_base = public_base_url
    download_base = f"{public_base_url}/tokenmeter"
    return f"""#!/bin/sh
set -eu

TOKENMETER_SERVER="${{1:-{upload_base}}}"
TOKENMETER_DOWNLOAD_BASE="${{2:-{download_base}}}"
TOKENMETER_HOST="${{TOKENMETER_HOST:-$(hostname)}}"
TOKENMETER_INTERVAL="${{TOKENMETER_INTERVAL:-900}}"
TOKENMETER_SINCE="${{TOKENMETER_SINCE:-1d}}"
TOKENMETER_BOOTSTRAP_SINCE="${{TOKENMETER_BOOTSTRAP_SINCE:-30d}}"
TOKENMETER_AGENTS="${{TOKENMETER_AGENTS:-hermes,openclaw,codex,zcode,workbuddy,claude}}"
TOKENMETER_HOME="${{TOKENMETER_HOME:-$HOME}}"
TOKENMETER_TOKEN="${{TOKENMETER_TOKEN:-}}"

need_cmd() {{
  command -v "$1" >/dev/null 2>&1 || {{ echo "缺少命令: $1" >&2; exit 1; }}
}}

need_cmd curl
need_cmd tar

PYTHON_BIN="${{TOKENMETER_PYTHON:-}}"
if [ -z "$PYTHON_BIN" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  else
    echo "缺少 python3" >&2
    exit 1
  fi
fi

OS="$(uname -s)"
if [ "$(id -u)" = "0" ] && [ "$OS" = "Linux" ]; then
  INSTALL_DIR="${{TOKENMETER_DIR:-/opt/tokenmeter}}"
else
  INSTALL_DIR="${{TOKENMETER_DIR:-$HOME/.local/share/tokenmeter}}"
fi

TMP_DIR="$(mktemp -d)"
cleanup() {{ rm -rf "$TMP_DIR"; }}
trap cleanup EXIT

echo "▸ 下载 TokenMeter..."
curl -fsSL "$TOKENMETER_DOWNLOAD_BASE/tokenmeter.tar.gz" -o "$TMP_DIR/tokenmeter.tar.gz"
tar -xzf "$TMP_DIR/tokenmeter.tar.gz" -C "$TMP_DIR"
mkdir -p "$(dirname "$INSTALL_DIR")"
rm -rf "$INSTALL_DIR"
mv "$TMP_DIR/tokenmeter" "$INSTALL_DIR"

RUNNER="$INSTALL_DIR/tokenmeter-upload.sh"
cat > "$RUNNER" <<EOF_RUNNER
#!/bin/sh
set -eu
TOKENMETER_DIR="$INSTALL_DIR"
TOKENMETER_PYTHON="$PYTHON_BIN"
TOKENMETER_SERVER="\\${{TOKENMETER_SERVER:-$TOKENMETER_SERVER}}"
TOKENMETER_HOST="\\${{TOKENMETER_HOST:-$TOKENMETER_HOST}}"
TOKENMETER_SINCE="\\${{TOKENMETER_SINCE:-$TOKENMETER_SINCE}}"
TOKENMETER_HOME="\\${{TOKENMETER_HOME:-$TOKENMETER_HOME}}"
TOKENMETER_AGENTS="\\${{TOKENMETER_AGENTS:-$TOKENMETER_AGENTS}}"
TOKENMETER_TOKEN="\\${{TOKENMETER_TOKEN:-$TOKENMETER_TOKEN}}"
cd "\\$TOKENMETER_DIR"
set -- "\\$TOKENMETER_PYTHON" -m tokenmeter upload --server "\\$TOKENMETER_SERVER" --host "\\$TOKENMETER_HOST" --since "\\$TOKENMETER_SINCE" --home "\\$TOKENMETER_HOME" --agents "\\$TOKENMETER_AGENTS"
if [ -n "\\$TOKENMETER_TOKEN" ]; then
  set -- "\\$@" --token "\\$TOKENMETER_TOKEN"
fi
PYTHONPATH="\\$TOKENMETER_DIR/src" exec "\\$@"
EOF_RUNNER
chmod +x "$RUNNER"

PYTHONPATH="$INSTALL_DIR/src" "$PYTHON_BIN" -m tokenmeter --help >/dev/null

install_systemd_root() {{
  ENV_FILE="/etc/tokenmeter-upload.env"
  cat > "$ENV_FILE" <<EOF_ENV
TOKENMETER_SERVER=$TOKENMETER_SERVER
TOKENMETER_HOST=$TOKENMETER_HOST
TOKENMETER_SINCE=$TOKENMETER_SINCE
TOKENMETER_HOME=$TOKENMETER_HOME
TOKENMETER_AGENTS=$TOKENMETER_AGENTS
TOKENMETER_TOKEN=$TOKENMETER_TOKEN
EOF_ENV
  chmod 600 "$ENV_FILE"
  cat > /etc/systemd/system/tokenmeter-upload.service <<EOF_SERVICE
[Unit]
Description=Upload local token usage to TokenMeter
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=$ENV_FILE
ExecStart=$RUNNER
EOF_SERVICE
  cat > /etc/systemd/system/tokenmeter-upload.timer <<EOF_TIMER
[Unit]
Description=Upload local token usage to TokenMeter every $TOKENMETER_INTERVAL seconds

[Timer]
OnBootSec=1min
OnUnitActiveSec=${{TOKENMETER_INTERVAL}}s
Persistent=true
Unit=tokenmeter-upload.service

[Install]
WantedBy=timers.target
EOF_TIMER
  systemctl daemon-reload
  systemctl enable --now tokenmeter-upload.timer
  systemctl start tokenmeter-upload.service || true
}}

install_systemd_user() {{
  USER_DIR="$HOME/.config/systemd/user"
  mkdir -p "$USER_DIR"
  cat > "$USER_DIR/tokenmeter-upload.service" <<EOF_SERVICE
[Unit]
Description=Upload local token usage to TokenMeter
After=network-online.target

[Service]
Type=oneshot
Environment=TOKENMETER_SERVER=$TOKENMETER_SERVER
Environment=TOKENMETER_HOST=$TOKENMETER_HOST
Environment=TOKENMETER_SINCE=$TOKENMETER_SINCE
Environment=TOKENMETER_HOME=$TOKENMETER_HOME
Environment=TOKENMETER_AGENTS=$TOKENMETER_AGENTS
Environment=TOKENMETER_TOKEN=$TOKENMETER_TOKEN
ExecStart=$RUNNER
EOF_SERVICE
  cat > "$USER_DIR/tokenmeter-upload.timer" <<EOF_TIMER
[Unit]
Description=Upload local token usage to TokenMeter every $TOKENMETER_INTERVAL seconds

[Timer]
OnBootSec=1min
OnUnitActiveSec=${{TOKENMETER_INTERVAL}}s
Persistent=true
Unit=tokenmeter-upload.service

[Install]
WantedBy=timers.target
EOF_TIMER
  systemctl --user daemon-reload
  systemctl --user enable --now tokenmeter-upload.timer
  systemctl --user start tokenmeter-upload.service || true
}}

install_launchd() {{
  PLIST="$HOME/Library/LaunchAgents/com.dake.tokenmeter-upload.plist"
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  cat > "$PLIST" <<EOF_PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.dake.tokenmeter-upload</string>
  <key>ProgramArguments</key>
  <array><string>$RUNNER</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>TOKENMETER_SERVER</key><string>$TOKENMETER_SERVER</string>
    <key>TOKENMETER_HOST</key><string>$TOKENMETER_HOST</string>
    <key>TOKENMETER_SINCE</key><string>$TOKENMETER_SINCE</string>
    <key>TOKENMETER_HOME</key><string>$TOKENMETER_HOME</string>
    <key>TOKENMETER_AGENTS</key><string>$TOKENMETER_AGENTS</string>
    <key>TOKENMETER_TOKEN</key><string>$TOKENMETER_TOKEN</string>
  </dict>
  <key>StartInterval</key><integer>$TOKENMETER_INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/tokenmeter-upload.out.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/tokenmeter-upload.err.log</string>
</dict>
</plist>
EOF_PLIST
  launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  launchctl kickstart -k "gui/$(id -u)/com.dake.tokenmeter-upload" || true
}}

echo "▸ 首次上传最近 $TOKENMETER_BOOTSTRAP_SINCE 数据..."
TOKENMETER_SINCE="$TOKENMETER_BOOTSTRAP_SINCE" "$RUNNER" || true

if [ "$OS" = "Linux" ] && command -v systemctl >/dev/null 2>&1; then
  if [ "$(id -u)" = "0" ]; then
    install_systemd_root
    echo "✓ 已安装 systemd timer: tokenmeter-upload.timer"
  else
    install_systemd_user
    echo "✓ 已安装 user systemd timer: tokenmeter-upload.timer"
  fi
elif [ "$OS" = "Darwin" ] && command -v launchctl >/dev/null 2>&1; then
  install_launchd
  echo "✓ 已安装 launchd: com.dake.tokenmeter-upload"
else
  echo "已安装到 $INSTALL_DIR，但当前系统没有可用的 systemd/launchd。"
  echo "请手动定时运行: $RUNNER"
fi

echo ""
echo "✓ 完成。页面: $TOKENMETER_SERVER/tokenmeter"
echo "上传命令: $RUNNER"
"""


DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="application-name" content="TokenMeter">
  <meta name="apple-mobile-web-app-title" content="TokenMeter">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="theme-color" content="#0f172a">
  <link rel="manifest" href="/tokenmeter/site.webmanifest">
  <link rel="icon" href="/tokenmeter/assets/favicon-plain-t.ico" sizes="any">
  <link rel="icon" href="/tokenmeter/assets/tokenmeter-plain-t-icon.svg" type="image/svg+xml">
  <link rel="apple-touch-icon" href="/tokenmeter/assets/apple-touch-icon-plain-t.png" sizes="180x180">
  <title>TokenMeter</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #ffffff;
      --card: #ffffff;
      --soft: #f7f7f9;
      --soft-card: #f4f4f6;
      --line: #e1e4ea;
      --text: #111827;
      --body: #4b5565;
      --muted: #9aa3b2;
      --track: #f0f1f4;
      --openclaw: #1397ad;
      --codex: #2f64e6;
      --hermes: #732ed8;
      --claude: #db7956;
      --workbuddy: #e3262b;
      --zcode: #bf2ed1;
      --gemini: #1ca34a;
      --cursor: #111827;
      --other: #7c8797;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 16px;
      letter-spacing: 0;
    }
    button, input {
      font: inherit;
      letter-spacing: 0;
    }
    .page {
      width: min(760px, calc(100vw - 40px));
      margin: 0 auto;
      padding: 20px 0 36px;
    }
    .panel {
      border: 2px solid var(--line);
      border-radius: 14px;
      background: var(--card);
    }
    .today-panel {
      background: var(--soft-card);
      padding: 22px 20px 20px;
      margin-bottom: 22px;
    }
    .today-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
    }
    .section-title {
      margin: 0;
      font-size: 20px;
      line-height: 1.2;
      font-weight: 850;
    }
    .date {
      color: var(--muted);
      font-size: 17px;
      font-weight: 650;
      white-space: nowrap;
    }
    .today-value {
      display: flex;
      align-items: baseline;
      flex-wrap: wrap;
      gap: 8px 14px;
      min-width: 0;
      margin-bottom: 14px;
    }
    .today-number {
      font-size: 44px;
      line-height: 1;
      font-weight: 900;
      white-space: nowrap;
    }
    .today-cost {
      color: #687184;
      font-size: 18px;
      font-weight: 650;
      white-space: normal;
    }
    .sync {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 16px;
      font-weight: 700;
      margin-bottom: 0;
    }
    .sync-dot {
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: #1bbf85;
      flex: 0 0 auto;
    }
    .metric-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 24px;
    }
    .metric-card {
      padding: 22px 20px 18px;
      min-height: 110px;
      min-width: 0;
    }
    .metric-value {
      font-size: 36px;
      line-height: 1.05;
      font-weight: 900;
      margin-bottom: 6px;
      overflow-wrap: anywhere;
      word-break: keep-all;
    }
    .metric-label {
      color: var(--muted);
      font-size: 16px;
      font-weight: 720;
    }
    .usage-panel {
      padding: 26px 22px 24px;
      margin-bottom: 24px;
    }
    .share-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
    }
    .share-layout {
      display: grid;
      grid-template-columns: 240px minmax(0, 1fr);
      align-items: center;
      gap: 28px;
      margin-top: 22px;
    }
    .pie-wrap {
      position: relative;
      width: min(100%, 220px);
      aspect-ratio: 1;
      margin: 0 auto;
    }
    .pie-chart {
      width: 100%;
      height: 100%;
      border-radius: 50%;
      background: var(--track);
      overflow: visible;
    }
    .pie-slice {
      cursor: default;
      transition: opacity 120ms ease;
    }
    .pie-slice:hover {
      opacity: 0.82;
    }
    .pie-center {
      position: absolute;
      inset: 25%;
      display: grid;
      align-content: center;
      justify-items: center;
      border-radius: 50%;
      background: #ffffff;
      text-align: center;
      pointer-events: none;
    }
    .pie-total {
      max-width: 100%;
      color: #050812;
      font-size: 20px;
      line-height: 1.05;
      font-weight: 900;
      white-space: nowrap;
    }
    .pie-label {
      margin-top: 8px;
      color: #111827;
      font-size: 13px;
      font-weight: 800;
    }
    .share-list {
      display: grid;
      gap: 14px;
      min-width: 0;
    }
    .share-row {
      display: grid;
      grid-template-columns: minmax(96px, 1fr) minmax(120px, 1.7fr) 128px;
      align-items: center;
      gap: 14px;
      color: #4b5565;
      font-size: 16px;
      font-weight: 700;
    }
    .share-name {
      display: inline-flex;
      align-items: center;
      gap: 12px;
      min-width: 0;
    }
    .share-name span:last-child {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .share-value {
      color: #111827;
      text-align: right;
      white-space: nowrap;
      font-weight: 800;
    }
    .legend {
      display: flex;
      flex-wrap: wrap;
      gap: 12px 22px;
      margin: 22px 0 18px;
      color: #687184;
      font-size: 18px;
      font-weight: 650;
    }
    .legend-item {
      display: inline-flex;
      align-items: center;
      gap: 12px;
      white-space: nowrap;
    }
    .dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--other);
      flex: 0 0 auto;
    }
    .chart-wrap {
      display: grid;
      grid-template-columns: 90px 1fr;
      align-items: stretch;
      min-height: 420px;
      margin-top: 8px;
    }
    .axis {
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      padding: 18px 14px 56px 0;
      color: var(--muted);
      font-size: 21px;
      font-weight: 650;
      text-align: right;
    }
    .chart {
      position: relative;
      min-width: 0;
      padding: 18px 8px 56px 0;
      overflow: hidden;
      cursor: grab;
      touch-action: pan-y;
      user-select: none;
    }
    .chart.dragging {
      cursor: grabbing;
    }
    .grid-line {
      position: absolute;
      left: 0;
      right: 0;
      border-top: 2px dashed #e0e3e8;
    }
    .grid-line.top { top: 18px; }
    .grid-line.mid { top: 50%; }
    .bars {
      position: relative;
      z-index: 1;
      display: flex;
      align-items: end;
      gap: 4px;
      height: 100%;
      min-height: 328px;
    }
    .bar {
      display: flex;
      flex: 1 1 14px;
      min-width: 12px;
      max-width: 34px;
      align-items: stretch;
      justify-content: end;
      flex-direction: column-reverse;
      border-radius: 3px;
      overflow: hidden;
      background: transparent;
    }
    .seg {
      width: 100%;
      min-height: 0;
    }
    .chart-dates {
      position: absolute;
      left: 0;
      right: 0;
      bottom: 16px;
      display: flex;
      justify-content: space-between;
      color: var(--muted);
      font-size: 24px;
      font-weight: 650;
    }
    .rank-panel {
      padding: 26px 22px 24px;
      margin-bottom: 24px;
    }
    .rank-list {
      display: grid;
      gap: 14px;
      margin-top: 22px;
    }
    .rank-row {
      display: grid;
      grid-template-columns: 120px minmax(110px, 1fr) 128px;
      align-items: center;
      gap: 14px;
      color: #4b5565;
      font-size: 16px;
      font-weight: 650;
    }
    .bar-track {
      height: 10px;
      overflow: hidden;
      border-radius: 999px;
      background: var(--track);
    }
    .bar-fill {
      height: 100%;
      min-width: 8px;
      border-radius: 999px;
      background: var(--other);
    }
    .rank-value {
      color: #687184;
      text-align: right;
      white-space: nowrap;
    }
    .show-all {
      width: 100%;
      border-top: 1px solid #eef0f3;
      border-right: 0;
      border-bottom: 0;
      border-left: 0;
      margin-top: 18px;
      padding-top: 14px;
      color: var(--muted);
      background: transparent;
      font-size: 16px;
      font-weight: 800;
      text-align: center;
      cursor: pointer;
    }
    .show-all[hidden] {
      display: none;
    }
    .detail-panel {
      padding: 26px 22px 12px;
    }
    .table-scroll {
      overflow-x: auto;
      margin-top: 18px;
    }
    table {
      width: 100%;
      min-width: 620px;
      border-collapse: collapse;
    }
    th, td {
      padding: 11px 4px;
      border-bottom: 1px solid #f0f2f5;
      text-align: right;
      color: #4b5565;
      font-size: 15px;
      font-weight: 650;
      white-space: nowrap;
    }
    th {
      color: var(--muted);
      font-size: 13px;
      font-weight: 800;
    }
    th:first-child, td:first-child {
      text-align: left;
    }
    .auth-panel {
      display: none;
      align-items: center;
      gap: 12px;
      border: 2px solid var(--line);
      border-radius: 18px;
      padding: 14px;
      margin-bottom: 20px;
      color: var(--body);
      background: #ffffff;
    }
    .auth-panel.show { display: flex; }
    .auth-panel input {
      flex: 1;
      min-width: 180px;
      height: 42px;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 0 12px;
    }
    .auth-panel button {
      height: 42px;
      border: 0;
      border-radius: 10px;
      padding: 0 16px;
      color: #ffffff;
      background: #111827;
      cursor: pointer;
    }
    .status {
      min-height: 0;
      margin: 0 0 18px;
      color: var(--muted);
      font-size: 16px;
    }
    .status:empty { display: none; }
    .status.error { color: #b42318; }
    .filters {
      display: grid;
      gap: 10px;
      margin: 0 0 22px;
    }
    .filter-row {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }
    .filter-chip {
      min-height: 40px;
      border: 2px solid var(--line);
      border-radius: 12px;
      padding: 0 18px;
      color: #596274;
      background: #ffffff;
      font-size: 18px;
      font-weight: 650;
      white-space: nowrap;
      cursor: pointer;
    }
    .filter-chip.active {
      color: var(--text);
      background: #f5f6f8;
      box-shadow: inset 0 0 0 1px #cfd5de;
      font-weight: 850;
    }
    .filter-chip:focus-visible {
      outline: 3px solid rgba(47, 100, 230, 0.25);
      outline-offset: 2px;
    }
    @media (max-width: 1100px) {
      table { min-width: 620px; }
    }
    @media (max-width: 980px) {
      .page { width: min(100% - 28px, 760px); padding-top: 20px; }
      .metric-grid { grid-template-columns: 1fr; }
      .today-panel, .usage-panel, .rank-panel, .detail-panel { padding: 22px 18px; border-radius: 14px; }
      .section-title { font-size: 20px; }
      .date, .sync, .metric-label, .chart-dates { font-size: 15px; }
      .today-value { display: block; }
      .today-number { font-size: 34px; margin-bottom: 8px; }
      .today-cost { font-size: 16px; }
      .legend { font-size: 16px; gap: 10px 14px; }
      .share-head { align-items: flex-start; flex-direction: column; }
      .share-layout { grid-template-columns: 1fr; gap: 28px; }
      .pie-wrap { width: min(220px, 76vw); }
      .pie-total { font-size: 20px; }
      .share-row { grid-template-columns: 1fr; gap: 10px; font-size: 18px; }
      .share-value { text-align: left; }
      .chart-wrap { grid-template-columns: 54px 1fr; min-height: 330px; }
      .axis { font-size: 16px; }
      .rank-row { grid-template-columns: 1fr; gap: 10px; font-size: 16px; }
      .show-all { font-size: 16px; }
      .filters { gap: 12px; }
      .filter-row { gap: 10px; }
      .filter-chip { min-height: 38px; border-radius: 12px; padding: 0 16px; font-size: 17px; }
      th, td { font-size: 15px; }
      th { font-size: 13px; }
    }
    @media (max-width: 560px) {
      html, body { overflow-x: hidden; }
      .page { width: 100%; padding: 10px 10px 24px; }
      .today-head { align-items: flex-start; flex-direction: column; }
      .today-panel, .usage-panel, .rank-panel, .detail-panel {
        padding: 18px 14px;
        border-radius: 12px;
      }
      .metric-grid { gap: 10px; margin-bottom: 16px; }
      .metric-card { min-height: 92px; padding: 18px 14px 14px; }
      .metric-value { font-size: 30px; }
      .pie-total { font-size: 19px; }
      .section-title { font-size: 19px; }
      .filters {
        overflow: hidden;
        margin-bottom: 16px;
      }
      .filter-row {
        flex-wrap: nowrap;
        overflow-x: auto;
        margin: 0 -10px;
        padding: 0 10px 4px;
        -webkit-overflow-scrolling: touch;
        scrollbar-width: none;
      }
      .filter-row::-webkit-scrollbar { display: none; }
      .filter-chip {
        flex: 0 0 auto;
        min-height: 36px;
        padding: 0 14px;
        font-size: 16px;
      }
      .share-layout { gap: 18px; }
      .share-row { font-size: 16px; }
      .chart-wrap { grid-template-columns: 46px 1fr; min-height: 286px; }
      .chart { padding-right: 0; }
      .axis { padding-right: 8px; font-size: 14px; }
      .chart-dates { font-size: 15px; }
      .bars { gap: 2px; min-height: 240px; }
      .bar { min-width: 9px; }
      .rank-row { grid-template-columns: 1fr; }
      .rank-row > div:first-child { overflow-wrap: anywhere; }
      .rank-value { text-align: left; }
      .auth-panel { align-items: stretch; flex-direction: column; }
      .detail-panel { overflow: hidden; }
      table { min-width: 560px; }
    }
    @media (max-width: 360px) {
      .today-number { font-size: 30px; }
      .metric-value { font-size: 28px; }
      .pie-total { font-size: 18px; }
      .today-panel, .usage-panel, .rank-panel, .detail-panel { padding-right: 12px; padding-left: 12px; }
      .chart-wrap { grid-template-columns: 42px 1fr; }
      .axis { font-size: 13px; }
      .bar { min-width: 8px; }
    }
  </style>
</head>
<body>
  <main class="page">
    <section id="authPanel" class="auth-panel" aria-label="API token">
      <span>API Token</span>
      <input id="tokenInput" type="password" autocomplete="off" placeholder="Bearer token">
      <button id="saveToken" type="button">刷新</button>
    </section>

    <section class="filters" aria-label="数据筛选">
      <div id="agentFilters" class="filter-row"></div>
      <div id="rangeFilters" class="filter-row"></div>
    </section>
    <p id="status" class="status"></p>

    <section class="panel today-panel">
      <div class="today-head">
        <h2 id="periodTitle" class="section-title">今日数据</h2>
        <div id="todayDate" class="date">--</div>
      </div>
      <div class="today-value">
        <div id="todayTokens" class="today-number">--</div>
        <div id="todayCost" class="today-cost">tokens · 成本未配置</div>
      </div>
      <div class="sync"><span class="sync-dot"></span><span id="syncText">最近同步 刚刚</span></div>
    </section>

    <section class="metric-grid">
      <article class="panel metric-card">
        <div id="totalTokens" class="metric-value">--</div>
        <div class="metric-label">总用量</div>
      </article>
      <article class="panel metric-card">
        <div id="totalCost" class="metric-value">未配置</div>
        <div class="metric-label">Token 成本</div>
      </article>
      <article class="panel metric-card">
        <div id="activeDays" class="metric-value">0</div>
        <div class="metric-label">活跃天数</div>
      </article>
    </section>

    <section class="panel usage-panel">
      <div class="share-head">
        <h2 id="shareTitle" class="section-title">今日用量占比</h2>
        <div id="shareDate" class="date">--</div>
      </div>
      <div class="share-layout">
        <div class="pie-wrap" aria-label="用量占比图">
          <svg id="sharePie" class="pie-chart" viewBox="0 0 100 100" role="img" aria-labelledby="shareTitle"></svg>
          <div class="pie-center">
            <div id="shareTotal" class="pie-total">--</div>
            <div id="shareLabel" class="pie-label">总用量</div>
          </div>
        </div>
        <div id="shareList" class="share-list"></div>
      </div>
    </section>

    <section class="panel rank-panel">
      <h2 id="modelRankTitle" class="section-title">今日按模型</h2>
      <div id="modelRanks" class="rank-list"></div>
      <button id="toggleModels" class="show-all" type="button">展开全部模型</button>
    </section>

    <section class="panel rank-panel">
      <h2 id="hostRankTitle" class="section-title">今日按服务器</h2>
      <div id="hostRanks" class="rank-list"></div>
      <button id="toggleHosts" class="show-all" type="button">展开全部服务器</button>
    </section>

    <section class="panel rank-panel">
      <h2 id="profileRankTitle" class="section-title">今日按 Profile</h2>
      <div id="profileRanks" class="rank-list"></div>
      <button id="toggleProfiles" class="show-all" type="button">展开全部 Profile</button>
    </section>

    <section class="panel detail-panel">
      <h2 class="section-title">历史明细</h2>
      <div class="table-scroll">
        <table>
          <thead><tr id="detailHead"></tr></thead>
          <tbody id="detailBody"></tbody>
        </table>
      </div>
    </section>
  </main>

  <script>
    const KNOWN_TOOLS = ["OpenClaw", "Codex", "Hermes", "Claude Code", "Cursor", "WorkBuddy", "ZCode", "Gemini"];
    const RANK_COLLAPSE_THRESHOLD = 10;
    const RANGE_OPTIONS = [
      {id: "today", label: "今天", title: "今日", days: 1, offset: 0},
      {id: "yesterday", label: "昨天", title: "昨日", days: 1, offset: 1},
      {id: "prevday", label: "前天", title: "前日", days: 1, offset: 2},
      {id: "3d", label: "近 3 天", title: "近 3 天", days: 3, offset: 0},
      {id: "7d", label: "近 7 天", title: "近 7 天", days: 7, offset: 0},
      {id: "30d", label: "近 30 天", title: "近 30 天", days: 30, offset: 0}
    ];
    const COLORS = {
      "OpenClaw": "var(--openclaw)",
      "Codex": "var(--codex)",
      "Hermes": "var(--hermes)",
      "Claude Code": "var(--claude)",
      "Cursor": "var(--cursor)",
      "WorkBuddy": "var(--workbuddy)",
      "ZCode": "var(--zcode)",
      "Gemini": "var(--gemini)",
      "Other": "var(--other)"
    };
    const MODEL_COLORS = ["#7e5cf1", "#2f64e6", "#f59e0b", "#1397ad", "#e3262b", "#1ca34a", "#7c8797"];
    const HOST_COLORS = ["#1397ad", "#2f64e6", "#732ed8", "#db7956", "#1ca34a", "#f59e0b", "#7c8797"];
    const tokenInput = document.getElementById("tokenInput");
    const authPanel = document.getElementById("authPanel");
    const statusEl = document.getElementById("status");
    const state = {
      data: null,
      chartWindow: 30,
      chartEndIndex: null,
      chartDays: [],
      modelExpanded: false,
      hostExpanded: false,
      profileExpanded: false,
      selectedAgent: "all",
      selectedRange: "today",
      agentExpanded: false
    };
    tokenInput.value = localStorage.getItem("tokenmeter.token") || "";
    document.getElementById("saveToken").addEventListener("click", () => {
      localStorage.setItem("tokenmeter.token", tokenInput.value.trim());
      refresh();
    });

    function setStatus(message, error = false) {
      statusEl.textContent = message || "";
      statusEl.className = error ? "status error" : "status";
    }

    function authHeaders() {
      const token = (localStorage.getItem("tokenmeter.token") || "").trim();
      return token ? {Authorization: `Bearer ${token}`} : {};
    }

    async function api(path) {
      const res = await fetch(path, {headers: authHeaders()});
      if (res.status === 401) {
        authPanel.classList.add("show");
        throw new Error("需要 API Token");
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      authPanel.classList.remove("show");
      return res.json();
    }

    function toolName(value) {
      const text = String(value || "").trim();
      const lower = text.toLowerCase();
      if (lower.includes("openclaw")) return "OpenClaw";
      if (lower.includes("codex")) return "Codex";
      if (lower.includes("hermes")) return "Hermes";
      if (lower.includes("claude")) return "Claude Code";
      if (lower.includes("cursor")) return "Cursor";
      if (lower.includes("workbuddy")) return "WorkBuddy";
      if (lower.includes("zcode")) return "ZCode";
      if (lower.includes("gemini")) return "Gemini";
      if (!text) return "Other";
      return text.slice(0, 1).toUpperCase() + text.slice(1);
    }

    function profileName(value) {
      const text = String(value || "").trim();
      return text && text.toLowerCase() !== "unknown" ? text : "default";
    }

    function hostName(value) {
      const text = String(value || "").trim();
      return text && text.toLowerCase() !== "unknown" ? text : "未知服务器";
    }

    function shouldSplitProfile(baseTool) {
      return baseTool === "OpenClaw" || baseTool === "Hermes";
    }

    function profileLabel(row) {
      const base = toolName(row?.agent);
      if (!shouldSplitProfile(base)) return base;
      return `${base} / ${profileName(row?.profile)}`;
    }

    function colorForToolLabel(name) {
      return colorFor(String(name || "").split(" / ")[0]);
    }

    function modelName(value) {
      const text = String(value || "").trim();
      const lower = text.toLowerCase();
      if (!text || lower === "unknown") return "其他";
      if (lower.startsWith("__") || lower.includes("bad_model") || lower.includes("fallback_test")) return "其他";
      const separatedGlm = lower.match(/^glm[-_ ]?(\\d+)[._-](\\d+)$/);
      if (separatedGlm) return `GLM-${separatedGlm[1]}.${separatedGlm[2]}`;
      const separatedGpt = lower.match(/^gpt[-_ ]?(\\d+)[._-](\\d+)(.*)$/);
      if (separatedGpt) return `GPT-${separatedGpt[1]}.${separatedGpt[2]}${separatedGpt[3]}`;
      const compactGlm = lower.replace(/[^a-z0-9]/g, "").match(/^glm(\\d)(\\d+)$/);
      if (compactGlm) return `GLM-${compactGlm[1]}.${compactGlm[2]}`;
      const compactGpt = lower.replace(/[^a-z0-9]/g, "").match(/^gpt(\\d)(\\d+)$/);
      if (compactGpt) return `GPT-${compactGpt[1]}.${compactGpt[2]}`;
      return text;
    }

    function colorFor(name) {
      return COLORS[name] || COLORS.Other;
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, char => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      })[char]);
    }

    function compactTokens(value, digits = 1) {
      const n = Number(value || 0);
      if (n >= 100000000) return `${trim(n / 100000000, 2)}亿`;
      if (n >= 10000) return `${trim(n / 10000, digits)}万`;
      return Math.round(n).toLocaleString("en-US");
    }

    function trim(value, digits) {
      return Number(value).toFixed(digits).replace(/\.0+$/, "").replace(/(\.\d*[1-9])0+$/, "$1");
    }

    function money(value) {
      if (value == null) return "未配置";
      const n = Number(value || 0);
      if (n >= 1000) return `$${Math.round(n).toLocaleString("en-US")}`;
      return `$${n.toFixed(2)}`;
    }

    function percent(part, total) {
      if (!total) return "0.0%";
      return `${trim(part / total * 100, 1)}%`;
    }

    function clamp(value, min, max) {
      return Math.max(min, Math.min(max, value));
    }

    function isoToday() {
      const d = new Date();
      const y = d.getFullYear();
      const m = String(d.getMonth() + 1).padStart(2, "0");
      const day = String(d.getDate()).padStart(2, "0");
      return `${y}-${m}-${day}`;
    }

    function addDays(iso, offset) {
      const date = new Date(`${iso}T00:00:00`);
      date.setDate(date.getDate() + offset);
      const y = date.getFullYear();
      const m = String(date.getMonth() + 1).padStart(2, "0");
      const d = String(date.getDate()).padStart(2, "0");
      return `${y}-${m}-${d}`;
    }

    function continuousDates(rows, endDate = null) {
      const dates = [...new Set(rows.map(row => row.date).filter(Boolean))].sort();
      if (!dates.length) return [endDate || isoToday()];
      const out = [];
      let cursor = dates[0];
      const lastDataDate = dates[dates.length - 1];
      const end = endDate && endDate > lastDataDate ? endDate : lastDataDate;
      while (cursor <= end && out.length < 500) {
        out.push(cursor);
        cursor = addDays(cursor, 1);
      }
      return out;
    }

    function rangeOption() {
      return RANGE_OPTIONS.find(option => option.id === state.selectedRange) || RANGE_OPTIONS[0];
    }

    function sum(rows, key = "total_tokens") {
      return rows.reduce((acc, row) => acc + Number(row[key] || 0), 0);
    }

    function actualCost(rows) {
      const cost = sum(rows, "estimated_cost_usd");
      return cost > 0 ? cost : null;
    }

    function byKey(rows, keyFn) {
      const map = new Map();
      for (const row of rows) {
        const key = keyFn(row);
        if (!map.has(key)) map.set(key, []);
        map.get(key).push(row);
      }
      return map;
    }

    function actualToolsFromRows(rows) {
      const totals = new Map();
      for (const row of rows || []) {
        const name = toolName(row.agent);
        const value = Number(row.total_tokens || 0);
        if (value > 0) totals.set(name, (totals.get(name) || 0) + value);
      }
      const tools = [...totals.entries()]
        .sort((a, b) => b[1] - a[1])
        .map(([name]) => name);
      return tools.length ? tools : ["Other"];
    }

    function actualTools(data) {
      return actualToolsFromRows(data.dailyByTool || []);
    }

    function agentMatches(row) {
      return state.selectedAgent === "all" || toolName(row.agent) === state.selectedAgent;
    }

    function anchorDate() {
      return isoToday();
    }

    function buildContext(data) {
      const allDays = continuousDates(data.dailyByTool || [], isoToday());
      state.chartDays = allDays;
      const option = rangeOption();
      const last = allDays.length - 1;
      const anchor = anchorDate();
      const anchorIndex = Math.max(0, allDays.indexOf(anchor));
      if (state.chartEndIndex == null || state.chartEndIndex > last) {
        state.chartEndIndex = clamp(anchorIndex - option.offset, 0, last);
      }
      state.chartWindow = option.days;
      const endIndex = clamp(state.chartEndIndex, 0, last);
      const startIndex = Math.max(0, endIndex - option.days + 1);
      const days = allDays.slice(startIndex, endIndex + 1);
      const dateSet = new Set(days);
      const toolRows = (data.dailyByTool || []).filter(row => dateSet.has(row.date) && agentMatches(row));
      const hostRows = (data.dailyByHost || []).filter(row => dateSet.has(row.date) && agentMatches(row));
      const agentModelRows = (data.dailyByAgentModel || []).filter(row => dateSet.has(row.date) && agentMatches(row));
      const modelRows = state.selectedAgent === "all"
        ? (data.dailyByModel || []).filter(row => dateSet.has(row.date))
        : agentModelRows;
      const tools = state.selectedAgent === "all"
        ? actualToolsFromRows(toolRows)
        : [state.selectedAgent];
      return {option, days, dateSet, toolRows, hostRows, modelRows, tools};
    }

    function formatDateRange(context) {
      if (!context.days.length) return "--";
      const first = context.days[0];
      const last = context.days[context.days.length - 1];
      return first === last ? first : `${first} 至 ${last}`;
    }

    function renderFilters(data) {
      const tools = actualTools(data).filter(name => name !== "Other");
      const visibleTools = state.agentExpanded ? tools : tools.slice(0, 5);
      const agentButtons = [
        `<button class="filter-chip ${state.selectedAgent === "all" ? "active" : ""}" type="button" data-agent="all">总榜</button>`,
        ...visibleTools.map(name => `<button class="filter-chip ${state.selectedAgent === name ? "active" : ""}" type="button" data-agent="${escapeHtml(name)}">${escapeHtml(name)}</button>`)
      ];
      if (tools.length > visibleTools.length) {
        agentButtons.push(`<button class="filter-chip" type="button" data-action="more-agents">更多 ${tools.length - visibleTools.length}</button>`);
      }
      document.getElementById("agentFilters").innerHTML = agentButtons.join("");
      document.getElementById("rangeFilters").innerHTML = RANGE_OPTIONS.map(option => (
        `<button class="filter-chip ${state.selectedRange === option.id ? "active" : ""}" type="button" data-range="${option.id}">${option.label}</button>`
      )).join("");
    }

    function renderTop(context) {
      const total = sum(context.toolRows);
      const cost = actualCost(context.toolRows);
      const activeDays = new Set(context.toolRows.filter(row => Number(row.total_tokens || 0) > 0).map(row => row.date)).size;

      document.getElementById("periodTitle").textContent = `${context.option.title}数据`;
      document.getElementById("todayDate").textContent = formatDateRange(context);
      document.getElementById("todayTokens").textContent = compactTokens(total, 1);
      document.getElementById("todayCost").textContent = cost == null ? "tokens · 成本未配置" : `tokens · ${money(cost)}`;
      document.getElementById("totalTokens").textContent = compactTokens(total, 2);
      document.getElementById("totalCost").textContent = money(cost);
      document.getElementById("activeDays").textContent = activeDays.toLocaleString("en-US");
      document.getElementById("syncText").textContent = "最近同步 刚刚";
      document.getElementById("modelRankTitle").textContent = `${context.option.title}按模型`;
      document.getElementById("hostRankTitle").textContent = `${context.option.title}按服务器`;
      document.getElementById("profileRankTitle").textContent = `${context.option.title}按 Profile`;
    }

    function renderShare(context) {
      const rankData = buildRankRows(
        context.toolRows,
        row => toolName(row.agent),
        row => colorFor(row.name)
      );
      const total = rankData.total;
      const rows = rankData.ranked;
      const pie = document.getElementById("sharePie");
      const shareTotalEl = document.getElementById("shareTotal");
      const shareLabelEl = document.getElementById("shareLabel");
      const resetShareCenter = () => {
        shareTotalEl.textContent = compactTokens(total, 1);
        shareLabelEl.textContent = "总用量";
      };
      document.getElementById("shareTitle").textContent = `${context.option.title}用量占比`;
      document.getElementById("shareDate").textContent = formatDateRange(context);
      resetShareCenter();
      if (!rows.length || !total) {
        pie.innerHTML = `<circle cx="50" cy="50" r="36" fill="none" stroke="var(--track)" stroke-width="28"><title>暂无数据</title></circle>`;
        shareTotalEl.textContent = "--";
        shareLabelEl.textContent = "暂无数据";
        document.getElementById("shareList").innerHTML = `<div class="share-row"><div>暂无数据</div><div class="bar-track"></div><div class="share-value">--</div></div>`;
        return;
      }

      let offset = 0;
      pie.innerHTML = rows.map((row, index) => {
        const pct = row.total_tokens / total * 100;
        const dash = rows.length === 1 ? 100 : Math.max(0, pct - 0.35);
        const value = compactTokens(row.total_tokens, 2);
        const pctText = percent(row.total_tokens, total);
        const label = `${row.name} ${value} · ${pctText}`;
        const html = `
          <circle class="pie-slice"
            cx="50" cy="50" r="36" fill="none"
            stroke="${rankData.colorFn(row, index)}"
            stroke-width="28"
            stroke-dasharray="${dash} ${100 - dash}"
            stroke-dashoffset="${-offset}"
            pathLength="100"
            transform="rotate(-90 50 50)"
            tabindex="0"
            aria-label="${escapeHtml(label)}"
            data-name="${escapeHtml(row.name)}"
            data-value="${escapeHtml(value)}"
            data-percent="${escapeHtml(pctText)}">
            <title>${escapeHtml(label)}</title>
          </circle>
        `;
        offset += pct;
        return html;
      }).join("");
      pie.querySelectorAll(".pie-slice").forEach(slice => {
        const showSlice = () => {
          shareTotalEl.textContent = slice.dataset.value || "--";
          shareLabelEl.textContent = `${slice.dataset.name || "用量"} · ${slice.dataset.percent || ""}`.trim();
        };
        slice.addEventListener("mouseenter", showSlice);
        slice.addEventListener("mouseleave", resetShareCenter);
        slice.addEventListener("focus", showSlice);
        slice.addEventListener("blur", resetShareCenter);
      });
      document.getElementById("shareList").innerHTML = rows.slice(0, 8).map((row, index) => {
        const pct = percent(row.total_tokens, total);
        const value = compactTokens(row.total_tokens, 2);
        const label = `${row.name} ${value} · ${pct}`;
        return `
          <div class="share-row" title="${escapeHtml(label)}">
            <div class="share-name"><span class="dot" style="background:${rankData.colorFn(row, index)}"></span><span>${escapeHtml(row.name)}</span></div>
            <div class="bar-track"><div class="bar-fill" style="width:${Math.max(6, row.total_tokens / rankData.max * 100)}%;background:${rankData.colorFn(row, index)}"></div></div>
            <div class="share-value">${value} · ${pct}</div>
          </div>
        `;
      }).join("");
    }

    function buildRankRows(rows, keyFn, colorFn) {
      const grouped = new Map();
      for (const row of rows) {
        const key = keyFn(row);
        if (!grouped.has(key)) grouped.set(key, {name: key, total_tokens: 0, estimated_cost_usd: 0});
        grouped.get(key).total_tokens += Number(row.total_tokens || 0);
        grouped.get(key).estimated_cost_usd += Number(row.estimated_cost_usd || 0);
      }
      const ranked = [...grouped.values()]
        .filter(row => row.total_tokens > 0)
        .sort((a, b) => b.total_tokens - a.total_tokens);
      const total = sum(ranked);
      const max = Math.max(...ranked.map(row => row.total_tokens), 1);
      return {ranked, total, max, colorFn};
    }

    function rankRows(rankData, limit = 3) {
      const visible = rankData.ranked.slice(0, limit);
      const total = rankData.total;
      return visible.map((row, index) => `
        <div class="rank-row" title="${escapeHtml(`${row.name} ${compactTokens(row.total_tokens, 2)} · ${percent(row.total_tokens, total)}`)}">
          <div>${escapeHtml(row.name)}</div>
          <div class="bar-track"><div class="bar-fill" style="width:${Math.max(6, row.total_tokens / rankData.max * 100)}%;background:${rankData.colorFn(row, index)}"></div></div>
          <div class="rank-value">${compactTokens(row.total_tokens, 2)} · ${percent(row.total_tokens, rankData.total)}</div>
        </div>
      `).join("") || `<div class="rank-row"><div>暂无数据</div><div class="bar-track"></div><div class="rank-value">--</div></div>`;
    }

    function renderRanks(context) {
      const modelRanks = buildRankRows(
        context.modelRows,
        row => modelName(row.model),
        (_row, index) => MODEL_COLORS[index % MODEL_COLORS.length]
      );
      const modelLimit = state.modelExpanded || modelRanks.ranked.length <= RANK_COLLAPSE_THRESHOLD
        ? modelRanks.ranked.length
        : RANK_COLLAPSE_THRESHOLD;
      document.getElementById("modelRanks").innerHTML = rankRows(modelRanks, modelLimit);
      updateToggle("toggleModels", state.modelExpanded, modelRanks.ranked.length, "模型");
    }

    function renderHostRanks(context) {
      const hostRanks = buildRankRows(
        context.hostRows,
        row => hostName(row.host),
        (_row, index) => HOST_COLORS[index % HOST_COLORS.length]
      );
      const hostLimit = state.hostExpanded || hostRanks.ranked.length <= RANK_COLLAPSE_THRESHOLD
        ? hostRanks.ranked.length
        : RANK_COLLAPSE_THRESHOLD;
      document.getElementById("hostRanks").innerHTML = rankRows(hostRanks, hostLimit);
      updateToggle("toggleHosts", state.hostExpanded, hostRanks.ranked.length, "服务器");
    }

    function renderProfileRanks(context) {
      const profileRows = context.toolRows.filter(row => shouldSplitProfile(toolName(row.agent)));
      const profileRanks = buildRankRows(
        profileRows,
        row => profileLabel(row),
        row => colorForToolLabel(row.name)
      );
      const profileLimit = state.profileExpanded || profileRanks.ranked.length <= RANK_COLLAPSE_THRESHOLD
        ? profileRanks.ranked.length
        : RANK_COLLAPSE_THRESHOLD;
      document.getElementById("profileRanks").innerHTML = rankRows(profileRanks, profileLimit);
      updateToggle("toggleProfiles", state.profileExpanded, profileRanks.ranked.length, "Profile");
    }

    function updateToggle(id, expanded, count, label) {
      const button = document.getElementById(id);
      if (!button) return;
      if (count <= RANK_COLLAPSE_THRESHOLD) {
        button.hidden = true;
        return;
      }
      button.hidden = false;
      button.textContent = expanded ? `收起${label}` : `展开全部${label}`;
    }

    function renderHistoryDetails(data) {
      const historyRows = (data.dailyByTool || []).filter(agentMatches);
      const days = continuousDates(historyRows).reverse();
      const baseTools = (state.selectedAgent === "all" ? actualToolsFromRows(historyRows) : [state.selectedAgent]).slice(0, 6);
      const dailyMap = new Map();
      for (const row of historyRows) {
        if (!dailyMap.has(row.date)) dailyMap.set(row.date, {});
        const tool = toolName(row.agent);
        dailyMap.get(row.date)[tool] = (dailyMap.get(row.date)[tool] || 0) + Number(row.total_tokens || 0);
      }
      document.getElementById("detailHead").innerHTML = `<th>日期</th>${baseTools.map(name => `<th>${escapeHtml(name)}</th>`).join("")}`;
      document.getElementById("detailBody").innerHTML = days.map(day => {
        const values = dailyMap.get(day) || {};
        return `<tr><td>${day}</td>${baseTools.map(name => {
          const value = Number(values[name] || 0);
          const text = value ? compactTokens(value, 1) : "–";
          const title = value ? `${day} ${name} ${compactTokens(value, 2)}` : `${day} ${name} 无数据`;
          return `<td title="${escapeHtml(title)}">${text}</td>`;
        }).join("")}</tr>`;
      }).join("");
    }

    function shiftChartWindow(deltaDays) {
      if (!state.data || !state.chartDays.length) return;
      if (rangeOption().days === 1) return;
      const last = state.chartDays.length - 1;
      state.chartEndIndex = clamp((state.chartEndIndex ?? last) + deltaDays, 0, last);
      renderDashboard(state.data);
    }

    function renderDashboard(data) {
      renderFilters(data);
      const context = buildContext(data);
      renderTop(context);
      renderShare(context);
      renderRanks(context);
      renderHostRanks(context);
      renderProfileRanks(context);
      renderHistoryDetails(data);
    }

    function wireInteractions() {
      document.getElementById("agentFilters").addEventListener("click", event => {
        const button = event.target.closest("button");
        if (!button) return;
        if (button.dataset.action === "more-agents") {
          state.agentExpanded = true;
          if (state.data) renderDashboard(state.data);
          return;
        }
        if (button.dataset.agent) {
          state.selectedAgent = button.dataset.agent;
          state.modelExpanded = false;
          state.hostExpanded = false;
          state.profileExpanded = false;
          if (state.data) renderDashboard(state.data);
        }
      });
      document.getElementById("rangeFilters").addEventListener("click", event => {
        const button = event.target.closest("button[data-range]");
        if (!button) return;
        state.selectedRange = button.dataset.range;
        state.chartEndIndex = null;
        state.modelExpanded = false;
        state.hostExpanded = false;
        state.profileExpanded = false;
        if (state.data) renderDashboard(state.data);
      });

      const modelButton = document.getElementById("toggleModels");
      const hostButton = document.getElementById("toggleHosts");
      const profileButton = document.getElementById("toggleProfiles");
      modelButton.addEventListener("click", () => {
        state.modelExpanded = !state.modelExpanded;
        if (state.data) renderDashboard(state.data);
      });
      hostButton.addEventListener("click", () => {
        state.hostExpanded = !state.hostExpanded;
        if (state.data) renderDashboard(state.data);
      });
      profileButton.addEventListener("click", () => {
        state.profileExpanded = !state.profileExpanded;
        if (state.data) renderDashboard(state.data);
      });

      const chart = document.querySelector(".chart");
      if (chart) {
        let drag = null;
        chart.addEventListener("pointerdown", event => {
          if (!state.data) return;
          if (rangeOption().days === 1) return;
          drag = {x: event.clientX, endIndex: state.chartEndIndex ?? state.chartDays.length - 1};
          chart.classList.add("dragging");
          chart.setPointerCapture(event.pointerId);
        });
        chart.addEventListener("pointermove", event => {
          if (!drag || !state.chartDays.length) return;
          const step = Math.round((drag.x - event.clientX) / 22);
          const last = state.chartDays.length - 1;
          state.chartEndIndex = clamp(drag.endIndex + step, 0, last);
          renderDashboard(state.data);
        });
        for (const eventName of ["pointerup", "pointercancel", "lostpointercapture"]) {
          chart.addEventListener(eventName, () => {
            drag = null;
            chart.classList.remove("dragging");
          });
        }
        chart.addEventListener("wheel", event => {
          const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
          if (!delta) return;
          event.preventDefault();
          shiftChartWindow(Math.sign(delta));
        }, {passive: false});
      }
    }

    async function refresh() {
      setStatus("加载中...");
      try {
        const [dailyByTool, dailyByModel, dailyByAgentModel, dailyByHost] = await Promise.all([
          api("/api/v1/daily?since=all&group_by=agent,profile"),
          api("/api/v1/daily?since=all&group_by=model"),
          api("/api/v1/daily?since=all&group_by=agent,profile,model"),
          api("/api/v1/daily?since=all&group_by=host,agent")
        ]);
        const data = {
          dailyByTool: dailyByTool.rows || [],
          dailyByModel: dailyByModel.rows || [],
          dailyByAgentModel: dailyByAgentModel.rows || [],
          dailyByHost: dailyByHost.rows || []
        };
        state.data = data;
        renderDashboard(data);
        setStatus("");
      } catch (err) {
        setStatus(err.message || String(err), true);
      }
    }

    wireInteractions();
    refresh();
    setInterval(refresh, 60 * 1000);
  </script>
</body>
</html>
"""
