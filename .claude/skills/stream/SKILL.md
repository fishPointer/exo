---
name: stream
description: Drive the stream-cards thread store — post to a thread, reconcile edits (run), check integrity (validate), or rebuild a view from records (restore). Use whenever the user wants to append to, repair, or inspect a thread.
---

# /stream

Thin wrapper over `python3 _system/stream.py`. Records are the truth; threads are views.
Full model: `_system/ARCHITECTURE.md`. Default thread: `notes/threads/main.md`.

## Post a card (the only sanctioned way to write)

```
echo "the body" | python3 _system/stream.py record --author claude --reply-head --view notes/threads/main.md
```

- `--author` — who's speaking (`fish`, `claude`, `claude-api`, …).
- `--reply-head` — thread it onto the current head card. Omit for a root post.
- The command echoes the stored card's frame. **That echo is the message** — never re-type it.

## Reconcile a thread after typing into it (the Run button)

```
python3 _system/stream.py run --view notes/threads/main.md
```

Folds text you typed between cards into new cards, restores any card bodies you edited
(records win), re-renders, clears the dirty flag.

## Inspect / repair

```
python3 _system/stream.py validate                          # hash + link integrity, all threads
python3 _system/stream.py scan                              # flag drifted threads → .stream/dirty.json
python3 _system/stream.py render --view notes/threads/main.md --write   # rebuild view from records
python3 _system/stream.py dashboard --write                # refresh DASHBOARD.md
```

## New thread

A thread is any note with `type: stream` frontmatter under `notes/threads/`. Create the file,
then `record`/`run` against it with `--view notes/threads/<name>.md`. Its records live in
`notes/records/<name>/`.

## This file is also the skill template

To add a skill: make `.claude/skills/<name>/SKILL.md` with the same frontmatter
(`name`, `description`) and a body. See `_system/config/skills.md`.
