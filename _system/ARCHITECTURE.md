# Architecture

What this is, why it's shaped this way, and where it breaks. Read once; it's the whole model.

This is a **stream-cards** vault: an append-only, content-addressed store of conversation
threads that a team appends to from multiple devices, asynchronously, with no server.

---

## 1. One idea

> A thread is a pile of immutable cards. A card's name is the hash of its body. The thread
> you read is a *rendering* of that pile — derived, throwaway, rebuildable at any time.

Everything below falls out of that.

```
_system/records/<thread>/<id>.md   ← SOURCE OF TRUTH. immutable. id = hash(body).
notes/<thread>.md                  ← VIEW. a rendering of the pile. derived. disposable.
```

You read and type in the **view**. The tooling reconciles the view back into **records**.
Records never lie; views can be rebuilt from records byte-for-byte.

---

## 2. The load-bearing contract: `enc:v1`

```
id   = sha256(normalize(body))[:8]
normalize(body):  NFC the unicode → rstrip each line → join with "\n"
                  → strip outer newlines → append exactly one "\n"
```

The id **is** the content. Two consequences, both deliberate:

- **Idempotence.** The same body always yields the same id. Recording it twice is a no-op.
  This is why concurrent, offline, multi-device writes don't conflict (§5).
- **Tamper-evidence.** If a body ever stops hashing to its filename, the card is corrupt and
  `validate` says so. The hash is a receipt: *what you see is what's stored, or it isn't the
  same card.*

`normalize` must stay byte-stable across editors and OSes or the whole scheme rots.
`_system/test_golden.py` pins it. **Do not touch `normalize` without re-running the golden
tests.**

---

## 3. The four layers (each one optional below the next)

```
  ┌─ Layer 3  prompt capture  _system/capture_prompt.py    (operator prompt → fish card)
  ├─ Layer 2  Obsidian UI     .obsidian/plugins/exo-ribbon (buttons → sentinel file)
  ├─ Layer 1  daemon          _system/watch.py             (sentinel → runs Layer 0)
  └─ Layer 0  the spine       _system/stream.py            (pure, deterministic, stdlib)
```

**Layer 0 is the product.** `stream.py` is a stdlib-only, deterministic CLI. No daemon, no
plugin, no network. You can run the entire system from the command line. Everything above is
convenience that can be absent without breaking what's below it:

