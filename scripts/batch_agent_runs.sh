#!/usr/bin/env bash
# Batch agent trajectory runner: 10 FreeCAD + 10 Blender goals, sequentially.
# Per-run trajectory + video + frames written under $OUT_ROOT/<run_name>/.
# Top-level summary CSV at $OUT_ROOT/batch_summary.csv.

set -u
set -o pipefail

OUT_ROOT="${OUT_ROOT:-/home/ubuntu/cua_agent_runs/batch_$(date +%Y%m%d_%H%M%S)}"
MAX_STEPS="${MAX_STEPS:-30}"
PROVIDERS=(--provider DeepInfra --provider Ambient --ignore-provider Novita)

REPO="/home/ubuntu/dev/init_envs"
UV="/home/ubuntu/.local/bin/uv"
DISPLAY_NUM=":99"
FREECAD_SHOTS="/home/ubuntu/cua_gui_smoketest/screenshots"
BLENDER_SHOTS="/home/ubuntu/cua_blender_smoketest/screenshots"

mkdir -p "$OUT_ROOT"
SUMMARY="$OUT_ROOT/batch_summary.csv"
echo "app,run_name,goal,terminated_by,steps,wall_time_s,success,error_brief" > "$SUMMARY"

# Each entry: app | run_name | goal_screenshot_basename
RUNS=(
  # ---- 10 FreeCAD ----
  "freecad|fc_cylinder|A_22_loaded_cylinder_step.png"
  "freecad|fc_showerpad|A_14_loaded_architectural-parts-hydro-equipment__showerpad1x1m_step.png"
  "freecad|fc_simple_door|A_06_loaded_architectural-parts-doors-generic__simple-door_step.png"
  "freecad|fc_hea_beam|A_03_loaded_architectural-parts-beams__profile-hea_fcstd.png"
  "freecad|fc_bracket|A_20_loaded_bracket_step.png"
  "freecad|fc_concrete_block|A_04_loaded_architectural-parts-construction-blocks__concrete-canal-block-14x39x19_step.png"
  "freecad|fc_glass_door|A_08_loaded_architectural-parts-doors-glass__simple-glass-door-with-handles_step.png"
  "freecad|fc_light_pendant|A_17_loaded_architectural-parts-lighting__fcbl_light_pendant_tm_fcstd.png"
  "freecad|fc_aircon|A_24_loaded_architectural-parts-electric-equipment__air-conditionning_stp.png"
  "freecad|fc_faucet|A_11_loaded_architectural-parts-hydro-equipment-faucets-faucet_lorenzetti__faucet_lorenzetti_banheiro_fcstd.png"

  # ---- 10 Blender ----
  "blender|bl_sphere_ring|A_11_loaded_10_sphere_ring.png"
  "blender|bl_pyramid_stack|A_12_loaded_11_pyramid_stack.png"
  "blender|bl_torus_array|A_14_loaded_13_torus_array.png"
  "blender|bl_beveled_cube|A_15_loaded_14_beveled_cube.png"
  "blender|bl_subdiv_sphere|A_16_loaded_15_subdivided_sphere.png"
  "blender|bl_screw_spring|A_18_loaded_17_screw_spring.png"
  "blender|bl_landscape|B_16_loaded_21_landscape.png"
  "blender|bl_stairs|B_18_loaded_23_stairs.png"
  "blender|bl_arch|B_19_loaded_24_arch.png"
  "blender|bl_lattice_cubes|B_20_loaded_25_lattice_cubes.png"
)

run_one() {
  local app="$1" name="$2" shot_base="$3"
  local shots_dir
  local script_name
  case "$app" in
    freecad) shots_dir="$FREECAD_SHOTS"; script_name="agent_trajectory.py" ;;
    blender) shots_dir="$BLENDER_SHOTS"; script_name="blender_agent_trajectory.py" ;;
    *) echo "unknown app $app"; return 2 ;;
  esac

  local out="$OUT_ROOT/$name"
  mkdir -p "$out"
  cp "$shots_dir/$shot_base" "$out/goal.png"

  echo
  echo "===== [$(date '+%H:%M:%S')] $app/$name ====="
  echo "  goal: $shot_base"
  local t0=$(date +%s)

  # Pre-kill any blender/freecad. Use exact name to avoid self-kill of our wrapper.
  pgrep -x blender 2>/dev/null | xargs -r kill -9 2>/dev/null || true
  pgrep -x freecad 2>/dev/null | xargs -r kill -9 2>/dev/null || true
  pgrep -x FreeCAD 2>/dev/null | xargs -r kill -9 2>/dev/null || true
  sleep 2

  DISPLAY="$DISPLAY_NUM" "$UV" run python "$REPO/scripts/$script_name" \
      --goal "$out/goal.png" \
      --output-dir "$out" \
      --max-steps "$MAX_STEPS" \
      "${PROVIDERS[@]}" \
      > "$out/run.log" 2>&1 &
  local pid=$!
  wait "$pid"
  local rc=$?
  local t1=$(date +%s)
  local wall=$((t1 - t0))

  # Extract summary fields from trajectory.json (if it exists).
  local term="error" steps=0 success="false" err=""
  if [ -f "$out/trajectory.json" ]; then
    term=$(python3 -c "import json; d=json.load(open('$out/trajectory.json')); print(d['terminated_by'])" 2>/dev/null || echo "error")
    steps=$(python3 -c "import json; d=json.load(open('$out/trajectory.json')); print(len(d['trajectory']))" 2>/dev/null || echo 0)
    err=$(python3 -c "import json; d=json.load(open('$out/trajectory.json')); print((d.get('error') or '')[:140].replace(',', ';'))" 2>/dev/null || echo "")
  fi
  case "$term" in agent) success="true" ;; esac
  echo "  -> terminated_by=$term  steps=$steps  wall=${wall}s  rc=$rc"
  echo "$app,$name,$shot_base,$term,$steps,$wall,$success,\"$err\"" >> "$SUMMARY"
}

for entry in "${RUNS[@]}"; do
  IFS='|' read -r app name shot <<<"$entry"
  run_one "$app" "$name" "$shot" || true
done

echo
echo "===== BATCH DONE ====="
echo "Summary CSV: $SUMMARY"
echo
column -s, -t < "$SUMMARY" || cat "$SUMMARY"
