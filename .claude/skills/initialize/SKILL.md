---
name: initialize
description: Verify AND repair the vault — run the golden tests, integrity checks, re-render every thread, and audit the hook/plugin/snippet/config wiring, fixing every safely-fixable fault and reporting the rest. Use at first setup, after pulling apparatus changes, when something looks off, or whenever you want a clean baseline.
---

# /initialize

Runs the deterministic health-and-repair pass and reports. No LLM guessing — it
executes `_system/doctor.py --fix`, which verifies everything, repairs what's
safely fixable, then re-verifies.

## Run it

```
python3 _system/doctor.py --fix      # verify + repair (this is /initialize)
python3 _system/doctor.py            # verify only, change nothing (diagnose)
```

Exit 0 = healthy (possibly after repairs). Exit 1 = something still failing that
needs a human — look for `✗`.

## What it verifies

- **environment** — Python ≥ 3.8; whether the `claude` CLI and an API key exist (both
  optional — only Summon needs them).
- **apparatus** — every tool/doc exists; the capture hook points at `capture_prompt.py`;
  `alwaysUpdateLinks: false` (the guard that stops Obsidian from rewriting `[[wikilinks]]`
  inside cards and breaking hashes); plugin id is `exo-ribbon`; the card-styling snippet is
  present and enabled.
- **the spine** — golden tests (enc:v1 / round-trip / thread isolation); `validate` (every
  card hashes to its name, reply links resolve); the CLI dispatches; **every thread
  re-renders byte-identical from its records** (no drift).
- **daemon** — whether `watch.py` is running.

## What `--fix` repairs (and what it won't)

Repairs only the unambiguous, **lossless** faults, then re-checks:

- flips `alwaysUpdateLinks` back to false (writes `app.json` if missing)
- re-injects the capture hook into `settings.json` (preserving your other settings)
- re-enables the `stream-cards` snippet; sets the plugin id back to `exo-ribbon`
- **reconciles a drifted thread with `run`** — which folds any text you'd typed into cards
  (preserved, not discarded) and re-renders. Threads flagged `dirty` (known unsaved work) are
  left alone — it never auto-cards a draft you haven't committed.
- refreshes the dirty index + `DASHBOARD.md`

It deliberately **won't** auto-"fix":

- **`✗ golden tests`** — a code regression (someone changed `normalize()`). Real bug; read the
  test output. Don't paper over it.
- **`✗ validate`** — a record's body no longer hashes to its name: data corruption. The doctor
  can't know the correct body. Inspect `_system/records/<thread>/` and fix or remove the file.
- **`✗ <source file> missing`** — a broken clone. `git checkout -- <path>`.
- **malformed JSON** in a config — it refuses to clobber your file; fix the JSON (or delete it
  and re-run, then it writes the canonical version).
- It **never starts the daemon for you** — reports it down and prints the command.

Acting on a `✗` it won't auto-fix is a deliberate, human step. That boundary is the point: it
repairs what's safe and is honest about the rest, rather than hiding data loss behind a green
checkmark.
