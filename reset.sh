#!/bin/bash
#25/5/26 v5
trap '' HUP  # survive SSH disconnect
exec > >(tee /config/reset.log) 2>&1

echo "Stopping Home Assistant to prevent memory overwrites..."
ha core stop

echo "Performing factory reset � erasing users, auth, mobile devices..."
cd /config

# 0. Bake the DISCARD-disable udev rule into the host overlay BEFORE this
# card gets imaged as the golden clone source. first_boot.sh's own
# apply_discard_fix() only takes effect starting the card's SECOND boot
# (udev rules load at boot time, so writing the rule mid-boot can't protect
# that same boot) -- but first_boot.sh does its heaviest writing (config,
# storage, addon stores) during exactly that unprotected first boot. Root
# cause of the 2026-07-06 corruption incident: a freshly cloned card got
# corrupted during that first-boot window despite the fix being "deployed".
# Writing the rule here, before capture, means every unit cloned from this
# image has it active from its literal first boot -- no gap. This runs
# directly (no nested ssh hop) because reset.sh already executes inside the
# SSH addon container, which has the docker socket needed to reach the host
# overlay (see project_sdcard_fix memory).
# v4 (2026-08-11): runs on the Supervisor-managed core image instead of
# busybox/alpine scratch images -- Supervisor flags any image outside the
# ecosystem as "Unsupported software" on every power-on, and this image is
# the source for every customer clone, so it must never carry them.
echo "Baking DISCARD-disable udev rule into host overlay for golden image..."
printf 'ACTION=="add", KERNEL=="mmcblk0", SUBSYSTEM=="block", ATTR{queue/discard_max_bytes}="0"\n' \
  > /tmp/99-mmc-nodiscard.rules
docker run --rm --entrypoint sh \
  -v /mnt/overlay:/host_overlay -v /tmp:/staging \
  "$(docker inspect homeassistant --format '{{.Config.Image}}')" \
  -c 'mkdir -p /host_overlay/etc/udev/rules.d && cp /staging/99-mmc-nodiscard.rules /host_overlay/etc/udev/rules.d/99-mmc-nodiscard.rules'
rm -f /tmp/99-mmc-nodiscard.rules
if docker run --rm --entrypoint sh -v /mnt/overlay:/host_overlay \
  "$(docker inspect homeassistant --format '{{.Config.Image}}')" \
  -c 'test -f /host_overlay/etc/udev/rules.d/99-mmc-nodiscard.rules'; then
  echo "DISCARD rule confirmed present in host overlay -- safe to capture this image."
else
  echo "ERROR: DISCARD rule NOT found in host overlay after write -- DO NOT capture this image until this is fixed."
fi

# 0b. (v52c) The alert-email credentials self-provision on every maintenance
# pass -- ensure_smtp_credential in first_boot.sh fetches them from the
# private repo on any unit whose .cytech_secrets carries
# CYTECH_SECRETS_TOKEN. That file is NOT wiped by this script (it is what
# ships the Tailscale client keys), so the token rides into the image iff the
# master has it. Check that here: an image captured without the token can
# never set up owner alert email on its clones, and nothing on a clone can
# fetch the token afterwards (it must already know a secret to authenticate
# to the private repo). Add the token line to /config/.cytech_secrets on the
# master and re-run this script before capturing.
if grep -q '^CYTECH_SECRETS_TOKEN=' /config/.cytech_secrets 2>/dev/null; then
  echo "CYTECH_SECRETS_TOKEN present -- clones will self-provision alert-email credentials."
else
  echo "ERROR: CYTECH_SECRETS_TOKEN missing from /config/.cytech_secrets -- DO NOT capture this image: its clones could never set up owner alert email and nothing on a clone can fetch the token afterwards. Add the token line and re-run."
fi

