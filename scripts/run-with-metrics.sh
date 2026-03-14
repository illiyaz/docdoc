#!/bin/bash
# run-with-metrics.sh — Run Claude Code with persistent memory AND metrics tracking
#
# Usage: ./scripts/run-with-metrics.sh "Your task description"
#
# Tracks: session duration, WORKSTATE.md checkpoint count (proxy for compaction),
# files modified, tests run, and git diff stats.
# Writes metrics to docs/workstate_metrics.jsonl (one JSON line per run).

set -e

WORKSTATE="docs/WORKSTATE.md"
METRICS_FILE="docs/workstate_metrics.jsonl"
METRICS_DIR="docs"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

if [ -z "$1" ]; then
    echo "Usage: ./scripts/run-with-metrics.sh 'Your task description'"
    exit 1
fi

TASK="$1"
RUN_ID=$(date +%s)
START_TIME=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
START_EPOCH=$(date +%s)

# Snapshot WORKSTATE before run (to detect checkpoint writes)
WORKSTATE_HASH_BEFORE=""
if [ -f "$WORKSTATE" ]; then
    WORKSTATE_HASH_BEFORE=$(md5sum "$WORKSTATE" 2>/dev/null | cut -d' ' -f1 || md5 -q "$WORKSTATE" 2>/dev/null || echo "none")
fi

# Count existing WORKSTATE lines before run
WORKSTATE_LINES_BEFORE=0
if [ -f "$WORKSTATE" ]; then
    WORKSTATE_LINES_BEFORE=$(wc -l < "$WORKSTATE" | tr -d ' ')
fi

# Snapshot git state
GIT_DIFF_BEFORE=$(git diff --stat 2>/dev/null | tail -1 || echo "no git")
FILES_BEFORE=$(git diff --name-only 2>/dev/null | wc -l | tr -d ' ' || echo "0")

# Create WORKSTATE if missing
if [ ! -f "$WORKSTATE" ]; then
    mkdir -p docs
    cat > "$WORKSTATE" << 'EOF'
# WORKSTATE.md — Live Task Checkpoint

## Current Task
[Will be set by agent]

## Task Status
- [ ] In progress

## Checkpoint Log
<!-- Agent appends a line here each time it writes a checkpoint -->
<!-- Format: [timestamp] checkpoint reason -->

## Findings
### Code Locations Found
[Agent will populate]

### Key Decisions Made
[Agent will populate]

## Files Modified So Far
[Agent will track]

## Files Created
[Agent will track]

## Tests Status
[Agent will track]

## Remaining Work
[Agent will populate]

## Blockers
None

## Last Updated
[Agent will set]
EOF
    echo -e "${GREEN}Created $WORKSTATE${NC}"
fi

# Build prompt with memory + metrics instructions
PROMPT="PERSISTENT MEMORY INSTRUCTIONS — follow these EXACTLY:

1. FIRST ACTION: Read docs/WORKSTATE.md. If it has prior progress, CONTINUE from where it left off.

2. CHECKPOINT RULE: After EVERY significant action, update docs/WORKSTATE.md:
   - After finding code locations → update 'Code Locations Found'
   - After modifying a file → mark ✓ DONE in 'Files Modified So Far'
   - After creating a file → update 'Files Created'
   - After running tests → update 'Tests Status'
   - After completing a sub-task → check it off in 'Remaining Work'
   - Always update 'Last Updated' with current timestamp

3. CHECKPOINT LOG: Every time you write to WORKSTATE.md, also append a line to the 'Checkpoint Log' section:
   Format: [YYYY-MM-DD HH:MM:SS] reason for checkpoint
   Example: [2026-03-14 10:30:00] Found extract_generator at line 780, recorded locations
   Example: [2026-03-14 10:35:00] Modified settings.py, marked done
   Example: [2026-03-14 10:40:00] Context getting large, proactive checkpoint before continuing
   This log helps us measure how often checkpoints happen and why.

4. RECOVERY RULE: If you feel unsure about what you've done, re-read docs/WORKSTATE.md.

5. PROACTIVE SAVE: If you've accumulated many findings, write a checkpoint NOW.

6. COMPLETION: Set Task Status to complete, update all sections, run final tests.

---

TASK: $TASK"

echo -e "${GREEN}Running with persistent memory + metrics...${NC}"
echo -e "${BLUE}Task: $TASK${NC}"
echo -e "${CYAN}Run ID: $RUN_ID${NC}"
echo ""

# Run Claude Code
claude -p "$PROMPT" --dangerously-skip-permissions

# Capture end state
END_TIME=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
END_EPOCH=$(date +%s)
DURATION=$((END_EPOCH - START_EPOCH))

# Count checkpoints written (lines in Checkpoint Log section starting with [)
CHECKPOINT_COUNT=0
if [ -f "$WORKSTATE" ]; then
    CHECKPOINT_COUNT=$(sed -n '/^## Checkpoint Log/,/^## /{/^\[/p}' "$WORKSTATE" 2>/dev/null | wc -l | tr -d ' ')
fi

# Count WORKSTATE changes
WORKSTATE_LINES_AFTER=0
WORKSTATE_HASH_AFTER=""
if [ -f "$WORKSTATE" ]; then
    WORKSTATE_LINES_AFTER=$(wc -l < "$WORKSTATE" | tr -d ' ')
    WORKSTATE_HASH_AFTER=$(md5sum "$WORKSTATE" 2>/dev/null | cut -d' ' -f1 || md5 -q "$WORKSTATE" 2>/dev/null || echo "none")
