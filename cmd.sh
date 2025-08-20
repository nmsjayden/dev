#!/bin/bash
# ChromeOS Enrollment Avoidance Script (with random serial number support)

fail() {
    echo "[!] $1"
    exit 1
}

# Detect ChromeOS milestone
if [ -f /etc/lsb-release ]; then
    REL=$(grep -m 1 "^CHROMEOS_RELEASE_CHROME_MILESTONE=" /etc/lsb-release)
    REL="${REL#*=}"
else
    fail "Unable to detect ChromeOS milestone!"
fi

echo "[*] Detected ChromeOS milestone: r$REL"
echo
read -p "Do you want Verified Mode? (y/N): " VMODE

# --------------------------
# Serial number randomizer
# --------------------------
randomize_serial() {
    if [ "$(crossystem wpsw_cur 2>/dev/null)" = "0" ]; then
        echo "✅ Firmware write protection is disabled."

        # Save original serial number
        ORIG_SERIAL=$(vpd -g serial_number)
        echo "$ORIG_SERIAL" > /mnt/stateful_partition/original_serial.txt
        echo "💾 Original serial number saved: $ORIG_SERIAL"

        # Generate new random serial number
        NEW_SERIAL="RAND-$(cat /dev/urandom | tr -dc 'A-Z0-9' | head -c12)"
        echo "🔀 New randomized serial: $NEW_SERIAL"

        # Apply new serial
        vpd -s serial_number="$NEW_SERIAL"
        echo "✅ Serial number changed successfully."
    else
        echo "⚠️ Write protection is still enabled — cannot change serial number."
        echo "👉 Disable WP first if you want to randomize serial."
    fi
}

# --------------------------
# Version-specific functions
# --------------------------
r110_lower() {
    echo "[*] Running r110 and lower commands..."
    vpd -i RW_VPD -s check_enrollment=0
    echo "[*] Done. Now powerwash (verified or developer mode)."
}

r111_124() {
    echo "[*] Running r111–r124 commands..."
    vpd -i RW_VPD -s check_enrollment=0
    tpm_manager_client take_ownership
    cryptohome --action=remove_firmware_management_parameters
    echo "[*] Done. Now powerwash (verified or developer mode)."
}

r125_135_dev() {
    echo "[*] Running r125–r135 Developer Mode commands..."
    echo --enterprise-enable-unified-state-determination=never >/tmp/chrome_dev.conf
    echo --enterprise-enable-forced-re-enrollment=never >>/tmp/chrome_dev.conf
    echo --enterprise-enable-initial-enrollment=never >>/tmp/chrome_dev.conf
    mount --bind /tmp/chrome_dev.conf /etc/chrome_dev.conf
    initctl restart ui
    echo "[*] Done. Switch back to UI (Ctrl+Alt+F1) and finish setup without rebooting."
}

r125_135_verified() {
    echo "[*] Running r125–r135 Verified Mode commands..."
    vpd -i RW_VPD -s check_enrollment=0
    tpm_manager_client take_ownership
    cryptohome --action=remove_firmware_management_parameters
    randomize_serial
    echo "[*] Done. Now powerwash (verified mode)."
}

r136_plus_dev() {
    echo "[*] Running r136+ Developer Mode commands..."
    echo --enterprise-enable-state-determination=never >/tmp/chrome_dev.conf
    mount --bind /tmp/chrome_dev.conf /etc/chrome_dev.conf
    initctl restart ui
    echo "[*] Done. Switch back to UI (Ctrl+Alt+F1) and finish setup without rebooting."
}

r136_plus_verified() {
    echo "[*] Running r136+ Verified Mode commands..."
    randomize_serial
    echo "[*] Then powerwash (verified mode)."
}

# --------------------------
# Decision tree
# --------------------------
if [ "$REL" -le 110 ]; then
    r110_lower
elif [ "$REL" -ge 111 ] && [ "$REL" -le 124 ]; then
    r111_124
elif [ "$REL" -ge 125 ] && [ "$REL" -le 135 ]; then
    if [[ "$VMODE" =~ ^[Yy]$ ]]; then
        r125_135_verified
    else
        r125_135_dev
    fi
elif [ "$REL" -ge 136 ]; then
    if [[ "$VMODE" =~ ^[Yy]$ ]]; then
        r136_plus_verified
    else
        r136_plus_dev
    fi
else
    fail "Unsupported milestone or detection failed."
fi
