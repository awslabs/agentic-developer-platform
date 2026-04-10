# Beads (bd) - Agent Task Management

Beads is the shared state management system for all ADP agents. It provides a distributed graph issue tracker powered by Dolt, enabling multi-agent coordination and persistent state across sessions.

**Documentation**: https://github.com/steveyegge/beads

## Critical: Remote Sync Workflow

> "The plane has NOT landed until `git push` completes successfully."
> — Beads Agent Instructions

### Session Start (REQUIRED)

At the START of every session, sync from remote:

```bash
# 1. Pull latest Beads state from remote
bd dolt pull origin main

# 2. Check what's ready to work on
bd ready --json
```

### Session End (MANDATORY)

At the END of every session, you MUST push:

```bash
# 1. Push Beads state to remote
bd dolt push origin main

# 2. Sync git (if using git)
git pull --rebase
git push
git status  # Verify "up to date with origin/main"
```

**NEVER stop before pushing.** Unpushed work breaks multi-agent coordination.

---

## When to Use Beads

| Situation | Command |
|-----------|---------|
| Start of session | `bd dolt pull origin main` |
| Check ready work | `bd ready --json` |
| See task details | `bd show <task-id>` |
| Create new task | `bd create "Title" -p 1 --json` |
| Claim a task | `bd update <task-id> --claim` |
| Update status | `bd update <task-id> --status in_progress` |
| Mark blocked | `bd dep add <your-task> <blocking-task>` |
| Complete task | `bd update <task-id> --status done` |
| End of session | `bd dolt push origin main` |

---

## Essential Commands

### Check Ready Work

```bash
# List tasks with no blockers (ready to start)
bd ready --json
```

### View Task Details

```bash
# Show task with dependencies and history
bd show bd-a3f8
```

### Create Task (When You Discover New Work)

```bash
# Create a subtask discovered during your work
bd create "Fix validation bug found during auth implementation" -p 1 --json
# Returns: bd-a3f8.4

# Link it as discovered-from your current task
bd dep add bd-a3f8.4 bd-a3f8.2 --type discovered-from
```

### Update Task Status

```bash
# Claim task ownership
bd update bd-a3f8 --claim

# Update status
bd update bd-a3f8 --status in_progress
bd update bd-a3f8 --status done

# Update description (avoid bd edit - use flags)
bd update bd-a3f8 --description "Updated description text"
```

### Manage Dependencies

```bash
# If you find your task is blocked by another
bd dep add <your-task> <blocking-task> --type blocks

# If a dependency is resolved
bd dep remove <your-task> <resolved-task>
```

### Check Project Status

```bash
# List all tasks in project
bd list --json

# Filter by status
bd list --status open --json
bd list --status in_progress --json
```

### Remote Operations

```bash
# Pull latest state from remote
bd dolt pull origin main

# Push your changes to remote
bd dolt push origin main

# List configured remotes
bd dolt remote list
```

---

## Task ID Format

Beads uses hierarchical hash-based IDs to prevent collisions:

| ID Format | Level | Example |
|-----------|-------|---------|
| `bd-a3f8` | Epic (top-level) | Main feature |
| `bd-a3f8.1` | Task under epic | Implementation task |
| `bd-a3f8.1.1` | Subtask | Specific action |

---

## Dependency Types

| Type | Meaning | Use When |
|------|---------|----------|
| `blocks` | Task A must complete before B can start | Sequential dependencies |
| `parent-child` | Hierarchical relationship | Epic → Task → Subtask |
| `discovered-from` | Found during work on another task | You discover new work |
| `related` | Soft link for reference | Related but not blocking |

---

## Integration with GitHub

- Beads tasks can link to GitHub issues via metadata
- Use `#NNN` in task title to reference GitHub issue
- Include issue ID in commit messages: `"Fix auth bug (bd-abc)"`
- PM agent syncs Beads state to GitHub Projects for human visibility

---

## Best Practices

### Do's

1. **Always sync at session start**: `bd dolt pull origin main`
2. **Always push at session end**: `bd dolt push origin main`
3. **Create discovered tasks**: If you find new work, create and link it
4. **Update blockers immediately**: If blocked, add the dependency
5. **Use JSON output**: Always use `--json` for programmatic access
6. **Use flag-based updates**: `bd update --description` instead of `bd edit`

### Don'ts

1. **Never skip the push**: Unpushed work breaks coordination
2. **Don't use `bd edit`**: It opens an interactive editor that agents can't use
3. **Don't close tasks directly**: The orchestration layer handles completion
4. **Don't leave work uncommitted**: Each write auto-commits to Dolt

---

## Complete Session Workflow

```bash
# ========== SESSION START ==========

# 1. Pull latest Beads state
bd dolt pull origin main

# 2. Check what's ready to work on
bd ready --json

# 3. View your assigned task
bd show bd-a3f8.2

# 4. Claim the task
bd update bd-a3f8.2 --claim
bd update bd-a3f8.2 --status in_progress

# ========== DO YOUR WORK ==========

# 5. During work, if you discover a bug
bd create "Fix null pointer in auth handler" -p 1 --json
# Returns: bd-a3f8.4

# 6. Link as discovered work
bd dep add bd-a3f8.4 bd-a3f8.2 --type discovered-from

# 7. When task is complete
bd update bd-a3f8.2 --status done

# ========== SESSION END (MANDATORY) ==========

# 8. Push Beads state to remote
bd dolt push origin main

# 9. Push git changes
git pull --rebase
git push
git status  # Must show "up to date with origin/main"
```

---

## Troubleshooting

### "bd: command not found"

Beads not installed or not in PATH:
```bash
export PATH="$HOME/.local/bin:$PATH"
bd version
```

### "failed to pull from origin"

Remote not configured or credentials missing:
```bash
bd dolt remote list
aws sts get-caller-identity  # Check AWS auth
```

### "merge conflict"

Multiple agents modified same data:
```bash
bd dolt status
bd dolt merge --abort  # If needed
bd dolt pull origin main
```

---

## AWS Remote Configuration

Our Beads remote uses AWS S3 + DynamoDB:

```
Remote URL: aws://[adp-beads-manifest:adp-beads-state-193832579677]/adp
Region: us-east-1
```

Required environment variables:
```bash
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
```

---

## Reference Links

- **Beads GitHub**: https://github.com/steveyegge/beads
- **Agent Instructions**: https://github.com/steveyegge/beads/blob/main/AGENT_INSTRUCTIONS.md
- **CLI Reference**: https://github.com/steveyegge/beads/blob/main/docs/CLI_REFERENCE.md
- **Troubleshooting**: https://github.com/steveyegge/beads/blob/main/docs/TROUBLESHOOTING.md
