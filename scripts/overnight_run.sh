#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Forentis AI — Overnight Pipeline Orchestrator
# ═══════════════════════════════════════════════════════════════
# Designed to be triggered by a Claude scheduled task.
# Runs the extraction pipeline, captures structured results,
# and writes a report for the next Claude session to analyze.
#
# Usage: bash scripts/overnight_run.sh [phase]
#   phase 1: Run forentis_extract.py on testingsamples/
#   phase 2: Run test_hybrid_pipeline.py (needs Ollama)
#   phase 3: Run pytest suite
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
RESULTS_DIR="$PROJECT_DIR/overnight_results"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
PHASE="${1:-all}"

mkdir -p "$RESULTS_DIR"

log() { echo "[$(date '+%H:%M:%S')] $1" | tee -a "$RESULTS_DIR/run_${TIMESTAMP}.log"; }

# ─── Environment Check ──────────────────────────────────────
check_env() {
    log "=== ENVIRONMENT CHECK ==="

    # Python
    if command -v python3 &>/dev/null; then
        log "✓ Python3: $(python3 --version 2>&1)"
    else
        log "✗ Python3 not found"
        return 1
    fi

    # Key Python packages
    for pkg in fitz openpyxl xlrd docx presidio_analyzer spacy; do
        if python3 -c "import $pkg" 2>/dev/null; then
            log "✓ Python package: $pkg"
        else
            log "⚠ Missing package: $pkg (will degrade gracefully)"
        fi
    done

    # Docker
    if docker ps &>/dev/null; then
        log "✓ Docker running"
        docker ps --format "  {{.Names}}: {{.Status}}" | while read line; do log "  $line"; done
    else
        log "⚠ Docker not running — skipping server-dependent tests"
    fi

    # Ollama
    if curl -s http://localhost:11434/api/tags &>/dev/null; then
        log "✓ Ollama running"
        curl -s http://localhost:11434/api/tags | python3 -c "
import sys,json
d=json.load(sys.stdin)
for m in d.get('models',[]):
    print(f\"  Model: {m['name']}\")" 2>/dev/null | while read line; do log "$line"; done
    else
        log "⚠ Ollama not reachable — Phase 2 will be skipped"
    fi

    log "=== ENV CHECK COMPLETE ==="
}

# ─── Phase 1: Standalone extraction on all test files ────────
phase1_extract() {
    log "=== PHASE 1: STANDALONE EXTRACTION ==="
    local INPUT_DIR="$PROJECT_DIR/docs/testingsamples"
    local OUTPUT="$RESULTS_DIR/phase1_extract_${TIMESTAMP}.json"

    if [ ! -d "$INPUT_DIR" ]; then
        log "✗ Test samples directory not found: $INPUT_DIR"
        return 1
    fi

    local FILE_COUNT=$(find "$INPUT_DIR" -type f \( -name "*.pdf" -o -name "*.xlsx" -o -name "*.xls" -o -name "*.msg" -o -name "*.heic" -o -name "*.jpg" \) | wc -l | tr -d ' ')
    log "Found $FILE_COUNT files to process"

    log "Running forentis_extract.py..."
    cd "$PROJECT_DIR"

    if python3 scripts/forentis_extract.py "$INPUT_DIR" --output "$OUTPUT" 2>&1 | tee -a "$RESULTS_DIR/run_${TIMESTAMP}.log"; then
        log "✓ Phase 1 complete — results at $OUTPUT"

        # Quick summary
        python3 -c "
import json,sys
with open('$OUTPUT') as f: d=json.load(f)
docs=d.get('documents',d.get('results',[]))
total_records=sum(doc.get('record_count',len(doc.get('records',[]))) for doc in docs)
success=sum(1 for doc in docs if doc.get('status','')!='error')
print(f'Documents: {len(docs)} | Success: {success} | Failed: {len(docs)-success} | Records: {total_records}')
" 2>/dev/null | while read line; do log "SUMMARY: $line"; done
    else
        log "✗ Phase 1 failed (exit code $?)"
    fi
}

# ─── Phase 2: Hybrid pipeline (needs Ollama) ─────────────────
phase2_hybrid() {
    log "=== PHASE 2: HYBRID PIPELINE (LLM) ==="

    if ! curl -s http://localhost:11434/api/tags &>/dev/null; then
        log "⚠ Ollama not available — skipping Phase 2"
        return 0
    fi

    local INPUT_DIR="$PROJECT_DIR/docs/testingsamples"
    local OUTPUT="$RESULTS_DIR/phase2_hybrid_${TIMESTAMP}.json"

    log "Running test_hybrid_pipeline.py..."
    cd "$PROJECT_DIR"

    if python3 scripts/test_hybrid_pipeline.py --pdf-dir "$INPUT_DIR" 2>&1 | tee -a "$RESULTS_DIR/run_${TIMESTAMP}.log"; then
        log "✓ Phase 2 complete"
        # Copy the default output if it exists
        [ -f "$INPUT_DIR/hybrid_pipeline_results.json" ] && cp "$INPUT_DIR/hybrid_pipeline_results.json" "$OUTPUT"
    else
        log "⚠ Phase 2 had errors (exit code $?)"
    fi
}

# ─── Phase 3: Test suite ────────────────────────────────────
phase3_tests() {
    log "=== PHASE 3: TEST SUITE ==="
    cd "$PROJECT_DIR"

    local TEST_OUTPUT="$RESULTS_DIR/phase3_tests_${TIMESTAMP}.txt"

    if python3 -m pytest tests/ -v --tb=short 2>&1 | tee "$TEST_OUTPUT" | tail -30 | tee -a "$RESULTS_DIR/run_${TIMESTAMP}.log"; then
        log "✓ Phase 3: All tests passed"
    else
        log "⚠ Phase 3: Some tests failed — see $TEST_OUTPUT"
        # Extract failure summary
        grep -E "FAILED|ERROR" "$TEST_OUTPUT" 2>/dev/null | head -20 | while read line; do log "  $line"; done
    fi
}

# ─── Write structured report for Claude to read ─────────────
write_report() {
    local REPORT="$RESULTS_DIR/overnight_report_${TIMESTAMP}.md"
    cat > "$REPORT" << 'HEADER'
# Forentis AI — Overnight Run Report
HEADER
    echo "**Timestamp:** $(date)" >> "$REPORT"
    echo "**Phase:** $PHASE" >> "$REPORT"
    echo "" >> "$REPORT"

    if [ -f "$RESULTS_DIR/run_${TIMESTAMP}.log" ]; then
        echo "## Run Log" >> "$REPORT"
        echo '```' >> "$REPORT"
        cat "$RESULTS_DIR/run_${TIMESTAMP}.log" >> "$REPORT"
        echo '```' >> "$REPORT"
    fi

    # Symlink latest report
    ln -sf "$REPORT" "$RESULTS_DIR/LATEST_REPORT.md"
    log "Report written to $REPORT"
}

# ─── Main ────────────────────────────────────────────────────
log "Forentis AI overnight run starting — Phase: $PHASE"
check_env

case "$PHASE" in
    1)      phase1_extract ;;
    2)      phase2_hybrid ;;
    3)      phase3_tests ;;
    all)    phase1_extract; phase2_hybrid; phase3_tests ;;
    *)      log "Unknown phase: $PHASE"; exit 1 ;;
esac

write_report
log "=== OVERNIGHT RUN COMPLETE ==="
