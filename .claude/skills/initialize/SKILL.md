---
name: initialize
description: Verify AND repair the vault — run the golden tests, integrity checks, re-render every thread, and audit the hook/plugin/snippet/config wiring, fixing every safely-fixable fault and reporting the rest. Use at first setup, after pulling apparatus changes, when something looks off, or whenever you want a clean baseline.
---

# /initialize

Runs the deterministic health-and-repair pass and reports. No LLM guessing — it
executes `_system/doctor.py --fix`, which verifies everything, repairs what's
safely fixable, then re-verifies.

## Run it

1. **Verify + repair** — `python3 _system/doctor.py --fix` (drop `--fix` to diagnose only).
2. **Start the daemon if it's down** — `_system/daemon.sh install` (idempotent). This installs
   the watcher as an **independent background service** — a systemd *user* service where one is
   available (survives the launching shell, restarts on crash, and with linger survives
   logout/reboot), or a detached `setsid`+`nohup` process otherwise. **Do not** launch it as a
   plain shell background (`python3 _system/watch.py &`): that makes the daemon a child of Claude
   Code, so it dies when Claude Code closes or crashes. Invoking `/initialize` **is** the
   operator's approval to install + run the watcher, which powers the Obsidian buttons (Summon et
   al.). Manage it with `_system/daemon.sh {status|restart|stop|logs}`. The doctor only *reports*
   daemon status — the skill is what starts it. (Capture is separate: the prompt hook runs inside
   Claude Code itself, so operator prompts are captured whether or not the daemon is up; replies
   are minted by the agent via `record`.)

Exit 0 = healthy (possibly after repairs). Exit 1 = something still failing that
needs a human — look for `✗`.

## What it verifies

- **environment** — Python ≥ 3.8; whether the `claude` CLI and an API key exist (both
  optional — only Summon needs them).
- **apparatus** — every tool/doc exists (incl. the prompt-capture hook `capture_prompt.py`);
  the prompt hook (UserPromptSubmit) is wired; `alwaysUpdateLinks: false` (the guard that stops
  Obsidian from rewriting `[[wikilinks]]`
  inside cards and breaking hashes); plugin id is `exo-ribbon`; the card-styling snippets are
  present and enabled — including the `claude-tui`/`claude-api` callout styling in
  `persona-cards.css` (without it, terminal-voice and summon cards fall back to the default
  callout background).
- **the spine** — golden tests (enc:v1 / round-trip / thread isolation); `validate` (every
  card hashes to its name, reply links resolve); the CLI dispatches; **every thread
  re-renders byte-identical from its records** (no drift).
- **daemon** — whether `watch.py` is running.

## What `--fix` repairs (and what it won't)

Repairs only the unambiguous, **lossless** faults, then re-checks:

- flips `alwaysUpdateLinks` back to false (writes `app.json` if missing)
- re-injects the prompt-capture hook (UserPromptSubmit) into `settings.json` (preserving your settings)
- re-enables the `persona-cards` snippet; sets the plugin id back to `exo-ribbon`
- **appends the `claude-tui`/`claude-api` callout styling** to `persona-cards.css` (and enables
  `persona-cards`) if a snippet was imported without it — otherwise those cards render on the
  default callout background
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
- The **doctor** never starts the daemon — it reports status and prints the command. The
  **skill** (step 2 in "Run it") starts it, because invoking `/initialize` is your approval to.

Acting on a `✗` it won't auto-fix is a deliberate, human step. That boundary is the point: it
repairs what's safe and is honest about the rest, rather than hiding data loss behind a green
checkmark.
