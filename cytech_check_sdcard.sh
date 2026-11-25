#!/bin/bash
# SD-card health check (v35, reworked v61). Greps the HOST kernel journal of
# the CURRENT boot for card/I-O failure signatures. Runs on every HA start via
# the cytech_start_health_checks automation, and hourly via the v52a
# cytech_periodic_sd_check automation.
#
# Why `ha host logs` and not journalctl: this script runs in the Core
# container and reads the host journal through the SSH addon container. That
# addon is Alpine and has no journalctl (the previous implementation hid the
# "command not found" with 2>/dev/null, so it silently reported OK on every
# unit). `ha` IS present there and proxies to the Supervisor, which reads the
# host's journald -- verified live on cytech.local 2026-11-25.
#
# Why the current boot only: customer cards are duplicated from the master
# image, so /var/log/journal on every clone carries the master's boot history.
# Scanning all retained boots would FAIL a healthy new card on old master
# errors (e.g. the 2026-05-25 fstrim erase timeout). Restart-loop detection is
# NOT duplicated here: first_boot.sh's record_boot_event/check_boot_storm owns
# it and surfaces sensor.cytech_warning, which reset.sh already clears before
# capture.
#
# If the SSH client, key, addon or `ha` CLI is unavailable the check reports
# SKIP with the reason -- never a fake OK. The pre-ship harness treats SKIP as
# a failure, because a shipped unit must be able to run this check.
OUT=/config/.cytech_sdcard_result
LOG=/config/cytech_sd.log
SSH_KEY=/config/.ssh/id_rsa
SSH_HOST=a0d7b954-ssh

status=OK
message="no SD/IO errors found in this boot's kernel log"

ssh_run() {
  ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
    -o BatchMode=yes root@"$SSH_HOST" "$1" 2>/dev/null
}

write_result() {
  local s="$1" m="$2"
  python3 - "$OUT" "$s" "$m" << 'PYEOF'
import json, sys, time
with open(sys.argv[1], "w") as f:
    json.dump({"ts": int(time.time()), "status": sys.argv[2], "message": sys.argv[3]}, f)
PYEOF
  echo "sdcard: $status ($message)" >> "$LOG"
}

if ! command -v ssh >/dev/null 2>&1; then
  status=SKIP
  message="ssh client missing in Core container -- check skipped"
  write_result "$status" "$message"
  exit 0
fi

if [ ! -f "$SSH_KEY" ]; then
  status=SKIP
  message="SSH addon key /config/.ssh/id_rsa missing -- check skipped"
  write_result "$status" "$message"
  exit 0
fi

# The SSH addon may still be starting up on HA start; retry before giving up.
for i in 1 2 3; do
  if ssh_run true >/dev/null 2>&1; then
    break
  fi
  if [ "$i" = 3 ]; then
    status=SKIP
    message="SSH addon unavailable -- check skipped (will retry next start)"
    write_result "$status" "$message"
    exit 0
  fi
  sleep 15
done

# Current boot id. `ha host logs boots` lists retained boots as
#   "0":  <id>   <- the running boot
#   "-1": <id>
# so key "0" is the only one scoped to this card's current session.
BOOT_ID=$(ssh_run 'ha host logs boots' | awk '$1 == "\"0\":" { print $2; exit }')
if [ -z "$BOOT_ID" ]; then
  status=SKIP
  message="could not read the boot list via 'ha host logs boots' -- check skipped"
  write_result "$status" "$message"
  exit 0
fi

BOOT_LOG=$(ssh_run "ha host logs -b $BOOT_ID -t kernel -n 5000")
if [ $? -ne 0 ] || [ -z "$BOOT_LOG" ]; then
  status=SKIP
  message="could not read this boot's host kernel log via 'ha host logs' -- check skipped"
  write_result "$status" "$message"
  exit 0
fi

# Card/controller signatures only. Deliberately NOT a bare "mmc": mmc1 is the
# Pi's non-removable SDIO (WiFi/BT) bus and logs "Failed to initialize a
# non-removable card" on every boot, which is not a card fault. "mmc_erase"
# and "Card stuck being busy" are what the 2026-05-25 fstrim timeout looked
# like on the bench unit.
HITS=$(printf '%s\n' "$BOOT_LOG" | grep -iE \
  -e 'mmc0:.*(error|timeout|timed out|busy|failed|crc)' \
  -e 'mmcblk0.*(error|timeout|crc)' \
  -e 'I/O error, dev mmcblk0' \
  -e 'blk_update_request: I/O error, dev mmcblk0' \
  -e 'Buffer I/O error on dev mmcblk0' \
  -e 'EXT4-fs.*(error|corrupt|remounting filesystem read-only)' \
  -e 'FAT-fs \(mmcblk0p[0-9]\).*(error|unable|corrupt)' \
  -e 'F2FS.*\(mmcblk0p[0-9]\).*(error|corrupt)' \
  -e 'mmc_erase: erase error' \
  -e 'Card stuck being busy' | tail -5)

if [ -n "$HITS" ]; then
  status=FAIL
  message="SD/IO errors in this boot's kernel log: $(echo "$HITS" | tr '\n' '|' | head -c 380)"
fi

write_result "$status" "$message"
