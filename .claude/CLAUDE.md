# Operating contract — exo (stream-cards vault)

You are an agent working in a **stream-cards** vault: an append-only, content-addressed
thread store. Read `_system/ARCHITECTURE.md` once if you haven't; it's short.

## The one model you must hold

- **Records are the truth.** `_system/records/<thread>/<id>.md` — immutable, one per card,
  `id = sha256(normalize(body))[:8]`. The id IS the content. Partitioned by thread.
- **Threads are views.** `notes/<thread>.md` (flat, no subfolders) — a rendering of a thread's records. Derived,
  regenerable, never authoritative. You author *in* the view; `run` reconciles it back.

## Iron rules (these are about correctness, not taste)

1. **Never hand-edit a record file, and never edit a card's body in a thread.** The body
   is hashed into the id; changing it breaks the address. If you edit a card body in a
   view, `run`/`render` will restore it from the record — your edit is discarded by design.
2. **Mint your reply with `record`, then re-emit its frame — no `ctrl+o`.** Your turn-final
   reply IS a card, and `record` is the *only* path to it. Compose the body as clean markdown and
   pipe it through `record`; it content-addresses the bytes, writes the immutable record,
   re-renders the view, and prints the `render_tui` callout — the `┏━ … ┃ … ┗━ enc:v1 <id>` frame:
   ```
   printf '%s' "your reply body" | python3 _system/stream.py record --author claude-tui --reply-head
   ```
   Then **stream that exact frame as your message** — the `┃` rail binding every line — so the
   operator reads the bound callout directly, with no `ctrl+o`. The body between the rails == the
   stored record == `<id>`, by construction; the footer hash is the receipt — strip the rails, hash
   the body, it equals `<id>`, or it isn't this card. Two failures, both forbidden: (a) leaving the
   frame only in the collapsed Bash result (the `ctrl+o` trap — re-emit it as your message);
   (b) typing a separate, embellished prose twin (the card body must be exactly what sits inside
   the bars). There is **no Stop hook** scraping the transcript — that was a misfeature, it raced,
   and it's gone (ARCHITECTURE.md §6); a reply you don't `record` simply isn't carded, and the
   dashboard's reply-debt shows the unanswered head. To re-show an existing card use
   `render-tui --id <id>` (omit `--id` for the head); author into another thread/persona with
   `--view notes/<t>.md` / `--author <name>`. Optional `--flair "◈ …"` sets the italic header
   glance-line — keep it to **3–6 words**, never a summary (the body carries the detail). Never
   transcribe a card by hand.
3. **The store is append-only.** New cards only. To remove something, that's the operator's
   call (delete the record file, then `render --write` the view).
4. **No autonomous loops.** You reply when asked. The `Summon` button is the only API path
   and it fires on an explicit human click. Do not wire up anything that replies on a timer
   or on file-change without the operator saying yes to that specific loop. (Mirroring an
   already-spoken turn — your reply or the operator's prompt — into a card is *capture*, not a
   reply: it generates nothing and calls no model. That's the capture layer, not a loop.)

## The verbs (all `python3 _system/stream.py <verb>`)

| verb | what it does |
|---|---|
| `record` | single-source emit: stdin body → record + re-render + echo the card |
| `run --view <t>` | reconcile one thread: fold typed-in text → cards, restore edited bodies, re-render |
| `validate` | re-hash every record in every thread; check reply links resolve |
| `render --view <t> --write` | rebuild a view from its records (discards unsaved edits) |
| `scan` | vault-wide: flag every thread that has drifted, write `.stream/dirty.json` |
| `id` | print the enc:v1 id of a body on stdin |

`DASHBOARD.md` (vault root) is the live status view — daemon, dirty threads, reply debt.

The watcher (Layer 1: button → `run`) runs as an **independent systemd user service**
(`exo-watch.service`) — *not* a Claude Code child, so it survives the TUI closing or crashing,
restarts on failure, and with linger survives logout/reboot. Manage it with
`_system/daemon.sh {status|restart|stop|logs}`; setup lives in `_system/config/daemon.md`. Capture
is separate and always-on — the prompt hook runs *inside* Claude Code, so operator prompts are
carded whether or not the daemon is up; replies you mint yourself via `record` (rule #2).

## Managing yourself

Config you may edit lives under `_system/config/` — settings, skills, API keys, CSS profiles.
Read `_system/config/README.md` for how. Mirror any apparatus change into the README/ARCHITECTURE
if it changes how a human sets the vault up.
