#!/bin/bash
# metrics-dashboard.sh — Show aggregate metrics across all Claude Code runs
#
# Usage: ./scripts/metrics-dashboard.sh
#
# Reads docs/workstate_metrics.jsonl and displays summary statistics.

METRICS_FILE="docs/workstate_metrics.jsonl"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

if [ ! -f "$METRICS_FILE" ]; then
    echo -e "${RED}No metrics file found at $METRICS_FILE${NC}"
    echo "Run a task with ./scripts/run-with-metrics.sh first."
    exit 1
fi

TOTAL_RUNS=$(wc -l < "$METRICS_FILE" | tr -d ' ')

echo -e "${CYAN}╔═══════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║         Claude Code Working Memory Metrics        ║${NC}"
echo -e "${CYAN}╚═══════════════════════════════════════════════════╝${NC}"
echo ""

# Use Python for JSON parsing (available on most systems)
python3 << 'PYTHON_SCRIPT'
import json
import sys
from datetime import datetime

metrics_file = "docs/workstate_metrics.jsonl"
runs = []

with open(metrics_file) as f:
    for line in f:
        line = line.strip()
        if line:
            try:
                runs.append(json.loads(line))
            except json.JSONDecodeError:
                continue

if not runs:
    print("No metrics data found.")
    sys.exit(0)

# Aggregate stats
total_runs = len(runs)
total_duration = sum(r.get("duration_seconds", 0) for r in runs)
total_checkpoints = sum(r.get("checkpoints_written", 0) for r in runs)
total_compactions = sum(r.get("estimated_compactions", 0) for r in runs)
total_files_modified = sum(r.get("files_modified", 0) for r in runs)
total_files_created = sum(r.get("files_created", 0) for r in runs)
total_lines_added = sum(r.get("git_lines_added", 0) for r in runs)
total_lines_removed = sum(r.get("git_lines_removed", 0) for r in runs)
completed = sum(1 for r in runs if r.get("task_complete"))
incomplete = total_runs - completed

avg_duration = total_duration / total_runs if total_runs else 0
avg_checkpoints = total_checkpoints / total_runs if total_runs else 0
avg_compactions = total_compactions / total_runs if total_runs else 0

# Runs with compaction vs without
runs_with_compaction = sum(1 for r in runs if r.get("estimated_compactions", 0) > 0)
runs_without_compaction = total_runs - runs_with_compaction

# Completion rate by compaction
completed_with_compaction = sum(1 for r in runs if r.get("estimated_compactions", 0) > 0 and r.get("task_complete"))
completed_without_compaction = sum(1 for r in runs if r.get("estimated_compactions", 0) == 0 and r.get("task_complete"))

print(f"\033[1m  Overall Summary ({total_runs} runs)\033[0m")
print(f"  ───────────────────────────────────────")
print(f"  Total sessions:        {total_runs}")
print(f"  Completed:             \033[0;32m{completed}\033[0m")
print(f"  Incomplete:            \033[1;33m{incomplete}\033[0m")
print(f"  Completion rate:       \033[0;32m{completed/total_runs*100:.0f}%\033[0m")
print(f"  Total time:            {total_duration//60}m {total_duration%60}s")
print(f"  Avg session:           {avg_duration//60:.0f}m {avg_duration%60:.0f}s")
print()

print(f"\033[1m  Compaction Analysis\033[0m")
print(f"  ───────────────────────────────────────")
print(f"  Total checkpoints:     {total_checkpoints}")
print(f"  Avg per session:       {avg_checkpoints:.1f}")
print(f"  Est. compactions:      \033[1;33m{total_compactions}\033[0m")
print(f"  Avg per session:       {avg_compactions:.1f}")
print(f"  Sessions w/ compaction: {runs_with_compaction}/{total_runs} ({runs_with_compaction/total_runs*100:.0f}%)")
print(f"  Sessions clean:        {runs_without_compaction}/{total_runs} ({runs_without_compaction/total_runs*100:.0f}%)")
print()

if runs_with_compaction > 0:
    print(f"\033[1m  Compaction Impact\033[0m")
    print(f"  ───────────────────────────────────────")
    rate_with = completed_with_compaction/runs_with_compaction*100 if runs_with_compaction else 0
    rate_without = completed_without_compaction/runs_without_compaction*100 if runs_without_compaction else 0
    print(f"  Completion w/ compaction:  {rate_with:.0f}% ({completed_with_compaction}/{runs_with_compaction})")
    print(f"  Completion w/o compaction: {rate_without:.0f}% ({completed_without_compaction}/{runs_without_compaction})")
    print()

print(f"\033[1m  Code Output\033[0m")
print(f"  ───────────────────────────────────────")
print(f"  Files modified:        {total_files_modified}")
print(f"  Files created:         {total_files_created}")
print(f"  Lines added:           \033[0;32m+{total_lines_added}\033[0m")
print(f"  Lines removed:         \033[0;31m-{total_lines_removed}\033[0m")
print(f"  Net lines:             {total_lines_added - total_lines_removed}")
print()

# Recent runs table
print(f"\033[1m  Recent Runs (last 10)\033[0m")
print(f"  ───────────────────────────────────────")
print(f"  {'Task':<40} {'Duration':>8} {'Chkpts':>6} {'Comp':>5} {'Done':>5}")
for r in runs[-10:]:
    task = r.get("task", "unknown")[:38]
    dur = r.get("duration_seconds", 0)
    dur_str = f"{dur//60}m{dur%60:02d}s"
    chk = r.get("checkpoints_written", 0)
    comp = r.get("estimated_compactions", 0)
    done = "✓" if r.get("task_complete") else "✗"
    comp_color = "\033[1;33m" if comp > 0 else "\033[0;32m"
    done_color = "\033[0;32m" if done == "✓" else "\033[1;33m"
    print(f"  {task:<40} {dur_str:>8} {chk:>6} {comp_color}{comp:>5}\033[0m {done_color}{done:>5}\033[0m")

print()
PYTHON_SCRIPT
