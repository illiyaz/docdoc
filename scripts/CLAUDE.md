
## 11) Persistent Working Memory

Claude Code sub-agents use `docs/WORKSTATE.md` as external memory for tasks modifying more than 2 files.

### Rules
- **Read first:** Before any work, read `docs/WORKSTATE.md` if it exists
- **Write often:** After every file modification or finding, update WORKSTATE.md
- **Trust the file:** After compaction, WORKSTATE.md is the source of truth
- **Don't redo:** If WORKSTATE.md says a file is modified ✓, skip it

### Scripts
```
./scripts/run-with-metrics.sh 'task description'   # New task with memory + metrics
./scripts/resume.sh                                  # Continue interrupted task
./scripts/clean.sh                                   # Archive and reset
./scripts/metrics-dashboard.sh                       # View aggregate stats
```
