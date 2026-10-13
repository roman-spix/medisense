#!/usr/bin/env bash
# Build RuView's ESP32-S3 CSI firmware the way MediSense needs it.
#
# Runs inside the official espressif/idf:v5.4 container (see
# .github/workflows/firmware.yml); Docker is not available on macOS 11, so the
# firmware is built in GitHub's cloud and the resulting files are committed to
# this repository for the app to flash.
#
# Usage: build.sh <variant> <ruview-checkout> <output-dir>
#   variant: s3-8mb  ESP32-S3 boards with 8 MB or more flash (DevKitC-1 N8R8, N16R8, ...)
#            s3-4mb  ESP32-S3 boards with 4 MB flash
set -euo pipefail

VARIANT="${1:?usage: build.sh <variant> <ruview-checkout> <output-dir>}"
SRC="$(cd "${2:?ruview checkout}" && pwd)"
OUT="${3:?output dir}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$SRC/firmware/esp32-csi-node"

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
exec > >(tee "$OUT/build.log") 2>&1

echo "== MediSense firmware build: $VARIANT"
RUVIEW_COMMIT="$(git -C "$SRC" rev-parse HEAD 2>/dev/null || echo unknown)"
echo "RuView commit: $RUVIEW_COMMIT"

# shellcheck disable=SC1091
. "$IDF_PATH/export.sh" >/dev/null
IDF_VERSION="$(idf.py --version)"
echo "ESP-IDF: $IDF_VERSION"

case "$VARIANT" in
  s3-8mb)
    DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.devkitc;sdkconfig.defaults.medisense"
    FLASH_SIZE="8MB"
    PARTITIONS="partitions_display.csv"
    ;;
  s3-4mb)
    DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.4mb;sdkconfig.defaults.medisense"
    FLASH_SIZE="4MB"
    PARTITIONS="partitions_4mb.csv"
    ;;
  *)
    echo "unknown variant: $VARIANT" >&2
    exit 2
    ;;
esac

cd "$PROJ"
PATCHES=()
for p in "$HERE"/patches/*.patch; do
  echo "-- applying $(basename "$p")"
  git -C "$SRC" apply --verbose "$p"
  PATCHES+=("$(basename "$p")")
done
cp "$HERE/sdkconfig.defaults.medisense" ./sdkconfig.defaults.medisense

# WASM tier 3 is compiled out (CONFIG_WASM_ENABLE=n). Removing the optional
# component also stops its CMake script from downloading third-party sources.
rm -rf components/wasm3

# Pin managed-component versions once a known-good lock exists.
if [ -f "$HERE/dependencies.lock.$VARIANT" ]; then
  echo "-- using pinned dependencies.lock.$VARIANT"
  cp "$HERE/dependencies.lock.$VARIANT" dependencies.lock
fi

rm -rf build sdkconfig
idf.py -DSDKCONFIG_DEFAULTS="$DEFAULTS" set-target esp32s3
idf.py -DSDKCONFIG_DEFAULTS="$DEFAULTS" build

echo "-- checking the resulting configuration"
check() {
  if grep -qxF -- "$1" sdkconfig; then
    echo "ok: $1"
  else
    echo "FAILED: expected '$1' in sdkconfig" >&2
    exit 1
  fi
}
check "# CONFIG_DISPLAY_ENABLE is not set"
check "# CONFIG_LED_GAMMA_VIZ is not set"
check "# CONFIG_WASM_ENABLE is not set"
check "CONFIG_ESP_WIFI_CSI_ENABLED=y"
check "CONFIG_ESPTOOLPY_FLASHSIZE=\"$FLASH_SIZE\""
check "CONFIG_PARTITION_TABLE_CUSTOM_FILENAME=\"$PARTITIONS\""
check "CONFIG_LWIP_UDP_RECVMBOX_SIZE=32"
check "CONFIG_ESP_WIFI_DYNAMIC_TX_BUFFER_NUM=64"
check "CONFIG_FREERTOS_TIMER_TASK_STACK_DEPTH=8192"

cp build/bootloader/bootloader.bin \
   build/partition_table/partition-table.bin \
   build/ota_data_initial.bin \
   build/esp32-csi-node.bin \
   build/flasher_args.json \
   "$OUT/"
cp sdkconfig "$OUT/sdkconfig.txt"
if [ -f dependencies.lock ]; then
  cp dependencies.lock "$OUT/dependencies.lock"
fi
python -m esptool --chip esp32s3 image_info --version 2 build/esp32-csi-node.bin \
  > "$OUT/image_info.txt" 2>&1 || echo "(image_info unavailable)" > "$OUT/image_info.txt"

(cd "$OUT" && sha256sum ./*.bin | sed 's# \./# #' > SHA256SUMS)
APP_SIZE="$(stat -c%s build/esp32-csi-node.bin)"
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
PATCH_JSON="$(printf '"%s",' "${PATCHES[@]}")"
cat > "$OUT/BUILDINFO.json" <<EOF
{
  "variant": "$VARIANT",
  "chip": "esp32s3",
  "flash_size": "$FLASH_SIZE",
  "partition_table": "$PARTITIONS",
  "firmware_version": "$(tr -d '[:space:]' < version.txt 2>/dev/null || echo unknown)",
  "ruview_commit": "$RUVIEW_COMMIT",
  "esp_idf": "$IDF_VERSION",
  "sdkconfig_defaults": "$DEFAULTS",
  "patches": [${PATCH_JSON%,}],
  "app_size_bytes": $APP_SIZE,
  "built_at": "$BUILT_AT"
}
EOF
echo "== done: $VARIANT, application image $APP_SIZE bytes"
