#!/bin/bash
# clean.sh — Archive current WORKSTATE.md and start fresh
#
# Usage: ./scripts/clean.sh
#
# Archives docs/WORKSTATE.md to docs/workstate_archive/ with timestamp,
# then removes it so the next run starts clean.

WORKSTATE="docs/WORKSTATE.md"
ARCHIVE_DIR="docs/workstate_archive"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

if [ ! -f "$WORKSTATE" ]; then
    echo "No WORKSTATE.md to clean. Already fresh."
    exit 0
fi

mkdir -p "$ARCHIVE_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
TASK_NAME=$(grep "^## Current Task" -A1 "$WORKSTATE" 2>/dev/null | tail -1 | tr ' ' '_' | tr -cd '[:alnum:]_' | head -c 50)
ARCHIVE_FILE="$ARCHIVE_DIR/WORKSTATE_${TIMESTAMP}_${TASK_NAME}.md"

cp "$WORKSTATE" "$ARCHIVE_FILE"
rm "$WORKSTATE"

echo -e "${GREEN}Archived: $ARCHIVE_FILE${NC}"
echo -e "${YELLOW}WORKSTATE.md removed. Next run starts fresh.${NC}"
