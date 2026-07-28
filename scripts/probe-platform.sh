#!/usr/bin/env bash
set -euo pipefail

printf '=== platform ===\n'
uname -a
printf '\n=== model ===\n'
for f in /proc/device-tree/model /sys/firmware/devicetree/base/model; do
  if [[ -r "$f" ]]; then tr -d '\0' < "$f"; echo; break; fi
done

printf '\n=== USB Device Controllers ===\n'
if [[ -d /sys/class/udc ]]; then
  ls -la /sys/class/udc
  for u in /sys/class/udc/*; do
    [[ -e "$u" ]] || continue
    echo "--- $(basename "$u") ---"
    [[ -r "$u/state" ]] && { printf 'state: '; cat "$u/state"; }
    [[ -r "$u/uevent" ]] && cat "$u/uevent"
  done
else
  echo '/sys/class/udc does not exist'
fi

printf '\n=== USB role switches ===\n'
find /sys/class/usb_role -maxdepth 2 -type f -name role -print -exec cat {} \; 2>/dev/null || true

printf '\n=== gadget/configfs/functionfs/raw-gadget kernel config ===\n'
config=''
if [[ -r /proc/config.gz ]]; then
  zcat /proc/config.gz | grep -E 'CONFIG_USB_(GADGET|CONFIGFS|CONFIGFS_F_FS|FUNCTIONFS|RAW_GADGET)|CONFIG_USB_DWC2|CONFIG_USB_DWC3' || true
elif [[ -r "/boot/config-$(uname -r)" ]]; then
  grep -E 'CONFIG_USB_(GADGET|CONFIGFS|CONFIGFS_F_FS|FUNCTIONFS|RAW_GADGET)|CONFIG_USB_DWC2|CONFIG_USB_DWC3' "/boot/config-$(uname -r)" || true
else
  echo 'No readable kernel config found.'
fi

printf '\n=== configfs and existing gadget owners ===\n'
mount | grep -E 'configfs|functionfs' || true
ls -ld /sys/kernel/config/usb_gadget 2>/dev/null || true
for f in /sys/kernel/config/usb_gadget/*/UDC; do
  [[ -e "$f" ]] || continue
  printf '%s: ' "$f"
  cat "$f"
done
systemctl list-units --type=service --all 2>/dev/null | grep -Ei 'usb.*(gadget|device)|gadget|l4t.*usb' || true

printf '\n=== raw gadget ===\n'
ls -l /dev/raw-gadget 2>/dev/null || echo '/dev/raw-gadget absent'

printf '\n=== verdict ===\n'
if compgen -G '/sys/class/udc/*' > /dev/null; then
  echo 'UDC_PRESENT: hardware/kernel can potentially act as a USB peripheral.'
else
  echo 'NO_UDC: this OS/kernel currently cannot present the board as a USB peripheral.'
fi
