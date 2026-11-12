#!/usr/bin/env bash
# Wall Street installer for an Ubuntu server (a fresh one, or the same one Gatekeeper runs on).
#   curl -fsSL https://raw.githubusercontent.com/cbabs789-ops/wallstreettrading/main/install.sh | sudo bash
# Paper trading only. Keys are typed here and stored only on this server, in /etc/wallstreet.env.
set -euo pipefail

REPO="${WALLSTREET_REPO:-https://github.com/cbabs789-ops/wallstreettrading.git}"
APP=/opt/wallstreet
DATA=/var/lib/wallstreet
ENVF=/etc/wallstreet.env
PORT=8765

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
if [ "$(id -u)" -ne 0 ]; then echo "Run this as root (or with sudo)."; exit 1; fi

say "1/6  Installing system packages"
if [ "$(awk '/MemTotal/{print $2}' /proc/meminfo)" -lt 1600000 ] && [ "$(swapon --show | wc -l)" -eq 0 ]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 git curl tzdata >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ needed"'

say "2/6  Downloading Wall Street"
if [ -d "$APP/.git" ]; then git -C "$APP" pull -q; else git clone -q "$REPO" "$APP"; fi
id wallstreet >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin wallstreet
mkdir -p "$DATA" && chown wallstreet:wallstreet "$DATA" && chmod 700 "$DATA"

say "3/6  Your settings (typed here, stored only on this server)"
systemctl stop wallstreet 2>/dev/null || true
clean() { printf '%s' "$1" | sed -e 's/\x1b\[20[01]~//g' -e 's/\[20[01]~//g' | tr -cd 'A-Za-z0-9:_-'; }
tg_ok() { curl -fsS -m 10 "https://api.telegram.org/bot$1/getMe" 2>/dev/null | grep -q '"ok":true'; }
get() { [ -f "$ENVF" ] && grep -m1 "^$1=" "$ENVF" | cut -d= -f2- | sed 's/^"//;s/"$//' || true; }

TG="$(clean "$(get TELEGRAM_BOT_TOKEN)")"; CHAT="$(get TELEGRAM_CHAT_ID)"; UA="$(get PWS_SEC_USER_AGENT)"
KEY="$(get PWS_DASH_KEY)"; STOK="$(get PWS_STATS_TOKEN)"; SREPO="$(get PWS_STATS_REPO)"

if [ -z "$UA" ]; then
  echo "The SEC and the Fed's data site require a name and contact email on every request."
  printf 'Your name and email (for example: Jane Doe jane@example.com): '
  read -r UA < /dev/tty
fi
echo
echo "Telegram: in the Telegram app, message @BotFather, send /newbot, pick a name, and copy the token it gives you."
while ! tg_ok "$TG"; do
  [ -n "$TG" ] && echo "Telegram doesn't recognize that token (starts '${TG:0:6}', ${#TG} characters). A real one looks like 8123456789:AAH... (about 46 characters)."
  printf 'Paste your Telegram bot token (input hidden), then press Enter: '
  read -rs RAW < /dev/tty; echo
  TG="$(clean "$RAW")"
done
BOTNAME="$(curl -fsS -m 10 "https://api.telegram.org/bot$TG/getMe" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')"
echo "Token works: @$BOTNAME"
[ -n "$KEY" ] || KEY="$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 22)"
IP="$(curl -fsS -m 10 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
if [ -z "$STOK" ]; then
  echo
  echo "Optional: publish stats to a private GitHub repo so results can be reviewed anywhere (like gatekeeper-stats)."
  echo "Make a fine-grained token with Contents read/write on ONE repo, or just press Enter to skip."
  printf 'GitHub stats token (input hidden): '
  read -rs STOK < /dev/tty; echo
  STOK="$(clean "$STOK")"
  if [ -n "$STOK" ]; then printf 'Stats repo [cbabs789-ops/wallstreet-stats]: '; read -r SREPO < /dev/tty; SREPO="${SREPO:-cbabs789-ops/wallstreet-stats}"; fi
fi

umask 077
cat > "$ENVF" <<EOF
# Wall Street settings. Edit with: wallstreet config
PWS_DATA_DIR=$DATA
PWS_SEC_USER_AGENT="$UA"
TELEGRAM_BOT_TOKEN=$TG
TELEGRAM_CHAT_ID=$CHAT
PWS_DASH_HOST=0.0.0.0
PWS_DASH_PORT=$PORT
PWS_DASH_KEY=$KEY
PWS_PUBLIC_URL=http://$IP:$PORT
PWS_STATS_TOKEN=$STOK
PWS_STATS_REPO=$SREPO
# How the paper autopilot trades: approve (every trade waits for your tap), auto, or off
PWS_AUTOPILOT_MODE=approve
TZ=America/New_York
EOF
chown root:wallstreet "$ENVF"; chmod 640 "$ENVF"
install -m 755 "$APP/deploy/wallstreet.sh" /usr/local/bin/wallstreet

if [ -z "$CHAT" ]; then
  say "4/6  Linking your Telegram chat"
  until CHAT="$(cd "$APP" && sudo -u wallstreet env PWS_ENV_FILE="$ENVF" python3 -m wallstreet setup-telegram 2>/dev/null | tail -1)" && [ -n "$CHAT" ] && [ "$CHAT" -eq "$CHAT" ] 2>/dev/null; do
    printf 'In Telegram, open @%s, send it "hi", then press Enter here (or type skip): ' "$BOTNAME"
    read -r ANS < /dev/tty
    [ "$ANS" = "skip" ] && { CHAT=""; break; }
  done
  [ -n "$CHAT" ] && sed -i "s/^TELEGRAM_CHAT_ID=.*/TELEGRAM_CHAT_ID=$CHAT/" "$ENVF" && echo "Linked to your chat."
else
  say "4/6  Telegram chat already linked"
fi

say "5/6  Loading 10 years of data and testing the strategies (about 10 minutes, once)"
cd "$APP"
sudo -u wallstreet env PWS_ENV_FILE="$ENVF" TZ=America/New_York python3 -m wallstreet daily || echo "First load had problems; the service will retry after today's close."
sudo -u wallstreet env PWS_ENV_FILE="$ENVF" python3 -m wallstreet replay >/dev/null 2>&1 || true

say "6/6  Starting the service"
install -m 644 "$APP/deploy/wallstreet.service" /etc/systemd/system/wallstreet.service
systemctl daemon-reload
systemctl enable -q --now wallstreet
systemctl restart wallstreet
/usr/local/bin/wallstreet autoupdate on || true
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow "$PORT/tcp" >/dev/null && echo "Opened port $PORT in the firewall."; fi
sleep 3
systemctl --no-pager --lines=0 status wallstreet | head -3 || true

say "Done. Wall Street is running (paper trading only)."
cat <<EOF
Dashboard: http://$IP:$PORT/?k=$KEY   (or send /site to @$BOTNAME)
In Telegram: /help, /status, /brief, /pending
Every trade the autopilot wants to make arrives in Telegram with Approve and Reject buttons.

Server commands:
  wallstreet status     service and paper account
  wallstreet logs       watch it work (Ctrl+C to stop watching)
  wallstreet brief      today's briefing
  wallstreet config     edit settings
  wallstreet update     install the latest version now
EOF
