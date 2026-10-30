#!/usr/bin/env bash
set -euo pipefail

SAMPLE_STL="${1:-/Volumes/STL/Loot Studios/2020-08 - Crazed Mage/Chimera_V2/32mm/No Supports/32mm_Chimer_FullBody_V2.stl}"
OUT_DIR="/tmp/desync_eval"
BLENDER_BIN="/Applications/Blender.app/Contents/MacOS/Blender"
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/render_stl_eval.py"

if [ ! -x "$BLENDER_BIN" ]; then
    BLENDER_BIN=$(which blender || true)
fi

if [ -z "$BLENDER_BIN" ] || [ ! -x "$BLENDER_BIN" ]; then
    echo "[-] Blender binary not found at /Applications/Blender.app or on PATH."
    exit 1
fi

if [ ! -f "$SAMPLE_STL" ]; then
    echo "[-] Sample STL not found at: $SAMPLE_STL"
    echo "[-] Pass path to a valid STL: ./run_eval.sh /path/to/file.stl"
    exit 1
fi

mkdir -p "$OUT_DIR"
echo "[*] Using Blender: $BLENDER_BIN"
echo "[*] Rendering target: $(basename "$SAMPLE_STL")"

# 1. 3/4 Isometric Perspective (Standard Catalog Card View)
echo "[-] Generating Angle 1: 3/4 Isometric (Azimuth 35°, Elevation 18°)..."
"$BLENDER_BIN" -b -P "$SCRIPT_PATH" -- \
    --input "$SAMPLE_STL" \
    --output "$OUT_DIR/01_isometric_35deg.jpg" \
    --up-axis Z --azimuth 35 --elevation 18

# 2. Front Eye-Level View (Direct Silhouette Check)
echo "[-] Generating Angle 2: Front Elevation (Azimuth 0°, Elevation 5°)..."
"$BLENDER_BIN" -b -P "$SCRIPT_PATH" -- \
    --input "$SAMPLE_STL" \
    --output "$OUT_DIR/02_front_profile.jpg" \
    --up-axis Z --azimuth 0 --elevation 5

# 3. High-Angle Three-Quarter (Top-Down Tabletop View)
echo "[-] Generating Angle 3: Elevated Action (Azimuth 45°, Elevation 35°)..."
"$BLENDER_BIN" -b -P "$SCRIPT_PATH" -- \
    --input "$SAMPLE_STL" \
    --output "$OUT_DIR/03_elevated_action.jpg" \
    --up-axis Z --azimuth 45 --elevation 35

echo -e "\n[+] R&D Render Evaluation complete! Outputs written to: $OUT_DIR"
open "$OUT_DIR"
