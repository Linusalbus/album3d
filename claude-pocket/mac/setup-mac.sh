#!/bin/bash
# One-command setup of Claude Pocket on a Mac:
#   - runs the relay on this Mac (starts at login, restarts if it crashes)
#   - publishes it on a fixed, free https address with Tailscale Funnel
#   - installs the Claude Code hooks, status line, MCP server and CLAUDE.md block
#   - opens a page with a QR code to scan with the phone
#
#   bash setup-mac.sh             set up (safe to run again; keeps the same token)
#   bash setup-mac.sh --uninstall remove everything again
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RELAY_DIR="$(cd "$HERE/../relay" && pwd)"
CONF_DIR="$HOME/.claude-pocket"
ENV_FILE="$CONF_DIR/relay.env"
AGENTS="$HOME/Library/LaunchAgents"
RELAY_AGENT="com.claudepocket.relay"
WATCH_AGENT="com.claudepocket.watch"
PORT=8787

bold() { printf '\n\033[1m%s\033[0m\n' "$1"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

find_tailscale() {
  if command -v tailscale >/dev/null 2>&1; then echo tailscale
  elif [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]; then echo /Applications/Tailscale.app/Contents/MacOS/Tailscale
  fi
}

if [ "${1:-}" = "--uninstall" ]; then
  for a in "$RELAY_AGENT" "$WATCH_AGENT"; do
    launchctl bootout "gui/$(id -u)/$a" 2>/dev/null || true
    rm -f "$AGENTS/$a.plist"
  done
  TS="$(find_tailscale)"; [ -n "$TS" ] && "$TS" funnel --https=443 off 2>/dev/null || true
  node "$HERE/pocket.mjs" uninstall || true
  echo "Removed. Your data is still in $CONF_DIR (delete it if you want)."
  exit 0
fi

# ---------------------------------------------------------------- 1. requirements
bold "1/5  Checking requirements"
NODE="$(command -v node || true)"
[ -n "$NODE" ] || fail "Node.js is missing. Install the LTS version from https://nodejs.org (or: brew install node) and run this again."
"$NODE" -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 18 ? 0 : 1)' || fail "Node.js 18 or newer is needed (you have $("$NODE" -v))."
echo "Node $("$NODE" -v)"

TS="$(find_tailscale)"
[ -n "$TS" ] || fail "Tailscale is missing. Install it from https://tailscale.com/download/mac (or the Mac App Store), sign in, then run this again."
"$TS" status >/dev/null 2>&1 || fail "Tailscale is installed but not connected. Open Tailscale from the menu bar, sign in, then run this again."
echo "Tailscale connected"

# ---------------------------------------------------------------- 2. token + config
bold "2/5  Creating your private token"
mkdir -p "$CONF_DIR/relay-data"
chmod 700 "$CONF_DIR"
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  echo "Reusing the existing token"
else
  POCKET_TOKEN="$(openssl rand -hex 24)"
  NTFY_TOPIC="claude-pocket-$(openssl rand -hex 8)"
  printf 'POCKET_TOKEN=%s\nNTFY_TOPIC=%s\n' "$POCKET_TOKEN" "$NTFY_TOPIC" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "Saved in $ENV_FILE"
fi

# ---------------------------------------------------------------- 3. public address
bold "3/5  Publishing the relay with Tailscale Funnel"
HOSTNAME_TS="$("$TS" status --json | "$NODE" -e 'let s="";process.stdin.on("data",c=>s+=c).on("end",()=>process.stdout.write(JSON.parse(s).Self.DNSName.replace(/\.$/,"")))')"
PUBLIC_URL="https://$HOSTNAME_TS"
if ! "$TS" funnel --bg "$PORT" 2>&1 | tee /tmp/claude-pocket-funnel.log; then
  echo
  echo "Tailscale needs Funnel switched on for your account once."
  echo "Open the link above (or https://login.tailscale.com/admin/acls), allow Funnel, then run this script again."
  exit 1
fi
echo "Public address: $PUBLIC_URL"

