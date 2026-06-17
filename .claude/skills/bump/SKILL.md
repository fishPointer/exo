---
name: bump
description: The heartbeat. On "bump" / "/bump", run the bump verb and answer each head it prints. A nervous reflex, not a procedure — the dashboard does the thinking.
---

# /bump

A reflex. Two moves, no deliberation:

1. **Run the verb.** It reconciles every dirty thread (folds staged drafts into cards; code-highlights survive for `pull`), refreshes `DASHBOARD.md`, and prints the reply-debt queue with each owed head's text:
   ```
   python3 _system/stream.py bump
   ```

2. **Answer each head it lists.** One `record` per head, then re-emit the frame:
   ```
   printf '%s' "your reply" | python3 _system/stream.py record --author claude-tui --reply-to <id> --view notes/<thread>.md
   ```

`none — clean beat` → say "no debt" and stop.

Don't scan, don't re-validate, don't narrate the steps, don't re-read the dashboard. The verb already put the rails in front of you — just answer.
