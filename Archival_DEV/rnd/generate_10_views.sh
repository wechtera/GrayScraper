cat << 'EOF' > rnd/fine_tune_views.sh
#!/usr/bin/env bash
set -euo pipefail

SAMPLE_STL="${1:-/Volumes/STL/Loot Studios/2020-08 - Crazed Mage/Chimera_V2/32mm/No Supports/32mm_Chimer_FullBody_V2.stl}"
OUT_DIR="/tmp/desync_eval/fine_tune"
BLENDER_BIN="/Applications/Blender.app/Contents/MacOS/Blender"
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/render_stl_eval.py"

if [ ! -x "$BLENDER_BIN" ]; then
    BLENDER_BIN=$(which blender || true)
fi

mkdir -p "$OUT_DIR"
rm -f "$OUT_DIR"/*.jpg
echo "[*] Generating fine-tuned camera variations for $(basename "$SAMPLE_STL")..."

run_view() {
    local num="$1"
    local desc="$2"
    shift 2
    echo "[$num/8] Rendering: $desc"
    "$BLENDER_BIN" -b -P "$SCRIPT_PATH" -- --input "$SAMPLE_STL" "$@" > /dev/null 2>&1
}

# Baseline was: Elevation 70°, Azimuth 20°, Roll -20°

# 1. Gentle height bump + slight right shift
run_view "01" "Elevation 74°, Azimuth 26°, Roll -20°" \
    --output "$OUT_DIR/01_elev74_az26.jpg" --elevation 74 --azimuth 26 --roll -20

# 2. Gentle height bump + moderate right shift
run_view "02" "Elevation 74°, Azimuth 32°, Roll -20°" \
    --output "$OUT_DIR/02_elev74_az32.jpg" --elevation 74 --azimuth 32 --roll -20

# 3. Medium height bump + slight right shift
run_view "03" "Elevation 77°, Azimuth 26°, Roll -20°" \
    --output "$OUT_DIR/03_elev77_az26.jpg" --elevation 77 --azimuth 26 --roll -20

# 4. Medium height bump + moderate right shift
run_view "04" "Elevation 77°, Azimuth 32°, Roll -20°" \
    --output "$OUT_DIR/04_elev77_az32.jpg" --elevation 77 --azimuth 32 --roll -20

# 5. Medium height bump + wide right shift (more flank view)
run_view "05" "Elevation 77°, Azimuth 38°, Roll -20°" \
    --output "$OUT_DIR/05_elev77_az38.jpg" --elevation 77 --azimuth 38 --roll -20

# 6. High height bump + moderate right shift
run_view "06" "Elevation 80°, Azimuth 30°, Roll -20°" \
    --output "$OUT_DIR/06_elev80_az30.jpg" --elevation 80 --azimuth 30 --roll -20

# 7. High height bump + wide right shift
run_view "07" "Elevation 80°, Azimuth 36°, Roll -20°" \
    --output "$OUT_DIR/07_elev80_az36.jpg" --elevation 80 --azimuth 36 --roll -20

# 8. Counter-check: Azimuth 12° (in case orbital perspective was inverted)
run_view "08" "Elevation 75°, Azimuth 12°, Roll -20°" \
    --output "$OUT_DIR/08_elev75_az12_alt.jpg" --elevation 75 --azimuth 12 --roll -20

echo -e "\n[+] Fine-tuned renders complete! Opening: $OUT_DIR"
open "$OUT_DIR"
EOF
chmod +x rnd/fine_tune_views.sh
./rnd/fine_tune_views.sh
