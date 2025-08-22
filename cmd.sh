#!/bin/bash
# ChromeOS Enrollment Avoidance Script
# Fully debugged with serial backup, restore, and random/manual serial option

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
# Serial number management
# --------------------------

manage_serial() {
    ORIG_SERIAL=$(vpd -g serial_number)
    echo "[INFO] Original serial: $ORIG_SERIAL"
    echo "[NOTE] Write this down if you want to re-enroll later or it should be on the bottom of your cb."

    echo
    echo "Choose an option:"
    echo "1) Randomize or input new serial"
    echo "2) Restore original serial"
    read -p "Enter 1 or 2: " CHOICE

    if [ "$CHOICE" = "1" ]; then
        # Ask user whether to input a custom serial or randomize
        read -p "Do you want to enter a custom serial number? (y/N): " CUSTOM
        if [[ "$CUSTOM" =~ ^[Yy]$ ]]; then
            read -p "Enter the serial number you want to use: " NEW_SERIAL
        else
            NEW_SERIAL="RAND-$(cat /dev/urandom | tr -dc 'A-Z0-9' | head -c12)"
            echo "[GENERATE] Generated random serial: $NEW_SERIAL"
        fi

        # Apply serial only if WP is disabled
        if [ "$(crossystem wpsw_cur 2>/dev/null)" = "0" ]; then
            vpd -s serial_number="$NEW_SERIAL"
            echo "[OK] Serial number set to: $NEW_SERIAL"
        else
            echo "[!] Write protection is enabled — cannot change serial number."
        fi

    elif [ "$CHOICE" = "2" ]; then
        # Prompt for manually-entered original serial
        read -p "Enter the original serial you wrote down: " INPUT_SERIAL
        echo "[RESTORE] Attempting to restore serial number: $INPUT_SERIAL"
        if [ "$(crossystem wpsw_cur 2>/dev/null)" = "0" ]; then
            vpd -s serial_number="$INPUT_SERIAL"
            echo "[OK] Serial number restored successfully."
        else
            echo "[!] Write protection is still enabled — cannot restore serial number."
        fi

    else
        echo "[!] Invalid choice, exiting manage_serial."
    fi
}

# Restore original serial directly (manual input)
restore_serial() {
    read -p "Enter the original serial you wrote down: " INPUT_SERIAL
    echo "[RESTORE] Attempting to restore serial number: $INPUT_SERIAL"
    if [ "$(crossystem wpsw_cur 2>/dev/null)" = "0" ]; then
        vpd -s serial_number="$INPUT_SERIAL"
        echo "[OK] Serial number restored successfully."
    else
        echo "[!] Write protection is still enabled — cannot restore serial number."
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
