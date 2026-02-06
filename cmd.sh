#!/bin/bash
# ChromeOS Enrollment Avoidance Script
# Fully debugged for VT2 with serial backup, restore, and random/manual serial option

set -euo pipefail

fail() {
    echo "[!] $1"
    exit 1
}

# --------------------------
# Check required commands
# --------------------------
for bin in vpd tpm_manager_client device_management_client crossystem flashrom cryptohome initctl; do
    command -v "$bin" >/dev/null 2>&1 || fail "Missing required command: $bin"
done

# --------------------------
# Detect ChromeOS milestone
# --------------------------
if [ -f /etc/lsb-release ]; then
    REL=$(grep -m 1 "^CHROMEOS_RELEASE_CHROME_MILESTONE=" /etc/lsb-release)
    REL="${REL#*=}"
else
    fail "Unable to detect ChromeOS milestone!"
fi

echo "[*] Detected ChromeOS milestone: r$REL"
echo
read -p "Do you want Verified Mode (N for Dev Mode)? (Y/N): " VMODE < /dev/tty
if [ -z "$VMODE" ]; then
    echo "[!] No input detected — defaulting to Developer Mode."
    VMODE="N"
fi

# Optional: confirm before running
echo "Selected mode: $( [[ "$VMODE" =~ ^[Yy]$ ]] && echo 'Verified' || echo 'Developer' )"
read -p "Proceed with these commands? (Y/N): " CONFIRM < /dev/tty
[[ ! "$CONFIRM" =~ ^[Yy]$ ]] && { echo "[*] Cancelled by user."; exit 0; }

# --------------------------
# Flash Write Protection Check
# --------------------------
check_wp() {
    local status
    status=$(flashrom --wp-status 2>/dev/null | grep -i "Protection mode" || true)
    if echo "$status" | grep -iq "disabled"; then
        echo "disabled"
    else
        echo "enabled"
    fi
}

# --------------------------
# Serial number management
# --------------------------
manage_serial() {
    ORIG_SERIAL=$(vpd -g serial_number | tr -d '[:space:]')
    echo "[INFO] Original serial: $ORIG_SERIAL"
    echo "[NOTE] Write this down if you want to re-enroll later or it should be on the bottom of your CB."

    echo
    echo "Choose an option:"
    echo "1) Randomize or input new serial"
    echo "2) Restore original serial"
    read -p "Enter 1 or 2: " CHOICE < /dev/tty

    if [ "$CHOICE" = "1" ]; then
        read -p "Do you want to enter a custom serial number? (Y/N): " CUSTOM < /dev/tty
        if [[ "$CUSTOM" =~ ^[Yy]$ ]]; then
            read -p "Enter the serial number you want to use: " NEW_SERIAL < /dev/tty
        else
            SERIAL_LEN=${#ORIG_SERIAL}
            NEW_SERIAL=$(tr -dc 'A-Z0-9' </dev/urandom | head -c"$SERIAL_LEN" || true)
            echo "[GENERATE] Generated random serial: $NEW_SERIAL"
        fi

        WP_STATE=$(check_wp)
        if [ "$WP_STATE" = "disabled" ]; then
            vpd -s serial_number="$NEW_SERIAL"
            echo "[OK] Serial number set to: $NEW_SERIAL"
        else
            echo "[!] Write protection is enabled — cannot change serial number."
        fi

    elif [ "$CHOICE" = "2" ]; then
        read -p "Enter the original serial you wrote down: " INPUT_SERIAL < /dev/tty
        echo "[RESTORE] Attempting to restore serial number: $INPUT_SERIAL"
        WP_STATE=$(check_wp)
        if [ "$WP_STATE" = "disabled" ]; then
            vpd -s serial_number="$INPUT_SERIAL"
            echo "[OK] Serial number restored successfully."
        else
            echo "[!] Write protection is enabled — cannot restore serial number."
        fi

    else
        echo "[!] Invalid choice, exiting manage_serial."
    fi
}

restore_serial() {
    read -p "Enter the original serial you wrote down: " INPUT_SERIAL < /dev/tty
    echo "[RESTORE] Attempting to restore serial number: $INPUT_SERIAL"
    WP_STATE=$(check_wp)
    if [ "$WP_STATE" = "disabled" ]; then
        vpd -s serial_number="$INPUT_SERIAL"
        echo "[OK] Serial number restored successfully."
    else
        echo "[!] Write protection is enabled — cannot restore serial number."
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
    device_management_client --action=remove_firmware_management_parameters
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
    device_management_client --action=remove_firmware_management_parameters
    manage_serial
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
    manage_serial
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

echo "[*] Completed for milestone r$REL in $( [[ "$VMODE" =~ ^[Yy]$ ]] && echo 'Verified' || echo 'Developer' ) mode."
