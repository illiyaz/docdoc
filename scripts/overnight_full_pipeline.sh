#!/bin/bash
# Full pipeline comparison: run end-to-end for each model on key files
# Pulls models, swaps OLLAMA_MODEL, submits via API, auto-approves, compares output
#
# Usage: ./scripts/overnight_full_pipeline.sh
# Output: output/overnight_report.txt

set -e
cd /Users/LENOVO/Documents/Projects/DocDoc

PYTHON=/opt/miniconda3/bin/python
REPORT=output/overnight_report.txt
RESULTS_DIR=output/overnight_full

mkdir -p output "$RESULTS_DIR"

API="http://localhost:8000/api"

echo "============================================" | tee "$REPORT"
echo "OVERNIGHT FULL PIPELINE COMPARISON" | tee -a "$REPORT"
echo "Started: $(date)" | tee -a "$REPORT"
echo "============================================" | tee -a "$REPORT"

# --- Pull models ---
echo "Pulling qwen2.5:14b..." | tee -a "$REPORT"
ollama pull qwen2.5:14b 2>&1 | tail -3 | tee -a "$REPORT"
echo "Pulling qwen2.5:32b..." | tee -a "$REPORT"
ollama pull qwen2.5:32b 2>&1 | tail -3 | tee -a "$REPORT"

# --- Test files ---
TEST_FILES=(
    "docs/testingsamples/phase2_100pg/3666752.pdf"
    "docs/testingsamples/phase2_100pg/3733050.pdf"
    "docs/testingsamples/phase2_100pg/3738594.pdf"
    "docs/testingsamples/phase2_100pg/3738641.pdf"
    "docs/testingsamples/phase2_100pg/Complex1.pdf"
    "docs/testingsamples/phase2_100pg/TPHS2_656_0000067171.pdf"
    "docs/testingsamples/phase2_100pg/CMG_Inc_0001352703.pdf"
    "docs/testingsamples/phase2_100pg/WashingtonCMD_0000102080.pdf"
)

MODELS=("qwen2.5:7b" "qwen2.5:14b" "qwen2.5:32b" "llama3:8b")

# --- Run full pipeline for each model × file ---
for model in "${MODELS[@]}"; do
    echo "" | tee -a "$REPORT"
    echo "============================================" | tee -a "$REPORT"
    echo "MODEL: $model" | tee -a "$REPORT"
    echo "============================================" | tee -a "$REPORT"

    for pdf in "${TEST_FILES[@]}"; do
        fname=$(basename "$pdf")
        echo "" | tee -a "$REPORT"
        echo "--- $fname with $model ---" | tee -a "$REPORT"
        start_time=$(date +%s)

        # Run the full pipeline test
        $PYTHON scripts/run_full_pipeline_test.py \
            --pdf "$pdf" \
            --model "$model" \
            --output "$RESULTS_DIR/${fname%.pdf}_${model//[:.]/_}.json" \
            2>&1 | tee -a "$REPORT" || echo "  FAILED" | tee -a "$REPORT"

        end_time=$(date +%s)
        elapsed=$((end_time - start_time))
        echo "  Time: ${elapsed}s" | tee -a "$REPORT"
    done
done

# --- Consolidate ---
echo "" | tee -a "$REPORT"
echo "============================================" | tee -a "$REPORT"
echo "CONSOLIDATION" | tee -a "$REPORT"
echo "============================================" | tee -a "$REPORT"

$PYTHON -c "
import json, glob, os

results = {}
for f in sorted(glob.glob('$RESULTS_DIR/*.json')):
    name = os.path.basename(f).replace('.json', '')
    try:
        with open(f) as fh:
            results[name] = json.load(fh)
    except:
        pass

# Leaderboard
models = {}
for name, data in results.items():
    m = data.get('model', 'unknown')
    if m not in models:
        models[m] = {'files': 0, 'subjects': 0, 'with_address': 0, 'time': 0}
    models[m]['files'] += 1
    models[m]['subjects'] += data.get('subjects', 0)
    models[m]['with_address'] += data.get('subjects_with_address', 0)
    models[m]['time'] += data.get('total_time', 0)

print()
print('╔═══════════════════════════════════════════════════════════╗')
print('║              FULL PIPELINE LEADERBOARD                   ║')
print('╠═══════════════════════════════════════════════════════════╣')
print(f'║ {\"Model\":<20} {\"Files\":>5} {\"Subjects\":>8} {\"W/Addr\":>7} {\"Time\":>8} ║')
print('╠═══════════════════════════════════════════════════════════╣')
for m in sorted(models, key=lambda x: -models[x]['subjects']):
    s = models[m]
    print(f'║ {m:<20} {s[\"files\"]:>5} {s[\"subjects\"]:>8} {s[\"with_address\"]:>7} {s[\"time\"]:>7.0f}s ║')
print('╚═══════════════════════════════════════════════════════════╝')

# Per-file detail
print()
for name in sorted(results):
    d = results[name]
    print(f'  {name}: {d.get(\"subjects\",0)} subjects, {d.get(\"subjects_with_address\",0)} with addr, {d.get(\"total_time\",0):.0f}s')

with open('$RESULTS_DIR/consolidated.json', 'w') as f:
    json.dump(results, f, indent=2)
" 2>&1 | tee -a "$REPORT"

echo "" | tee -a "$REPORT"
echo "COMPLETED: $(date)" | tee -a "$REPORT"
