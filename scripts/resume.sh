#!/bin/bash
# resume.sh — Resume an interrupted task from WORKSTATE.md checkpoint
#
# Usage: ./scripts/resume.sh
#
# Reads docs/WORKSTATE.md and tells Claude Code to continue from where
# the previous run left off. Each resume gets a fresh context window
# focused only on remaining work.

set -e

WORKSTATE="docs/WORKSTATE.md"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

if [ ! -f "$WORKSTATE" ]; then
    echo -e "${RED}No docs/WORKSTATE.md found. Nothing to resume.${NC}"
    echo "Use ./scripts/run-with-memory.sh to start a new task."
    exit 1
fi

echo -e "${BLUE}=== Resuming from checkpoint ===${NC}"
echo ""
echo "Task:"
grep "^## Current Task" -A1 "$WORKSTATE" 2>/dev/null | tail -1
echo ""
echo "Remaining work:"
grep "NOT STARTED\|NOT YET\|pending\|← NEXT\|IN PROGRESS" "$WORKSTATE" 2>/dev/null | head -5
echo ""

PROMPT="PERSISTENT MEMORY INSTRUCTIONS — follow these EXACTLY:

1. FIRST ACTION: Read docs/WORKSTATE.md carefully. It contains your checkpoint from a previous session.

2. DO NOT redo work already marked as ✓ or DONE in WORKSTATE.md.

3. CONTINUE from the first item in 'Remaining Work' that is NOT marked done.

4. After EVERY significant action, update docs/WORKSTATE.md:
   - After modifying a file → mark it ✓ DONE in 'Files Modified'
   - After running tests → update 'Tests Status'
   - After completing a sub-task → check it off in 'Remaining Work'
   - Always update 'Last Updated'

5. When all remaining work is done, set Task Status to complete and run final tests.

TASK: Resume from docs/WORKSTATE.md checkpoint. Complete all remaining work listed there."

echo -e "${YELLOW}Resuming...${NC}"
echo ""

claude -p "$PROMPT" --dangerously-skip-permissions

# Post-run summary
echo ""
echo -e "${GREEN}=== Resume Complete ===${NC}"
if [ -f "$WORKSTATE" ]; then
    remaining=$(grep -c "NOT STARTED\|NOT YET\|pending\|← NEXT" "$WORKSTATE" 2>/dev/null || echo "0")
    if [ "$remaining" = "0" ] || [ "$remaining" = "" ]; then
        echo -e "${GREEN}All work complete!${NC}"
    else
        echo -e "${YELLOW}$remaining items still pending. Run ./scripts/resume.sh again.${NC}"
    fi
fi