# 0c. (v5) Strip the master's own owner-specific data so no clone inherits it.
# The alert-email login and recipient are per-unit values: USER/PASS are
# refetched from the private repo on each clone's first maintenance pass (the
# token checked above is what authorises that fetch), and RECIPIENT is written
# when the new owner presses Register. Shipping the master's values would give
# every clone the same SMTP login and -- because cytech_alert_mail.py falls
# back to .cytech_registered when CY_SMTP_RECIPIENT is absent -- send a new
# owner's alert copies to the master's owner instead. The four fleet-wide
# lines (TAILSCALE_CLIENT_ID, TAILSCALE_CLIENT_SECRET, CYTECH_MANIFEST_URL,
# CYTECH_SECRETS_TOKEN) must stay; clones cannot provision without them.
if [ -f /config/.cytech_secrets ]; then
  cp -p /config/.cytech_secrets /tmp/.cytech_secrets.strip
  awk '!/^CY_SMTP_(USER|PASS|RECIPIENT)=/' /tmp/.cytech_secrets.strip > /tmp/.cytech_secrets.new \
    && cat /tmp/.cytech_secrets.new > /config/.cytech_secrets
  rm -f /tmp/.cytech_secrets.strip /tmp/.cytech_secrets.new
  chmod 600 /config/.cytech_secrets
  echo "Master alert-email credentials/recipient stripped from .cytech_secrets."
fi
# Stale copies of that file would otherwise carry the same values past the strip.
rm -f /config/.cytech_secrets.bak_*
# Owner identity the alert mailer falls back to; a clone must wait for its own
# owner to Register. These files (and the logs below) hold the owner's email
# address in clear.
rm -f /config/.cytech_registered
rm -f /config/.cytech_register_result
rm -f /config/.cytech_register_popup
rm -f /config/cytech_alerts.log
rm -f /config/cytech_update.log
rm -f /config/.cytech_email_activation_armed
# The restore-state cache still carries input_text.cytech_alert_recipient.
rm -f /config/.storage/core.restore_state

# 1. Auth and user accounts
rm -f .storage/auth
rm -f .storage/auth_provider.homeassistant
rm -f .storage/onboarding
rm -f .storage/cloud
rm -f .storage/person
rm -f .storage/person.corrupt.*

# 2. Mobile app base file
rm -f .storage/mobile_app

# 3. Safely remove Mobile App devices/entities WITHOUT touching Comfort MQTT entities
CE=".storage/core.config_entries"
DR=".storage/core.device_registry"
ER=".storage/core.entity_registry"

if [ -f "$CE" ]; then
    MOBILE_IDS=$(jq -r '.data.entries[]? | select(.domain == "mobile_app") | .entry_id' "$CE")
    if [ -n "$MOBILE_IDS" ]; then
        for ID in $MOBILE_IDS; do
            if [ -f "$ER" ]; then
                jq --arg id "$ID" '.data.entities |= map(select(.config_entry_id != $id))' "$ER" > "${ER}.tmp" && mv "${ER}.tmp" "$ER"
            fi
            if [ -f "$DR" ]; then
                jq --arg id "$ID" '.data.devices |= map(select(.config_entries | index($id) | not))' "$DR" > "${DR}.tmp" && mv "${DR}.tmp" "$DR"
            fi
        done
    fi
    jq '.data.entries |= map(select(.domain != "mobile_app" and .domain != "google_translate" and .domain != "met"))' "$CE" > "${CE}.tmp" && mv "${CE}.tmp" "$CE"
fi

# 4. Logs and old database (SQLite sidecars too -- a leftover WAL can still
# hold recorder rows from before the reset)
rm -f home-assistant.log
rm -f home-assistant_v2.db
rm -f home-assistant_v2.db-wal
rm -f home-assistant_v2.db-shm

# 5. Clear SSH terminal history
history -c
rm -f ~/.bash_history

