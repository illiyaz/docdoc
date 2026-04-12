#!/bin/bash
# Overnight model comparison test
# Pulls qwen2.5:14b and qwen2.5:32b, then tests extraction on all PDFs
#
# Usage: ./scripts/overnight_model_test.sh
# Output: output/overnight_report.txt + output/overnight_results.json

set -e
cd /Users/LENOVO/Documents/Projects/DocDoc

PYTHON=/opt/miniconda3/bin/python
REPORT=output/overnight_report.txt
RESULTS_DIR=output/overnight_models

mkdir -p output "$RESULTS_DIR"

echo "============================================" | tee "$REPORT"
echo "OVERNIGHT MODEL COMPARISON TEST" | tee -a "$REPORT"
echo "Started: $(date)" | tee -a "$REPORT"
echo "============================================" | tee -a "$REPORT"

# --- Pull models ---
echo "" | tee -a "$REPORT"
echo "--- Pulling qwen2.5:14b ---" | tee -a "$REPORT"
ollama pull qwen2.5:14b 2>&1 | tail -3 | tee -a "$REPORT"

echo "" | tee -a "$REPORT"
echo "--- Pulling qwen2.5:32b ---" | tee -a "$REPORT"
ollama pull qwen2.5:32b 2>&1 | tail -3 | tee -a "$REPORT"

echo "" | tee -a "$REPORT"
echo "Models available:" | tee -a "$REPORT"
curl -s http://localhost:11434/api/tags | python3 -c "
import json,sys
d = json.load(sys.stdin)
for m in d.get('models',[]):
    if 'qwen' in m['name'].lower() and 'vl' not in m['name'].lower():
        print(f'  {m[\"name\"]}: {m[\"size\"]/(1024**3):.1f}GB')
" 2>&1 | tee -a "$REPORT"

# --- Test PDFs ---
PDF_DIR="docs/testingsamples/phase2_large_pdfs_mini"
MODELS="qwen2.5:7b,qwen2.5:14b,qwen2.5:32b"

echo "" | tee -a "$REPORT"
echo "============================================" | tee -a "$REPORT"
echo "EXTRACTION ACCURACY TEST" | tee -a "$REPORT"
echo "Models: $MODELS" | tee -a "$REPORT"
echo "============================================" | tee -a "$REPORT"

# Test on each PDF with 5 pages
for pdf in "$PDF_DIR"/*.pdf; do
    fname=$(basename "$pdf")
    echo "" | tee -a "$REPORT"
    echo "--- $fname ---" | tee -a "$REPORT"

    # Copy to uploads dir for the test script
    UPLOAD_DIR="uploads/overnight-test"
    mkdir -p "$UPLOAD_DIR"
    cp "$pdf" "$UPLOAD_DIR/"

    $PYTHON scripts/test_extraction_models.py \
        --models "$MODELS" \
        --pages 5 \
        --pdf "$pdf" \
        2>&1 | tee -a "$REPORT"

    # Move results
    if [ -f "output/extraction_model_comparison.json" ]; then
        mv "output/extraction_model_comparison.json" "$RESULTS_DIR/${fname%.pdf}_results.json"
    fi
done

# --- Full pipeline test with best model ---
echo "" | tee -a "$REPORT"
echo "============================================" | tee -a "$REPORT"
echo "FULL 225-PAGE TEST (Meadowdale)" | tee -a "$REPORT"
echo "============================================" | tee -a "$REPORT"

# Test each model on 15 pages of the Meadowdale report
for model in qwen2.5:7b qwen2.5:14b qwen2.5:32b; do
    echo "" | tee -a "$REPORT"
    echo "--- $model on 15 pages ---" | tee -a "$REPORT"
    $PYTHON scripts/test_extraction_models.py \
        --models "$model" \
        --pages 15 \
        --pdf "docs/testingsamples/phase2_large_pdfs_mini/3733050.pdf" \
        2>&1 | tee -a "$REPORT"

    if [ -f "output/extraction_model_comparison.json" ]; then
        mv "output/extraction_model_comparison.json" "$RESULTS_DIR/meadowdale_15pg_${model//[:.]/_}.json"
    fi
done

# --- Summary ---
echo "" | tee -a "$REPORT"
echo "============================================" | tee -a "$REPORT"
echo "COMPLETED: $(date)" | tee -a "$REPORT"
echo "============================================" | tee -a "$REPORT"
echo "" | tee -a "$REPORT"
echo "Results in: $RESULTS_DIR/" | tee -a "$REPORT"
echo "Report: $REPORT" | tee -a "$REPORT"

# Consolidate all results into one JSON
$PYTHON -c "
import json, glob, os
results = {}
for f in sorted(glob.glob('$RESULTS_DIR/*.json')):
    name = os.path.basename(f).replace('_results.json', '').replace('.json', '')
    with open(f) as fh:
        results[name] = json.load(fh)

# Build leaderboard
models = {}
for test_name, data in results.items():
    for r in data.get('results', []):
        m = r.get('model', 'unknown')
        if m not in models:
            models[m] = {'tests': 0, 'correct': 0, 'total': 0, 'time': 0}
        models[m]['tests'] += 1
        models[m]['correct'] += r.get('correct_student', 0)
        models[m]['total'] += r.get('total_pages', 0)
        t = r.get('time', '0s')
        models[m]['time'] += float(t.replace('s','')) if isinstance(t, str) else t

print('\\n=== MODEL LEADERBOARD ===')
for m in sorted(models, key=lambda x: -models[x]['correct']):
    s = models[m]
    acc = 100*s['correct']/s['total'] if s['total'] else 0
    print(f'  {m:<20} {acc:5.1f}% accuracy  {s[\"correct\"]}/{s[\"total\"]} correct  {s[\"time\"]:.0f}s total')

with open('$RESULTS_DIR/consolidated.json', 'w') as f:
    json.dump({'leaderboard': models, 'tests': results}, f, indent=2)
print(f'\\nConsolidated: $RESULTS_DIR/consolidated.json')
" 2>&1 | tee -a "$REPORT"
