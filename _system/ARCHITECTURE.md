# Architecture

What this is, why it's shaped this way, and where it breaks. Read once; it's the whole model.

This is a **stream-cards** vault: an append-only, content-addressed store of conversation
threads that a team appends to from multiple devices, asynchronously, with no server.

---

## 1. One idea

> Cards are immutable atoms in one global pool; a card's name is the hash of its body. A thread is
> *second-order* — a manifest that **includes** cards (by id-list, or a query) in an order. The
> thread you read is a *rendering* of that manifest over the pool: derived, throwaway, rebuildable.

Everything below falls out of that.

```
_system/data/cards/<id>.md       ← THE POOL. immutable atoms. id = hash(body). global, no partition.
_system/data/threads/<name>.md   ← MANIFEST. inclusion (id-list, or subtree(root)) + order. IS the thread.
notes/<thread>.md                ← VIEW. a rendering of the manifest over the pool. derived. disposable.
```

You read and type in the **view**. The tooling reconciles it back into pooled **cards** and the
thread's **manifest**. Cards never lie; a view rebuilds from manifest + pool byte-for-byte. A card is
free — cited by any number of manifests; a thread is a lens, not a box.

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

Because `normalize` *is* the address, an editor that smart-punctuates a copied span (straight
quotes → curly, hyphen → non-breaking hyphen, `*emph*` → `_emph_`) yields **different bytes → a
different id**, so a hand-pasted excerpt won't dedupe or `locate` against its straight-ASCII
source. The fix that does **not** re-address the pool is a **compare-only** fold inside `locate`
(`_cmp_fold`): smart punctuation is folded for *matching* only, never hashed. Folding it into
`normalize` itself would re-address every existing card — that is the **`enc:v2`** change
(`id = hash(body, reply_to)` + a punctuation-stable normalize), a deliberate one-way door held
behind operator sign-off (§6), never a quiet bump.

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
| `annotate` | harvest in-body <code>`…`</code> sigil notes from edited cards into `fish` reply cards that quote the annotated excerpt as a nested callout, then restore the hosts. Deterministic capture, like `fold` — no LLM. |
| `pull` | extract code-highlighted excerpts from the cards into ``` codeblocks below the `---` barline — drafting scaffolds to compose against. Scrub above / append below; idempotent; scaffolds survive *incidental* reconciles (record/capture/bump), and gel on `run`/`fold`. |
| `gel` | (not a CLI verb — runs inside `run`/`fold`) each `---`-separated staging post that embeds a `pull` scaffold folds into one `fish` quote-reply card: the codeblock becomes a nested callout in the quoted card's *author* style, the surrounding prose is kept, reply_to = the quoted card. |
| `fork` | new thread = the reply-subtree rooted at a card — writes a `subtree` manifest resolved *live* from the pool. No cards copied; any future descendant of the root appears automatically. The privileged fork. |
| `clone` | copy a thread's manifest to a new name — two manifests over one pool, sharing history then diverging as each gets new cards. The "two people on one thread" answer. |
| `validate` | re-hash every card in the pool; check every reply link resolves (globally) and every manifest id is pooled; assert the `reply_to` graph is **acyclic**; print the **edge digest** (soft-seal fingerprint of the `child→parent` set — a recorded digest makes a later edge rewrite detectable, with no re-addressing). |
| `render --write` | rebuild the cards from manifest + the pool (the pool wins; in-view card-body edits discarded) — **carries the staging draft below `---` verbatim** (`_render_preserving`, §6). |
| `render --write --hard` | the flask/**Restore** button: discriminate every local change, then dissolve in-view edits AND the staging draft, rebuilding canonical. The deliberate wash — the one path that overrides the carry, and only on the operator's click. |
| `scan` | vault-wide: flag every drifted thread, write `.stream/dirty.json`. |
| `bump` | the heartbeat: reconcile every dirty thread (no scrub), refresh the dashboard, print the reply-debt queue — **every fish leaf**, one owed head per open lane — + each head's text. The agent's one-command reflex. |
| `id` | print the enc:v1 id of a body on stdin. |
| `locate` | reverse lookup — a stdin excerpt → its source card id(s). Whole body → `exact` (the hash, O(1)); partial span → `contains` (pool substring-scan, since a partial can't be hashed to an id). The *content*-located counterpart to annotate/pull's *position* lookup; ambiguous spans list every match. |
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
  reads the bound callout directly (no `ctrl+o`) and the footer hash is the receipt. **Divergence is
  forbidden** (CLAUDE.md rule #2): the re-emitted frame must be the *verbatim, untruncated* stdout —
  never piped through `tail`/`head`/`sed`. Truncate it and you'll reconstruct the frame from memory
  and abridge it into a forgery whose body no longer hashes to the footer `<id>` (this happened, and
  the operator caught the terminal frame diverging from the Obsidian card on three replies; the store
  was clean, the chat message was the forgery). No transcript, no scrape, no race; the card body ==
  what's inside the bars, by construction. There is **no Stop hook** — an earlier build had one and it
  raced (§6); `record` is the whole reply path now, so a reply the agent doesn't `record` simply isn't
  carded (the dashboard's reply-debt flags it).

This is **mirroring/at-source authoring, not generation** — `record` copies the agent's own piped
bytes, the prompt hook the operator's; both stay clear of the no-autonomous-loop rule (only `Summon`
hits the API, on a click). The summon lane records its own reply as `claude-api` (red) and sets
`STREAM_SUMMON`; content-addressing dedupes any overlap to one card.

### Bump: the reply cadence

The agent's turns are paced by **`bump`** — the operator's nudge that it's the agent's beat. One
bump = `dashboard → reply-debt → generate the owed replies across every thread`. Reply-debt is **every fish leaf** — a fish card no card replies to — so concurrent terminals each surface their own owed head (a single global-latest head would bury every lane but one). It is the read side
of the same no-loop contract as `Summon`: the agent acts only when bumped, never on a timer. The
operator points at what they want addressed by **code-highlighting** a span (`` `like this` ``, inline
like bold); `annotate` lifts that span into a `[!quote]` card, and the next bump answers it. The
procedure is the `/bump` skill; the rule is in CLAUDE.md.

---

## 4. Cards pool globally; threads are manifests

`_system/data/cards/<id>.md` — one global, content-addressed **pool**, no per-thread partition. The
**same body in two threads is ONE pooled card**, cited by both threads' manifests — content-addressing
finishing its job (one body → one id → one file), where the old layout kept two copies in two dirs.

A thread is a **manifest**, `_system/data/threads/<name>.md` — frontmatter `include: list | subtree`
(+ `root:` for subtree) over an ordered set of card-ids:

- **`list`** — an explicit, ordered id-set. The default; equals the old behaviour, now made *data*
  instead of a folder. New cards append their id.
- **`subtree`** — `root: <id>`; the inclusion is `root` + all its `reply_to`-descendants, resolved
  *live* from the global graph. This is `fork`: promote any branch into its own thread without copying.

`load_records(thread)` resolves the manifest → the pool; `write_record(card, thread)` appends the card
to the pool (written once) and adds its id to the manifest. `records_dir(thread)` now returns the thread
*name*, not a directory. Replies (`reply_to`) are a single **global** graph and resolve across the pool,
not within a partition.

Why this shape: a thread becomes diffable and queryable like a card (diff the inclusion; `clone` = copy
it; a query *is* an inclusion); and the old partition — the one place the design refused to let the
global hash be global — is *deleted*, not maintained. `test_golden.py` [3] pins the dedup (one pooled
card, two manifests cite it) and [9] pins fork (subtree) + clone (independent divergence).

**Migration is lazy and lossless.** A legacy `_system/records/<thread>/` converts to pool + manifest on
first touch (`_migrate_if_needed`), idempotent, and the old `records/` tree is **left in place as a
backup** — not authoritative. The doctor's `manifests resolve to pooled cards` check guards the one new
fault: a `list` id with no pooled card is silently dropped by `load_records`, so it never shows as
render-drift.

---

## 5. Multi-device, multi-person, no server

Sync is **Obsidian Sync** (or any folder sync). Git distributes the *apparatus*; Obsidian
Sync distributes the *content*. They don't overlap (see `.gitignore` and the root README).

Why it doesn't corrupt under concurrent editing:

- **Two people add cards offline.** Different bodies → different ids → different files in the pool.
  On sync they merge as two new files. No conflict. Same body → same id → same file → sync sees
  identical content → still no conflict.
- **Manifests merge by union.** A thread's manifest is an ordered id-list; two devices appending
  different cards diverge into a set-union of ids, each a real pooled card — so the thread re-renders
  cleanly from the merged manifest. (Coordinating the brief *draft* window two people might edit at
  once is advisory **presence**, a pending concurrency layer — not a lock.)
- **A view file conflicts** (both edited `main.md`). The view is *derived*, so the conflict
  doesn't matter: any re-render rebuilds the card region identically from the merged pool + manifest.
  You never lose data to a view conflict, because the view was never the data — and a *plain* re-render
  (automatic, or a sync-triggered regenerate on another device) **carries the staging draft below the
  `---` verbatim** (§6), so it can't clobber an uncommitted draft. The flask/**Restore** button is the
  one deliberate exception — `render --hard`, the explicit wash that *does* dissolve the draft, because
  the operator asked for canonical.

Content-addressing turns "distributed write conflict" into "set union." That's the trick.

**One setup rule that matters:** exclude `.stream/` from Obsidian Sync. It's local,
per-machine daemon state (trigger files, the daemon's PID, the API log). Syncing it would
make one person's button click fire on everyone's machine.

---

## 6. Where it breaks (known edges, by design)

- **Hand-editing a pooled card body** breaks the address. `run`/`render` restore card bodies from
  the pool, so an in-view body edit is *discarded* — that's intentional, not a bug. To genuinely
  change history, delete the pooled card and `render --write`.
  - **The re-render is non-destructive — the substrate.** A view has two regions: the CARD region
    (above the staging `---`, derived — rebuilt from the pool, in-view edits discarded) and the
    STAGING region (below it — the operator's uncommitted draft). Every re-render regenerates the
    cards but carries the staging **verbatim** (`_render_preserving`), so a plain regenerate — a CLI
    `render --write`, a fork/clone, a sync-triggered re-render on another device — can never clobber a
    draft. Drift detection compares the card region only; the staging is never drift. The one path that
    *does* dissolve the staging is the flask/**Restore** button (`render --hard` / `_render_hard`), and
    only because the operator deliberately asked: it diffs the local changes, then washes the edits and
    the draft back to canonical.
  - **Staged work is also never scrubbed by an append.** Both append paths (`record` a reply,
    `capture` a prompt) run a *reconcile first* (`_reconcile_view`): fold floating drafts into fish
    cards and file any whole new card typed in the view — THEN append, THEN re-render, with
    code-highlights re-applied and `pull` scaffolds preserved. So a draft you left unsaved before
    bumping is carded, not erased. (Folding into cards is the append paths' behaviour; a plain
    `render --write` does not fold — it just carries the staging, per the substrate above.) (Whole-line `…` sigil notes are NOT harvested by the
    reconcile — that needs an explicit `annotate` or `run`; see the deferred reconcile-coherence
    work. Composed `pull` scaffolds gel only on `run`/`fold`, not on an incidental reconcile.)
- **`normalize` drift** (an editor that rewrites line endings or unicode form) would change
  ids. The golden tests catch it; `alwaysUpdateLinks: false` in `.obsidian/app.json` stops
  Obsidian from rewriting `[[wikilinks]]` inside cards.
- **No deletion verb.** Removal is a deliberate manual act: drop a card's id from a thread's manifest
  to remove it *there*, or delete the pooled card to retire it everywhere. The store is append-only on
  purpose.
- **A manifest id with no pooled card** is silently dropped from the view by `load_records`, so it
  can't surface as render-drift — the doctor checks `manifests resolve to pooled cards` directly.
  (`subtree` manifests resolve live and can't dangle.)
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
- **Concurrent terminals branch one thread (handled).** A reply used to bind to the single
  global-latest card, so N terminals collapsed into one near-linear chain. Fixed with a **per-terminal
  lane pointer** (`.stream/sessions/<sid>.json`, keyed by the Claude session id): `record --reply-head`
  and capture bind to *this* terminal's lane tip, falling back to the global head only when the session
  is unknown (pure-CLI / single-terminal unchanged). Reply-debt then lists **every fish leaf**, one owed
  head per open lane. A brand-new terminal's *first* card still falls back to the global head (no lane
  tip yet) — a `SessionStart` seed would close that, left as a deliberate follow-up.
- **The edge graph is mutable; the nodes aren't (soft-sealed).** A card *body* is tamper-evident (it
  hashes to its id); a `reply_to` *edge* lives in frontmatter, so a rewrite is silent — `validate` only
  checked that edges *resolve*. The reversible **soft seal** closes most of that gap: `validate` now
  asserts the graph is **acyclic** and prints an **edge digest** (sha256 of the `child→parent` set), so
  a recorded digest makes a later rewrite *detectable* with no re-addressing. The **hard seal** —
  `enc:v2`, `id = hash(body, reply_to)`, a true Merkle-DAG where the parent is part of the address — is a
  one-way door (re-addressing cascades to every descendant), so it stays behind operator sign-off and
  three open questions (repair boundary / dedup axiom / threat tier): develop up to it, never flip it
  silently.

---

## 7. Provenance

Distilled from the `stream-cleanroom` proof (P0–P3: `enc:v1` pinned against 115 records,
deterministic spine, render round-trip, sentinel daemon, reactive + Summon lanes). This
deployable strips it to the spine, pools cards globally with threads as manifests (fork/clone/query
as manifest ops) for multi-thread/team use, makes every path self-locating, and ships empty.

Grounding: log-as-source-of-truth, content-addressing, change-data-capture, idempotence
(Kleppmann, *Designing Data-Intensive Applications*, ch. 11–12); the view-from-records split
is the same move as a materialized view over an event log.
