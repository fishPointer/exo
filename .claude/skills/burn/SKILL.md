---
name: burn
description: Factory-reset the vault — wipe ALL cards and threads back to the empty seed scaffold, leaving the apparatus and local config untouched. Takes a local backup first (the net), since content lives in Obsidian Sync, not git. Use when the user wants to empty the vault of all captured content and start clean (e.g. handing off a fresh deployment, or clearing a scratch instance). NOT for deleting one thread or card — that's a manual file removal.
---

# /burn

Wipes the whole content store — every card in the global pool (`_system/data/cards/`),
every thread manifest (`_system/data/threads/`), the legacy `_system/records/` backup,
and every thread view in `notes/` — back to a fresh clone's seed scaffold (empty pool,
no manifests; `notes/` = just the empty seed `main.md`). Resets the local
`.stream/` drift state and regenerates `DASHBOARD.md`. Touches nothing else: the
apparatus (`_system/`, `.claude/`, `.obsidian/`) and per-machine config are left
exactly as they are. Full model: `_system/ARCHITECTURE.md` §4 + §6.

This is destructive and deliberate. The store is append-only by design (no
record-level delete verb); `/burn` is the one sanctioned total reset.

## The safety net is a local backup, not git

exo's content is distributed by Obsidian Sync, not git — `_system/data/*`,
`_system/records/*`, and `notes/*` are gitignored, so there is nothing in git to recover.
Before wiping, burn copies the pool + manifests + legacy records + views into
`.stream/burns/<ts>/` (gitignored, never synced, untouched by later burns). Recovery is a
plain `cp` of cards + threads back, then `validate`.

## Run it

```
python3 _system/burn.py            # dry-run: report what would be wiped, do nothing
python3 _system/burn.py --yes      # back up to .stream/burns/<ts>/, then wipe to seed
python3 _system/burn.py --yes --no-backup   # wipe with NO backup (recovery is your problem)
```

## How to use it

1. **Always dry-run first** — `python3 _system/burn.py` is read-only. Show the user the
   survey (how many cards / threads would go).
2. If the user did not explicitly say "yes / do it", **stop after the dry-run and confirm.**
   Never wipe without an explicit go-ahead.
3. On confirmation, run `python3 _system/burn.py --yes`.
4. **Stream the tool's output verbatim** — especially the backup path and the `cp …` recovery
   line. That path is the user's only recovery handle; do not omit or paraphrase it.
5. **Burn means burn — the turn must leave the thread *empty*.** There's no Stop hook to re-seed
   it: the reply primitive is opt-in (`record`), so just report the result in prose and do **not**
   `record`/summon into the wiped thread — that would re-seed what you just cleared.

After a burn the dashboard shows the empty state and `main.md` is an empty thread —
the same state a fresh clone ships in. `python3 _system/stream.py validate` should
pass (0 records, trivially valid).
