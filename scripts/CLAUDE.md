
## Persistent Working Memory

Claude Code sub-agents use `docs/WORKSTATE.md` as external memory for tasks that modify more than 2 files. This prevents context compaction from losing progress.

### Rules
- **Read first:** Before any work, read `docs/WORKSTATE.md` if it exists
- **Write often:** After every file modification or important finding, update WORKSTATE.md
- **Trust the file:** After compaction, WORKSTATE.md is the source of truth
- **Don't redo:** If WORKSTATE.md says a file is modified ✓, skip it
- **Proactive save:** If you've accumulated many findings, checkpoint NOW

### Scripts
```bash
./scripts/run-with-memory.sh 'task description'   # New task with memory
./scripts/resume.sh                                 # Continue interrupted task
./scripts/clean.sh                                  # Archive and reset
```
