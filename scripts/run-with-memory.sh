#!/bin/bash
# run-with-memory.sh — Run Claude Code with persistent working memory
#
# Usage: ./scripts/run-with-memory.sh "Your task description"
#
# Creates docs/WORKSTATE.md if missing, prepends memory instructions,
# and runs Claude Code with --yes flag.

set -e

WORKSTATE="docs/WORKSTATE.md"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

if [ -z "$1" ]; then
    echo "Usage: ./scripts/run-with-memory.sh 'Your task description'"
    echo ""
    echo "Examples:"
    echo "  ./scripts/run-with-memory.sh 'Refactor auth module to use JWT'"
    echo "  ./scripts/run-with-memory.sh 'Read PLAN.md Step 5 and execute it'"
    exit 1
fi

TASK="$1"

# Create WORKSTATE.md if it doesn't exist
if [ ! -f "$WORKSTATE" ]; then
    mkdir -p docs
    cat > "$WORKSTATE" << 'EOF'
# WORKSTATE.md — Live Task Checkpoint
# This file is your external memory. Read it first, update it often.

## Current Task
[Will be set by agent]

## Task Status
- [ ] In progress

## Findings
### Code Locations Found
[Agent will populate with file:line references]

### Key Decisions Made
[Agent will record architectural choices]

### Important Context
[Anything needed after compaction]

## Files Modified So Far
[Agent will track: filename — change description ✓ DONE / IN PROGRESS / NOT STARTED]

## Files Created
[New files created during this task]

## Tests Status
[Which tests exist and their pass/fail status]

## Remaining Work
[Ordered checklist — agent checks off items as they complete]

## Blockers
None

## Last Updated
[Agent will set timestamp]
EOF
    echo -e "${GREEN}Created $WORKSTATE${NC}"
else
    echo -e "${BLUE}Resuming from existing $WORKSTATE${NC}"
    echo ""
    grep "^## Current Task" -A1 "$WORKSTATE" 2>/dev/null | tail -1
    echo ""
fi

# Build prompt with memory instructions
PROMPT="PERSISTENT MEMORY INSTRUCTIONS — follow these EXACTLY:

1. FIRST ACTION: Read docs/WORKSTATE.md. If it has prior progress, CONTINUE from where it left off. Do NOT redo completed work.

2. CHECKPOINT RULE: After EVERY significant action, update docs/WORKSTATE.md:
   - After finding important code locations → update 'Code Locations Found'
   - After modifying a file → update 'Files Modified So Far' with ✓ DONE
   - After creating a file → update 'Files Created'
   - After running tests → update 'Tests Status'
   - After completing a sub-task → check it off in 'Remaining Work'
   - Always update 'Last Updated' with current timestamp

3. RECOVERY RULE: If you notice your context feels incomplete or you're unsure what you've done, STOP and re-read docs/WORKSTATE.md before continuing.

4. PROACTIVE SAVE: If you've been working for a while and have accumulated many findings, write a detailed checkpoint to WORKSTATE.md NOW, even if you haven't finished the current sub-task.

5. COMPLETION: When done, set Task Status to complete, update all sections, and run final tests.

---

TASK: $TASK"

echo -e "${GREEN}Running with persistent memory...${NC}"
echo -e "${BLUE}Task: $TASK${NC}"
echo ""

claude -p "$PROMPT" --dangerously-skip-permissions

# Post-run summary
echo ""
echo -e "${GREEN}=== Run Complete ===${NC}"
if [ -f "$WORKSTATE" ]; then
    echo ""
    remaining=$(grep -c "NOT STARTED\|NOT YET\|pending\|← NEXT" "$WORKSTATE" 2>/dev/null || echo "0")
    done=$(grep -c "✓\|DONE\|complete" "$WORKSTATE" 2>/dev/null || echo "0")
    echo -e "Completed: ${GREEN}$done${NC} items"
    echo -e "Remaining: ${YELLOW}$remaining${NC} items"
    if [ "$remaining" != "0" ] && [ "$remaining" != "" ]; then
        echo ""
        echo -e "${YELLOW}Run ./scripts/resume.sh to continue remaining work${NC}"
    fi
fi
