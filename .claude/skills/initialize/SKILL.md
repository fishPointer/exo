---
name: initialize
description: The agent's wake-up. Verify AND repair the vault — run the golden tests, integrity checks, re-render every thread, and audit the hook/plugin/snippet/config wiring, fixing every safely-fixable fault and reporting the rest — then orient: read the dashboard's reply-debt + recent work back and confirm where we left off, like clocking in and checking your email before starting. Use at the start of a session, at first setup, after pulling apparatus changes, when something looks off, or whenever you want a clean baseline.
---

# /initialize

The agent's **wake-up**: clock in, run the deterministic health-and-repair pass, then **orient** —
read the queue back and confirm where we left off, the way you'd log in and check your email before
starting work. Two halves, deliberately split by who does the judging:

- **The health pass is `_system/doctor.py --fix`** — no LLM guessing. It verifies everything,
  repairs what's safely fixable, then re-verifies. Deterministic; that boundary is the point.
- **The orientation is the agent's** (step 3) — read the *refreshed* dashboard back, reconstruct
  where each thread left off, and confirm the action items. This is the LLM-layer half the
  deterministic doctor can't do, and it's what makes `/initialize` a wake-up rather than just a
  health check.

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
3. **Orient — read your queue and confirm where we left off.** The doctor just refreshed
   `DASHBOARD.md`; now *read it back* and turn it into an action briefing. The machine is proven
   healthy — this step proves the **agent** is oriented. Specifically:
   - **Read `DASHBOARD.md`** and restate, in plain language, the **reply-debt queue** (every owed
     head + its text) and any **dirty threads**. These are the action items left on the table.
   - **Skim the head card(s)** of each active thread to reconstruct *what* we left off on, not just
     *that* a reply is owed — re-show a card untruncated with
     `python3 _system/stream.py render-tui --id <id>` (omit `--id` for the head), or read the view.
   - On a **fresh wake** (operational model not yet loaded this session), read
     `_system/ARCHITECTURE.md` and `.claude/CLAUDE.md` so the verbs and iron rules are in hand
     before you touch a thread.
   - Close with an explicit **readiness line**: `N threads · M owed replies` + a one-sentence "here's
     what we left off on — ready." Clean board → say so ("no reply debt — all clear").

   This step is **read + report only**. It never auto-answers the queue — that's a `bump`, on the
   operator's explicit nudge (rule #4). Orienting is checking your inbox; replying is a separate,
   deliberate beat — no autonomous loop sneaks in here.

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
- **the spine** — golden tests (enc:v1 / round-trip / global pool + inclusion / fork+clone);
  `validate` (every pooled card hashes to its id, reply links resolve); the CLI dispatches;
  **every thread re-renders byte-identical from its manifest + the pool** (no drift); and
  **every manifest id resolves to a pooled card** (inclusion integrity — a dangling id would be
  silently dropped from the view).
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
- **`✗ validate`** — a pooled card's body no longer hashes to its id: data corruption. The doctor
  can't know the correct body. Inspect `_system/data/cards/<id>.md` and fix or remove the file.
- **`✗ <source file> missing`** — a broken clone. `git checkout -- <path>`.
- **malformed JSON** in a config — it refuses to clobber your file; fix the JSON (or delete it
  and re-run, then it writes the canonical version).
- The **doctor** never starts the daemon — it reports status and prints the command. The
  **skill** (step 2 in "Run it") starts it, because invoking `/initialize` is your approval to.

Acting on a `✗` it won't auto-fix is a deliberate, human step. That boundary is the point: it
repairs what's safe and is honest about the rest, rather than hiding data loss behind a green
checkmark.
