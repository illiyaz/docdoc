#!/bin/bash
# Overnight model comparison test
# Pulls qwen2.5:14b and qwen2.5:32b, tests extraction on ALL PDFs
#
# Usage: ./scripts/overnight_model_test.sh
# Output: output/overnight_report.txt + output/overnight_models/

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
ollama pull qwen2.5:14b 2>&1 | tail -5 | tee -a "$REPORT"

echo "" | tee -a "$REPORT"
echo "--- Pulling qwen2.5:32b ---" | tee -a "$REPORT"
ollama pull qwen2.5:32b 2>&1 | tail -5 | tee -a "$REPORT"

echo "" | tee -a "$REPORT"
echo "Models available:" | tee -a "$REPORT"
curl -s http://localhost:11434/api/tags | python3 -c "
import json,sys
d = json.load(sys.stdin)
for m in d.get('models',[]):
    if 'qwen2.5' in m['name'] and 'vl' not in m['name']:
        print(f'  {m[\"name\"]}: {m[\"size\"]/(1024**3):.1f}GB')
" 2>&1 | tee -a "$REPORT"

# --- Collect all test PDFs ---
MODELS="qwen2.5:7b,qwen2.5:14b,qwen2.5:32b"
PDF_DIRS=(
    "docs/testingsamples/phase2_large_pdfs_mini"
    "docs/testingsamples/taxonomy_samples"
)

PDFS=()
for dir in "${PDF_DIRS[@]}"; do
    if [ -d "$dir" ]; then
        for f in "$dir"/*.pdf; do
            [ -f "$f" ] && PDFS+=("$f")
        done
    fi
done

echo "" | tee -a "$REPORT"
echo "============================================" | tee -a "$REPORT"
echo "TESTING ${#PDFS[@]} PDFs WITH $MODELS" | tee -a "$REPORT"
echo "============================================" | tee -a "$REPORT"

# --- Test each PDF ---
count=0
for pdf in "${PDFS[@]}"; do
    count=$((count + 1))
    fname=$(basename "$pdf")
    echo "" | tee -a "$REPORT"
    echo "[$count/${#PDFS[@]}] $fname" | tee -a "$REPORT"
    echo "─────────────────────────────────────" | tee -a "$REPORT"

    $PYTHON scripts/test_extraction_models.py \
        --models "$MODELS" \
        --pages 5 \
        --pdf "$pdf" \
        2>&1 | tee -a "$REPORT" || echo "  FAILED: $fname" | tee -a "$REPORT"

    # Save per-file results
    if [ -f "output/extraction_model_comparison.json" ]; then
        mv "output/extraction_model_comparison.json" \
           "$RESULTS_DIR/${fname%.pdf}_results.json"
    fi
done

# --- 15-page deep test on school reports ---
echo "" | tee -a "$REPORT"
echo "============================================" | tee -a "$REPORT"
echo "DEEP TEST: 15 pages on school reports" | tee -a "$REPORT"
echo "============================================" | tee -a "$REPORT"

for pdf in docs/testingsamples/phase2_large_pdfs_mini/3733050.pdf \
           docs/testingsamples/phase2_large_pdfs_mini/3738594.pdf \
           docs/testingsamples/phase2_large_pdfs_mini/3738641.pdf; do
    if [ ! -f "$pdf" ]; then continue; fi
    fname=$(basename "$pdf")
    for model in qwen2.5:7b qwen2.5:14b qwen2.5:32b; do
        echo "" | tee -a "$REPORT"
        echo "--- $model on $fname (15 pages) ---" | tee -a "$REPORT"
        $PYTHON scripts/test_extraction_models.py \
            --models "$model" \
            --pages 15 \
            --pdf "$pdf" \
            2>&1 | tee -a "$REPORT" || echo "  FAILED" | tee -a "$REPORT"

        if [ -f "output/extraction_model_comparison.json" ]; then
            safe_model=$(echo "$model" | tr ':.' '_')
            mv "output/extraction_model_comparison.json" \
               "$RESULTS_DIR/${fname%.pdf}_15pg_${safe_model}.json"
        fi
    done
done

# --- Consolidate results + leaderboard ---
echo "" | tee -a "$REPORT"
echo "============================================" | tee -a "$REPORT"
echo "CONSOLIDATION" | tee -a "$REPORT"
echo "============================================" | tee -a "$REPORT"

$PYTHON -c "
import json, glob, os

results = {}
for f in sorted(glob.glob('$RESULTS_DIR/*.json')):
    if 'consolidated' in f:
        continue
    name = os.path.basename(f).replace('_results.json', '').replace('.json', '')
    try:
        with open(f) as fh:
            results[name] = json.load(fh)
    except:
        pass

# Build leaderboard
models = {}
for test_name, data in results.items():
    for r in data.get('results', []):
        m = r.get('model', 'unknown')
        if m not in models:
            models[m] = {'tests': 0, 'correct_student': 0, 'correct_address': 0,
                         'total_pages': 0, 'wrong': 0, 'missing': 0, 'time': 0, 'errors': 0}
        models[m]['tests'] += 1
        models[m]['correct_student'] += r.get('correct_student', 0)
        models[m]['correct_address'] += r.get('correct_address', 0)
        models[m]['total_pages'] += r.get('total_pages', 0)
        models[m]['wrong'] += r.get('wrong_student', 0)
        models[m]['missing'] += r.get('missing', 0)
        t = r.get('time', '0s')
        models[m]['time'] += float(str(t).replace('s',''))
        if r.get('error'):
            models[m]['errors'] += 1

print()
print('╔══════════════════════════════════════════════════════════════════╗')
print('║                    MODEL LEADERBOARD                           ║')
print('╠══════════════════════════════════════════════════════════════════╣')
print(f'║ {\"Model\":<20} {\"Student%\":>9} {\"Address%\":>9} {\"Wrong\":>6} {\"Miss\":>6} {\"Time\":>7} ║')
print('╠══════════════════════════════════════════════════════════════════╣')
for m in sorted(models, key=lambda x: -models[x]['correct_student']):
    s = models[m]
    s_acc = 100*s['correct_student']/s['total_pages'] if s['total_pages'] else 0
    a_acc = 100*s['correct_address']/s['total_pages'] if s['total_pages'] else 0
    print(f'║ {m:<20} {s_acc:8.1f}% {a_acc:8.1f}% {s[\"wrong\"]:>6} {s[\"missing\"]:>6} {s[\"time\"]:>6.0f}s ║')
print('╚══════════════════════════════════════════════════════════════════╝')
print(f'Tests: {sum(s[\"tests\"] for s in models.values())} across {len(results)} files')

with open('$RESULTS_DIR/consolidated.json', 'w') as f:
    json.dump({'leaderboard': models, 'tests': results}, f, indent=2)
print(f'Saved: $RESULTS_DIR/consolidated.json')
" 2>&1 | tee -a "$REPORT"

echo "" | tee -a "$REPORT"
echo "============================================" | tee -a "$REPORT"
echo "COMPLETED: $(date)" | tee -a "$REPORT"
echo "============================================" | tee -a "$REPORT"