# 6. Re-arm the Zero-Touch deployment script
echo "Resetting Zero-Touch deployment triggers..."
rm -f /config/.zero_touch_completed
# Keep /config/.cytech_version. It is the installed version marker, not a
# zero-touch trigger: with it present, a clone whose image matches the current
# fleet release reads as up to date and is not offered a same-version update.
# Erasing it made every clone read v0 and re-download the whole payload, which
# would also replace this very script with the manifest's copy (v4 while the
# fleet is still on v57) and undo the strip above.
rm -f /config/.cytech_update_pending
rm -f /config/.cytech_notify_pending
rm -f /config/.cytech_last_result
# Boot-history counters are master-specific: five or more entries within 24 h
# would make every clone's first boot read REPEATED_RESTARTS and raise the
# storage-card alert before the clone has done anything.
rm -f /config/.cytech_boot_events
rm -f /config/.cytech_boot_result
echo "Initializing Secure Link..." > /config/device_id.txt
rm -f /config/authorized_keys
rm -rf /config/.ssh
rm -f /config/www/remote_access_qr.png
rm -f /config/www/remote_access_qr_v2.png

# Clear SSH addon password/keys AND the persistent per-device admin password
# file (see ensure_ssh_admin_access in first_boot.sh) -- this is golden-image
# prep for cloning, not the customer-facing Reset to Default button, so the
# master image should ship fully blank. Each unit cloned from it mints its
# own fresh persistent password on its own first real boot; without removing
# this file too, every clone would otherwise inherit the master's password.
rm -f /config/.ssh_admin_password
# Also clear any live Remote Support Access window (see enable_remote_support.sh)
# -- a golden-image master with an active/stale window would otherwise hand
# every clone a pre-authorized operator key until whatever expiry timestamp
# happened to be on the master at capture time.
rm -f /config/.remote_support_expires_at
curl -s -H "Authorization: Bearer $SUPERVISOR_TOKEN" http://supervisor/addons/a0d7b954_ssh/info \
  | jq '.data.options | .ssh.password = "" | .ssh.authorized_keys = [] | {options: .}' > /tmp/ssh_reset_opts.json
curl -s -X POST -H "Authorization: Bearer $SUPERVISOR_TOKEN" -H "Content-Type: application/json" \
     -d @/tmp/ssh_reset_opts.json http://supervisor/addons/a0d7b954_ssh/options
# Drop boot:auto/watchdog too, or the clone's first boot has Supervisor
# auto-starting an addon we just made unstartable on purpose: init-ssh exits 1
# on blank credentials and the watchdog crash-loops it for ~2 minutes before
# giving up -- the same window in which first_boot.sh needs SSH to register
# Tailscale, and during which Supervisor answers /start with "already
# running!" instead of applying the credentials first_boot.sh just wrote.
# ensure_ssh_admin_access sets both back to auto/true once real credentials
# exist, so this only covers the gap before it runs.
curl -s -X POST -H "Authorization: Bearer $SUPERVISOR_TOKEN" -H "Content-Type: application/json" \
     -d '{"boot": "manual", "watchdog": false}' http://supervisor/addons/a0d7b954_ssh/options

# Clear per-user sidebar preferences so new users inherit the default lovelace_dashboards order
rm -f /config/.storage/frontend.user_data_*

# 7. Clear Tailscale state so every cloned SD card registers as a unique device
# Must STOP the addon first � daemon writes state back to disk on graceful shutdown if running
echo "Clearing Tailscale state for fresh node identity..."
curl -s -X POST -H "Authorization: Bearer $SUPERVISOR_TOKEN" http://supervisor/addons/a0d7b954_tailscale/stop
sleep 8
docker run --rm --entrypoint sh -v /mnt/data/supervisor/apps/data/a0d7b954_tailscale:/tsdata \
  "$(docker inspect homeassistant --format '{{.Config.Image}}')" \
  -c "rm -f /tsdata/tailscaled.state && rm -rf /tsdata/state && echo 'Tailscale state cleared'"

# Remove the scratch images the old versions of this script (and old
# first_boot.sh) used to pull -- a captured golden image must not ship them,
# or every clone flags "Unsupported software" at power-on.
docker image rm busybox alpine >/dev/null 2>&1 || true

echo "Reset complete. HA is stopped � safe to power off and clone SD card."
echo "On next boot, first_boot.sh will run automatically."
date > /config/reset_complete.txt