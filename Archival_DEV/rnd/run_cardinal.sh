#!/usr/bin/env bash
set -euo pipefail

SAMPLE_STL="${1:-/Volumes/STL/Loot Studios/2020-08 - Crazed Mage/Chimera_V2/32mm/No Supports/32mm_Chimer_FullBody_V2.stl}"
OUT_DIR="/tmp/desync_eval/cardinal"
BLENDER_BIN="/Applications/Blender.app/Contents/MacOS/Blender"
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/render_cardinal_views.py"

if [ ! -x "$BLENDER_BIN" ]; then
    BLENDER_BIN=$(which blender || true)
fi

mkdir -p "$OUT_DIR"
rm -f "$OUT_DIR"/*.jpg
echo "[*] Rendering 6-View Cardinal Suite for $(basename "$SAMPLE_STL")..."

"$BLENDER_BIN" -b -P "$SCRIPT_PATH" -- \
    --input "$SAMPLE_STL" \
    --out-dir "$OUT_DIR" \
    --resolution 800

echo -e "\n[+] Finished rendering all 6 views in: $OUT_DIR"
open "$OUT_DIR"
