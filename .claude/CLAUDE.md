# Operating contract — exo (stream-cards vault)

You are an agent working in a **stream-cards** vault: an append-only, content-addressed
thread store. Read `_system/ARCHITECTURE.md` once if you haven't; it's short.

## The one model you must hold

- **Records are the truth.** `notes/records/<thread>/<id>.md` — immutable, one per card,
  `id = sha256(normalize(body))[:8]`. The id IS the content. Partitioned by thread.
- **Threads are views.** `notes/threads/*.md` — a rendering of a thread's records. Derived,
  regenerable, never authoritative. You author *in* the view; `run` reconciles it back.

## Iron rules (these are about correctness, not taste)

1. **Never hand-edit a record file, and never edit a card's body in a thread.** The body
   is hashed into the id; changing it breaks the address. If you edit a card body in a
   view, `run`/`render` will restore it from the record — your edit is discarded by design.
2. **To say something in a thread, pipe it through the recorder. Once.**
   ```
   echo "your reply body" | python3 _system/stream.py record --author claude --reply-head --view notes/threads/<t>.md
   ```
   The recorder writes the record, re-renders the view, and echoes the card's TUI frame.
   **That echoed frame IS your message — do not also write a prose copy.** Authoring it
   twice is how the thread and the record drift apart. The hash is the receipt: the body
   you piped in is exactly what's stored.
3. **The store is append-only.** New cards only. To remove something, that's the operator's
   call (delete the record file, then `render --write` the view).
4. **No autonomous loops.** You reply when asked. The `Summon` button is the only API path
   and it fires on an explicit human click. Do not wire up anything that replies on a timer
   or on file-change without the operator saying yes to that specific loop.

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

## Managing yourself

Config you may edit lives under `_system/config/` — settings, skills, API keys, CSS profiles.
Read `_system/config/README.md` for how. Mirror any apparatus change into the README/ARCHITECTURE
if it changes how a human sets the vault up.