# ---------------------------------------------------------------- 4. run at login
bold "4/5  Starting the relay (and keeping it running)"
mkdir -p "$AGENTS"
write_agent() { # label, program args..., env xml
  local label="$1" envxml="$2"; shift 2
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0"><dict>'
    echo "  <key>Label</key><string>$label</string>"
    echo '  <key>ProgramArguments</key><array>'
    for a in "$@"; do echo "    <string>$a</string>"; done
    echo '  </array>'
    echo "  <key>EnvironmentVariables</key><dict>$envxml</dict>"
    echo '  <key>RunAtLoad</key><true/>'
    echo '  <key>KeepAlive</key><true/>'
    echo "  <key>StandardOutPath</key><string>$CONF_DIR/$label.log</string>"
    echo "  <key>StandardErrorPath</key><string>$CONF_DIR/$label.log</string>"
    echo '</dict></plist>'
  } > "$AGENTS/$label.plist"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
}
write_agent "$RELAY_AGENT" "
    <key>POCKET_TOKEN</key><string>$POCKET_TOKEN</string>
    <key>PORT</key><string>$PORT</string>
    <key>DATA_DIR</key><string>$CONF_DIR/relay-data</string>
    <key>PUBLIC_URL</key><string>$PUBLIC_URL</string>
    <key>NTFY_URL</key><string>https://ntfy.sh/$NTFY_TOPIC</string>" \
  "$NODE" "$RELAY_DIR/server.mjs"

for _ in $(seq 1 20); do
  curl -fsS "http://localhost:$PORT/api/health" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -fsS "http://localhost:$PORT/api/health" >/dev/null || fail "The relay did not start. See $CONF_DIR/$RELAY_AGENT.log"
echo "Relay running on port $PORT"

# ---------------------------------------------------------------- 5. Claude Code
bold "5/5  Connecting Claude Code"
# The Mac talks to its own relay over localhost; the phone and claude.ai use the public address.
"$NODE" "$HERE/pocket.mjs" install --relay "http://localhost:$PORT" --token "$POCKET_TOKEN"
write_agent "$WATCH_AGENT" "<key>HOME</key><string>$HOME</string>" "$NODE" "$HERE/pocket.mjs" watch

# ---------------------------------------------------------------- pairing page
PAIR_URL="$PUBLIC_URL/#token=$POCKET_TOKEN"
MCP_URL="$PUBLIC_URL/mcp?token=$POCKET_TOKEN"
PAIR_FILE="$CONF_DIR/pair.html"
QR_SVG="$("$NODE" -e 'const q=require(process.argv[1])(0,"M");q.addData(process.argv[2]);q.make();process.stdout.write(q.createSvgTag({cellSize:6,margin:0,scalable:true}))' "$HERE/vendor/qrcode.cjs" "$PAIR_URL")"
cat > "$PAIR_FILE" <<HTML
<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pair Claude Pocket</title>
<style>
  :root{--bg:#fff;--text:#0d0d0d;--muted:#6b6b6b;--surface:#f4f4f4}
  @media (prefers-color-scheme:dark){:root{--bg:#212121;--text:#ececec;--muted:#a3a3a3;--surface:#2f2f2f}}
  body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 -apple-system,BlinkMacSystemFont,sans-serif}
  main{max-width:640px;margin:0 auto;padding:48px 16px}
  h1{font-size:28px;letter-spacing:-.4px;margin:0 0 6px} p{color:var(--muted);margin:0 0 16px}
  .step{background:var(--surface);border-radius:20px;padding:20px;margin:16px 0}
  .step h2{font-size:17px;margin:0 0 6px}
  #qr{background:#fff;border-radius:16px;padding:14px;display:inline-block;margin-top:8px}
  #qr svg{display:block;width:240px;height:240px}
  code{display:block;background:var(--bg);border-radius:10px;padding:10px 12px;font-size:13px;word-break:break-all;margin-top:8px}
</style></head><body><main>
  <h1>Pair your phone</h1>
  <p>Keep this page private — the code contains your token.</p>
  <div class="step"><h2>1. Scan with your Pixel</h2>
    <p>Open the camera, scan, and open the link in Chrome. Then Chrome menu → <b>Add to home screen</b> → <b>Install</b>.</p>
    <div id="qr">$QR_SVG</div></div>
  <div class="step"><h2>2. Notifications (optional)</h2>
    <p>Install <b>ntfy</b> from the Play Store and subscribe to this topic:</p><code>$NTFY_TOPIC</code></div>
  <div class="step"><h2>3. Let Claude text you from claude.ai too (optional)</h2>
    <p>claude.ai → Settings → Connectors → <b>Add custom connector</b>, name it <b>Claude Pocket</b>, and paste:</p><code>$MCP_URL</code>
    <p style="margin-top:10px">Claude Code on this Mac is already connected.</p></div>
</main></body></html>
HTML
chmod 600 "$PAIR_FILE"

bold "Done ✓"
echo "Phone app:      $PUBLIC_URL"
echo "Pairing page:   $PAIR_FILE (opening now)"
echo "Restart any open Claude Code sessions to pick up the hooks."
echo "Note: the app is reachable while this Mac is awake."
open "$PAIR_FILE" 2>/dev/null || true
