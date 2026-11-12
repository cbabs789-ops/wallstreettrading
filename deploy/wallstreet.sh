#!/usr/bin/env bash
# The `wallstreet` command on your server.
APP=/opt/wallstreet
ENVF=/etc/wallstreet.env
run() { cd "$APP" && sudo -u wallstreet env PWS_ENV_FILE="$ENVF" TZ=America/New_York python3 -m wallstreet "$@"; }
notify() {  # one Telegram line, used by auto-update
  tok="$(sudo grep -m1 '^TELEGRAM_BOT_TOKEN=' "$ENVF" | cut -d= -f2-)"; chat="$(sudo grep -m1 '^TELEGRAM_CHAT_ID=' "$ENVF" | cut -d= -f2-)"
  [ -n "$tok" ] && [ -n "$chat" ] && curl -fsS -m 10 "https://api.telegram.org/bot$tok/sendMessage" -d chat_id="$chat" --data-urlencode text="$1" >/dev/null || true
}

case "${1:-help}" in
  status)   systemctl is-active --quiet wallstreet && echo "Service: running" || echo "Service: STOPPED (try: wallstreet restart)"
            echo "Autopilot mode: $(run mode)"; run status ;;
  brief)    run briefing ;;
  pending)  run pending ;;
  approve|reject) run "$1" "$2" ;;
  mode)     shift; run mode "$@" ;;
  daily)    run daily ;;
  report)   shift; run report "$@" ;;
  publish)  run publish ;;
  demo)     run demo ;;
  logs)     journalctl -u wallstreet -f -n 50 ;;
  restart)  sudo systemctl restart wallstreet && echo "Restarted." ;;
  stop)     sudo systemctl stop wallstreet && echo "Stopped. Start again with: wallstreet restart" ;;
  config)   sudo nano "$ENVF" && sudo systemctl restart wallstreet && echo "Saved and restarted." ;;
  update)   sudo git -C "$APP" pull -q && sudo install -m 755 "$APP/deploy/wallstreet.sh" /usr/local/bin/wallstreet \
              && sudo install -m 644 "$APP/deploy/wallstreet.service" /etc/systemd/system/wallstreet.service \
              && sudo systemctl daemon-reload && sudo systemctl restart wallstreet && echo "Updated and restarted." ;;
  autoupdate)
    case "${2:-on}" in
      on)
        sudo tee /etc/systemd/system/wallstreet-autoupdate.service >/dev/null <<'UNIT'
[Unit]
Description=Install new Wall Street updates automatically
[Service]
Type=oneshot
ExecStart=/usr/local/bin/wallstreet auto-update-check
UNIT
        sudo tee /etc/systemd/system/wallstreet-autoupdate.timer >/dev/null <<'UNIT'
[Unit]
Description=Check for Wall Street updates every 10 minutes
[Timer]
OnBootSec=5min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
UNIT
        sudo systemctl daemon-reload && sudo systemctl enable --now wallstreet-autoupdate.timer >/dev/null \
          && echo "Auto-update is ON. New versions install within 10 minutes." ;;
      off) sudo systemctl disable --now wallstreet-autoupdate.timer >/dev/null 2>&1; echo "Auto-update is OFF. Use: wallstreet update" ;;
      *) systemctl is-active --quiet wallstreet-autoupdate.timer && echo "Auto-update: ON" || echo "Auto-update: OFF" ;;
    esac ;;
  auto-update-check)
    sudo git -C "$APP" fetch -q origin || exit 0
    [ "$(sudo git -C "$APP" rev-parse HEAD)" = "$(sudo git -C "$APP" rev-parse '@{u}')" ] && exit 0
    # don't restart in the middle of the daily run
    if [ -f /var/lib/wallstreet/daily.lock ] && ! sudo flock -n /var/lib/wallstreet/daily.lock true; then echo "Daily run in progress; later."; exit 0; fi
    sudo git -C "$APP" pull -q && sudo install -m 755 "$APP/deploy/wallstreet.sh" /usr/local/bin/wallstreet \
      && sudo install -m 644 "$APP/deploy/wallstreet.service" /etc/systemd/system/wallstreet.service \
      && sudo systemctl daemon-reload && sudo systemctl restart wallstreet \
      && notify "⬆️ Wall Street updated: $(sudo git -C "$APP" log -1 --format=%s)" && echo "Auto-updated." ;;
  setup-telegram) chat="$(run setup-telegram | tail -1)"
    if [ "$chat" -eq "$chat" ] 2>/dev/null; then sudo sed -i "s/^TELEGRAM_CHAT_ID=.*/TELEGRAM_CHAT_ID=$chat/" "$ENVF" && sudo systemctl restart wallstreet && echo "Linked."; else echo "$chat"; fi ;;
  *) cat <<'EOF'
wallstreet status | brief | pending | approve ID | reject ID | mode [approve|auto|off]
           daily | report [YYYY-MM] | publish | demo
           logs | restart | stop | config | update | autoupdate on|off|status | setup-telegram
EOF
  ;;
esac
