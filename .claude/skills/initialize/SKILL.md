---
name: initialize
description: Verify the whole vault is wired and working — run the golden tests, integrity checks, re-render every thread, and audit the hook/plugin/snippet/config wiring, then report green/red. Use at first setup, after pulling apparatus changes, when something looks off, or whenever you want a clean baseline.
---

# /initialize

Runs the deterministic health check and reports. No LLM guessing — it executes
`_system/doctor.py`, which is read-only except for refreshing the derived
`DASHBOARD.md`.

## Run it

```
python3 _system/doctor.py
```

Exit 0 = healthy. Exit 1 = at least one required check failed (look for `✗`).

## What it verifies

- **environment** — Python ≥ 3.8; reports whether the `claude` CLI and an API key
  are present (both optional — only Summon needs them).
- **apparatus wiring** — every tool/doc exists; the capture hook actually points at
  `capture_prompt.py`; `alwaysUpdateLinks: false` is set (the guard that stops
  Obsidian from rewriting `[[wikilinks]]` inside cards and breaking hashes); the
  plugin id is `exo-ribbon`; the card-styling snippet is present and enabled.
- **the spine** — golden tests pass (enc:v1 / render round-trip / thread isolation);
  `validate` is clean (every card hashes to its name, reply links resolve); the CLI
  entrypoint dispatches and agrees with the library; **every thread re-renders
  byte-identical from its records** (no silent drift; a thread flagged `dirty` with
  unsaved edits is allowed to differ).
- **daemon** — reports whether `watch.py` is running.

## Reading the result

- `✓` required check passed · `✗` required check failed (fix it) · `·` optional/info.
- **`✗ threads render clean from records`** → a thread drifted. Hit **Restore**
  (`python3 _system/stream.py render --view <thread> --write`) to rebuild it from records.
- **`✗ validate`** → a card body was hand-edited. Restore the thread, or fix/remove the
  offending file under `notes/records/<thread>/`.
- **daemon `🔴 down`** → buttons won't work until you run `python3 _system/watch.py`. Not a
  failure — the CLI works without it. (Do not background a daemon silently for the operator;
  print the command and let them start it.)

This skill does not start the daemon or change content — it diagnoses. Acting on a `✗` is a
separate, deliberate step.