fi

WORKSTATE_CHANGED="false"
if [ "$WORKSTATE_HASH_BEFORE" != "$WORKSTATE_HASH_AFTER" ]; then
    WORKSTATE_CHANGED="true"
fi

# Count files modified and created (strip whitespace from wc output)
FILES_MODIFIED=$(grep -c '✓\|DONE' "$WORKSTATE" 2>/dev/null | tr -d ' ' || echo "0")
FILES_CREATED=$(grep -c 'created\|Created\|NEW' "$WORKSTATE" 2>/dev/null | tr -d ' ' || echo "0")

# Count remaining work
REMAINING=$(grep -c 'NOT STARTED\|NOT YET\|pending\|← NEXT' "$WORKSTATE" 2>/dev/null | tr -d ' ' || echo "0")

# Git diff stats
FILES_AFTER=$(git diff --name-only 2>/dev/null | wc -l | tr -d ' ' || echo "0")
GIT_DIFF_AFTER=$(git diff --stat 2>/dev/null | tail -1 || echo "no git")
LINES_ADDED=$(git diff --numstat 2>/dev/null | awk '{sum += $1} END {print sum+0}' || echo "0")
LINES_REMOVED=$(git diff --numstat 2>/dev/null | awk '{sum += $2} END {print sum+0}' || echo "0")

# Determine if task completed (fixed: no piped grep)
TASK_COMPLETE="false"
if [ -f "$WORKSTATE" ]; then
    # Check if Task Status line contains "Complete" or "[x]"
    if grep -q '\[x\].*[Cc]omplete' "$WORKSTATE" 2>/dev/null; then
        TASK_COMPLETE="true"
    elif [ "$REMAINING" = "0" ]; then
        TASK_COMPLETE="true"
    fi
fi

# Infer compaction events:
# If checkpoint_count > 3 and duration > 120s, compaction likely occurred
ESTIMATED_COMPACTIONS=0
if [ "$CHECKPOINT_COUNT" -gt 3 ] 2>/dev/null; then
    ESTIMATED_COMPACTIONS=$(( (CHECKPOINT_COUNT - 2) / 2 ))
fi

# Write metrics (single line JSON, no embedded newlines)
mkdir -p "$METRICS_DIR"
printf '{"run_id":"%s","task":"%s","start":"%s","end":"%s","duration_seconds":%d,"checkpoints_written":%d,"estimated_compactions":%d,"files_modified":%d,"files_created":%d,"remaining_items":%d,"task_complete":%s,"workstate_changed":%s,"workstate_lines_before":%d,"workstate_lines_after":%d,"git_files_changed":%d,"git_lines_added":%d,"git_lines_removed":%d}\n' \
    "$RUN_ID" \
    "$(echo "$TASK" | head -c 100 | sed 's/"/\\"/g' | tr '\n' ' ')" \
    "$START_TIME" \
    "$END_TIME" \
    "$DURATION" \
    "$CHECKPOINT_COUNT" \
    "$ESTIMATED_COMPACTIONS" \
    "$FILES_MODIFIED" \
    "$FILES_CREATED" \
    "$REMAINING" \
    "$TASK_COMPLETE" \
    "$WORKSTATE_CHANGED" \
    "$WORKSTATE_LINES_BEFORE" \
    "$WORKSTATE_LINES_AFTER" \
    "$FILES_AFTER" \
    "$LINES_ADDED" \
    "$LINES_REMOVED" \
    >> "$METRICS_FILE"

# Display metrics
echo ""
echo -e "${CYAN}═══════════════════════════════════════════════${NC}"
echo -e "${CYAN}  Session Metrics${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════${NC}"
echo -e "  Duration:             ${GREEN}${DURATION}s$(if [ $DURATION -gt 60 ]; then echo " ($(($DURATION/60))m $(($DURATION%60))s)"; fi)${NC}"
echo -e "  Checkpoints written:  ${GREEN}${CHECKPOINT_COUNT}${NC}"
echo -e "  Est. compactions:     ${YELLOW}${ESTIMATED_COMPACTIONS}${NC}"
echo -e "  Files modified:       ${GREEN}${FILES_MODIFIED}${NC}"
echo -e "  Files created:        ${GREEN}${FILES_CREATED}${NC}"
echo -e "  Remaining items:      ${YELLOW}${REMAINING}${NC}"
echo -e "  Task complete:        $(if [ "$TASK_COMPLETE" = "true" ]; then echo "${GREEN}YES${NC}"; else echo "${YELLOW}NO${NC}"; fi)"
echo -e "  Git lines added:      ${GREEN}+${LINES_ADDED}${NC}"
echo -e "  Git lines removed:    ${GREEN}-${LINES_REMOVED}${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════${NC}"

if [ "$REMAINING" != "0" ] && [ "$REMAINING" != "" ]; then
    echo ""
    echo -e "${YELLOW}Run ./scripts/resume.sh to continue remaining work${NC}"
fi

echo -e "${BLUE}Metrics saved to $METRICS_FILE${NC}"

# Auto-archive if task completed
if [ "$TASK_COMPLETE" = "true" ]; then
    echo ""
    echo -e "${GREEN}Task complete — auto-archiving WORKSTATE.md${NC}"
    ./scripts/clean.sh
fi