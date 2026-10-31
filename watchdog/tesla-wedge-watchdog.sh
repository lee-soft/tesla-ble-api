#!/bin/bash
# OPTIONAL. Only useful on a flaky onboard Wi-Fi+BT combo chip where the BLE
# adapter can wedge (ENOSYS on hci up) and ONLY a reboot recovers it. If you run
# a USB BT dongle you almost certainly do NOT need this — leave it disabled.
#
# Fires a reboot only on the confirmed wedge signature AND a dead session, with
# a cooldown so it can't loop. Reads the API's own /health to decide.
PORT="${TESLA_API_PORT:-8710}"
H=$(curl -s --max-time 5 "http://127.0.0.1:${PORT}/health" 2>/dev/null)
[ -n "$H" ] || exit 0

# wedge signature must be present in the session's last_error...
echo "$H" | grep -qiE "function not implemented|no devices available|can't init hci" || exit 0
# ...and the session must actually be dead (not just a transient)
echo "$H" | grep -q '"alive": false' || exit 0

# cooldown: at most one reboot per 4 min (flag on tmpfs, clears each boot)
FLAG=/run/tesla-wedge-reboot
now=$(date +%s); last=$(cat "$FLAG" 2>/dev/null || echo 0)
[ $((now - last)) -gt 240 ] || exit 0
echo "$now" > "$FLAG"

logger -t tesla-watchdog "BT adapter wedged (ENOSYS) + session dead -> rebooting to recover"
systemctl reboot