- No Obsidian? Use the CLI.
- No daemon? Use the CLI (the buttons just won't do anything).
- No API key? Everything works except the Summon button.

That layering is the robustness story. The fragile parts (a GUI, a long-running process, a
network call) are all *optional shells* around a core that is none of those things.

The daemon is installed as an **independent systemd user service** (`_system/daemon.sh install`),
not a child of the shell that launched it — so closing or crashing Claude Code can't take it down;
it restarts on failure and (with linger) survives logout/reboot. Hosts without a systemd user
session fall back to a detached `setsid`+`nohup` process. Either way it acts only on an explicit
button click (a new nonce in `.stream/trigger.json`) — no autonomous loop. Setup and verification:
`config/daemon.md`.

### The verbs (Layer 0)

| verb | does |
|---|---|
| `record` | **the primitive.** stdin body → write record → re-render view → echo the card. Every card is born here, exactly once. |
| `run` | reconcile one thread: fold typed-in text into cards, restore edited bodies from records, re-render, clear the dirty flag. |
| `validate` | re-hash every record in every thread; check reply links resolve within their thread. |
| `render --write` | rebuild a view from its records (records win; discards unsaved edits). |
| `scan` | vault-wide: flag every drifted thread, write `.stream/dirty.json`. |
| `id` | print the enc:v1 id of a body on stdin. |
| `dashboard` | compile `.stream/` state → `DASHBOARD.md`. |

### Why a sentinel file between plugin and daemon (Layer 2 ↔ 1)

The Obsidian plugin writes `.stream/trigger.json` (a nonce + action); the daemon polls for a
new nonce and runs the matching verb. No `child_process`, no localhost port, no shell from
the plugin. Plugin and daemon never need to be up at the same instant. It's the most
loosely-coupled, cross-platform, hard-to-break channel available.

### Why the LLM call lives at the edge (Layer 1 only)

`stream.py` is deterministic — same input, same output, always. The one non-deterministic
thing in the system, the API call, is quarantined in `watch.py` (`Summon`). Its output is
piped *straight back into* `stream.py record`, so even the agent's reply is captured at its
source, byte-for-byte, exactly like a human's. **No component is both stateful/networked and
load-bearing.**

### Capture: the prompt by hook, the reply by `record` + re-emit

Both ends reach the thread through the single-source `record`, but by **different paths**, because
only one of them has a clean source. The prompt's only source is the harness, so a hook grabs it;
the reply's source is the agent itself, so the agent pipes it straight in — no hook, no scrape.

- **prompts → `fish` cards** — `capture_prompt.py` (UserPromptSubmit, Layer 3) mirrors every
  operator prompt the instant it's sent — out of the agent's discretion (the fix for "you forgot
  to publish some of my inputs"). Harness-injected text on the same channel (task-notifications,
  system reminders, slash-command echoes) is **not the operator**, so it's skipped.
- **the reply → one `claude-tui` card, minted at source** — the agent pipes its reply body
  through `record` (capture-*at-source*, like the daemon's `claude -p | record`): one clean body
  in, a content-addressed card written, and the `render_tui` callout — `┏━ … ┃ … ┗━ enc:v1 <id>` —
  printed. The agent then **re-emits that exact frame as its terminal message**, so the operator
  reads the bound callout directly (no `ctrl+o`) and the footer hash is the receipt. No transcript,
  no scrape, no race; the card body == what's inside the bars, by construction. There is **no Stop
  hook** — an earlier build had one and it raced (§6); `record` is the whole reply path now, so a
  reply the agent doesn't `record` simply isn't carded (the dashboard's reply-debt flags it).

This is **mirroring/at-source authoring, not generation** — `record` copies the agent's own piped
bytes, the prompt hook the operator's; both stay clear of the no-autonomous-loop rule (only `Summon`
hits the API, on a click). The summon lane records its own reply as `claude-api` (red) and sets
`STREAM_SUMMON`; content-addressing dedupes any overlap to one card.

---

## 4. Records are partitioned by thread

`_system/records/<thread>/<id>.md` — one directory per thread. A card physically cannot leak
into another thread's view, and the **same short body in two threads is two files** (same id,
different directories), not a hash collision that silently drops one. ("ok" in two threads is
common; this is not a corner case.) Replies (`reply_to`) resolve *within* a thread.

This is the one place the design departs from the original single-thread proof, and it's why
`load_records`/`write_record` take a thread directory. `test_golden.py` checks the isolation
property directly.

---

## 5. Multi-device, multi-person, no server

Sync is **Obsidian Sync** (or any folder sync). Git distributes the *apparatus*; Obsidian
Sync distributes the *content*. They don't overlap (see `.gitignore` and the root README).

Why it doesn't corrupt under concurrent editing:

- **Two people add cards offline.** Different bodies → different ids → different files. On
  sync they merge as two new files. No conflict. Same body → same id → same file → sync sees
  identical content → still no conflict.
- **A view file conflicts** (both edited `main.md`). The view is *derived*, so the conflict
  doesn't matter: hit **Restore** (`render --write`) on either device and the view is rebuilt
  identically from the merged record set. You never lose data to a view conflict, because the
  view was never the data.

Content-addressing turns "distributed write conflict" into "set union." That's the trick.

**One setup rule that matters:** exclude `.stream/` from Obsidian Sync. It's local,
per-machine daemon state (trigger files, the daemon's PID, the API log). Syncing it would
make one person's button click fire on everyone's machine.

---

## 6. Where it breaks (known edges, by design)

- **Hand-editing a record or a card body** breaks the address. `run`/`render` restore card
  bodies from records, so an in-view body edit is *discarded* — that's intentional, not a bug.
  To genuinely change history, delete the record file and `render --write`.
- **`normalize` drift** (an editor that rewrites line endings or unicode form) would change
  ids. The golden tests catch it; `alwaysUpdateLinks: false` in `.obsidian/app.json` stops
  Obsidian from rewriting `[[wikilinks]]` inside cards.
- **No record-level deletion verb.** Removal is a deliberate manual act (operator deletes the
  file). The store is append-only on purpose.
- **Summon is single-shot and manual.** There is no autonomous reply loop and adding one
  requires explicit operator sign-off (it's a standing rule, not an oversight).
- **A reply the agent never `record`s isn't carded.** That's the cost of capture-at-source, and
  it's the right cost. An earlier build added a Stop hook that scraped the turn-final reply back
  out of Claude Code's transcript — but a Stop hook fires *as* that file is being flushed, so the
  read raced the write and once carded a mid-turn line instead of the closing reply. The fix
  wasn't to mitigate the race (a settle-wait poll); it was to **delete the scrape**. Replies mint
  through `record` and the agent re-emits the frame (§3) — one producer, one consumer, no
  transcript in the loop, so there is no race to lose. The discipline (every reply goes through
  `record`) is enforced by CLAUDE.md and surfaced by the dashboard's reply-debt when a head goes
  unanswered.

---

## 7. Provenance

Distilled from the `stream-cleanroom` proof (P0–P3: `enc:v1` pinned against 115 records,
deterministic spine, render round-trip, sentinel daemon, reactive + Summon lanes). This
deployable strips it to the spine, partitions records by thread for multi-thread/team use,
makes every path self-locating, and ships empty.

Grounding: log-as-source-of-truth, content-addressing, change-data-capture, idempotence
(Kleppmann, *Designing Data-Intensive Applications*, ch. 11–12); the view-from-records split
is the same move as a materialized view over an event log.
