# Architecture

What this is, why it's shaped this way, and where it breaks. Read once; it's the whole model.

This is a **stream-cards** vault: an append-only, content-addressed store of conversation
threads that a team appends to from multiple devices, asynchronously, with no server.

---

## 1. One idea

> Cards are immutable atoms in one global pool; a card's name is the hash of its body **and its
> parent** — so the store is a true **Merkle-DAG** (`enc:v2`). A thread is *second-order* — a
> manifest that **derives** its membership from the graph (the subtree rooted at one card). The
> thread you read is a *rendering* of that manifest over the pool: derived, throwaway, rebuildable.

Everything below falls out of that.

```
_system/data/cards/<id>.md       ← THE POOL. immutable atoms. id = hash(body, reply_to). global, no partition.
_system/data/threads/<name>.md   ← MANIFEST. derive: {root, render}. membership = subtree(root). IS the thread.
notes/<thread>.md                ← VIEW. a rendering of the manifest over the pool. derived. disposable.
_system/data/ENC                 ← the pool's encoding stamp (`v2`); validate reads it.
```

You read and type in the **view**. The tooling reconciles it back into pooled **cards** and the
thread's **manifest**. Cards never lie; a view rebuilds from manifest + pool byte-for-byte. A card is
free — any thread whose root is its ancestor renders it; a thread is a lens, not a box.

---

## 2. The load-bearing contract: `enc:v2`

```
body_hash = sha256(normalize(body)).hexdigest()                 # 64 hex, fixed width
id        = sha256("enc:v2\n" + body_hash + "\n" + (reply_to or "ROOT")).hexdigest()   # 64 hex — the address
normalize(body):  NFC the unicode → fold editor-substituted punctuation to ASCII
                  (curly quotes/apostrophes/primes, NB-hyphen, NBSP — NOT emphasis *↔_)
                  → rstrip each line → join with "\n" → strip outer newlines → append one "\n"
```

The id **is** the content *in its causal context* — a **Merkle-DAG node name** (git's commit-hashes-
its-parent, applied to a thread). It is a **hash-of-hashes**: the body is committed via its own
fixed-width 64-hex hash, so body bytes never sit adjacent to the parent field — no body content can
shift the boundary or forge a parent (naive `hash(body+parent)` is injectable; rejected). The full
256-bit (64-hex) id is stored everywhere; an **8-char prefix** is human-display only (git's short-hash
trick — `[[<full64>|<short8>]]` in views, `enc:v2 <short8>` in the rail). Four consequences, all
deliberate:

- **Idempotence, parent-scoped.** Same body + **same parent** → same id; recording it twice is a
  no-op. Same body + **different parent** → a *different* card (the dedup-softening axiom: "yes" under
  two questions is two cards). The id-resolution must therefore fix the parent *before* hashing.
- **Tamper-evidence covers the edge.** Under v1 a `reply_to` rewrite was silent (the edge wasn't
  hashed) and needed a separate "soft seal" digest. Now the edge is **in** the id: rewrite it and the
  card no longer hashes to its filename — `validate` says `INVALID` on sight. No soft seal needed.
- **Acyclic by construction.** You can't compute an id inside a cycle (A's id needs B's needs A's), so
  honest minting can't make one; `validate` still asserts it (the store is hand-editable plaintext).
- **Punctuation-stable.** `normalize` folds the bytes an editor silently swaps (curly quotes, NB-
  hyphen, NBSP) **into** the address, so a smart-quoted paste hashes identically to its ASCII source.
  Emphasis `*`↔`_` is left strict (intentional content); `locate`'s `_cmp_fold` still folds it for
  *matching* only — id strict, locate lenient.

`normalize` + the `card_id` preimage must stay byte-stable across editors and OSes or the whole scheme
rots. `_system/test_golden.py` pins both (fixed point, punc-stability, the verbatim preimage, root-
domain, injection). **Do not touch `normalize` or `card_id` without re-running the golden tests.**

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
| `fork` | new thread = the reply-subtree rooted at a card — writes a `{root, render}` **derive** manifest resolved *live* from the pool. No cards copied; any future descendant of the root appears automatically. The privileged fork. |
| `validate` | re-hash every card in the pool under `enc:v2` (`hash(body, reply_to)`); check every reply link resolves (globally); assert the `reply_to` graph is **acyclic** (a hard failure). The edge is *in* the id now, so a rewrite is just a hash mismatch — no separate edge digest. Self-polices a v1 stray (8-hex can't reproduce as 64-hex). |
| `render --write` | rebuild the cards from manifest + the pool (the pool wins; in-view card-body edits discarded) — **carries the staging draft below `---` verbatim** (`_render_preserving`, §6). |
| `render --write --hard` | the flask/**Restore** button: discriminate every local change, then dissolve in-view edits AND the staging draft, rebuilding canonical. The deliberate wash — the one path that overrides the carry, and only on the operator's click. |
| `scan` | vault-wide: flag every drifted thread, write `.stream/dirty.json`. |
| `bump` | the heartbeat: reconcile every dirty thread (no scrub), refresh the dashboard, print the reply-debt queue — **every fish leaf**, one owed head per open lane — + each head's text. The agent's one-command reflex. |
| `id` | print the `enc:v2` id of a body on stdin (pass `--reply-to <id>` for a reply's address; omit for a root). |
| `locate` | reverse lookup — a stdin excerpt → its source card id(s) by **substring-scan** (the v1 O(1) `exact` tier is gone: an id commits to its parent, so a bare body can't be hashed to an id). The *content*-located counterpart to annotate/pull's *position* lookup; ambiguous spans list every match. |
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
  in, a content-addressed card written, and the `render_tui` callout — `┏━ … ┃ … ┗━ enc:v2 <short>` —
  printed. The agent then **re-emits that exact frame as its terminal message**, so the operator
  reads the bound callout directly (no `ctrl+o`) and the footer hash is the receipt (hash the body
  *with its `reply_to`* → the full id this prefixes). **Divergence is
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

### Authoring standards (what the agent writes, not how the store works)

Two house rules shape the *prose* that enters cards. **Orientation-first voice** — ground the reader
in plain, lived language before ramping into the rigorous systems-engineering voice (never open in the
deep end). And the **`/latex suite`** equation format (the suneater "formulary" lineage, named
agnostically): *no naked equations* — every `$$…$$` relation is paired with a four-column terms table
(`Symbol | Name | Units | Typical Value`) that is a superset of every symbol in it. An entry is authored
as a nested `[!latex]` callout, so it renders as a boxed formulary panel *inside* whatever card carries
it — a **render class, not a persona**: producing one is a capability every author has (modular, not
fused), and the box is pure presentation (the `latex` CSS class) over a body that hashes like any other.
Both ship in the core distribution as skills under `.claude/skills/` (`latex-suite/SKILL.md` carries the
full contract + a worked entry).

---

## 4. Cards pool globally; threads are manifests

`_system/data/cards/<id>.md` — one global, content-addressed **pool**, no per-thread partition. The
**same body in two threads is ONE pooled card**, cited by both threads' manifests — content-addressing
finishing its job (one body → one id → one file), where the old layout kept two copies in two dirs.

A thread is a **manifest**, `_system/data/threads/<name>.md` — frontmatter `{root: <id>, render:
scroll|head}`. Membership is **derived, never stored**: a thread = `subtree(root)` = `root` + all its
transitive `reply_to`-descendants, resolved *live* from the global graph. There is **no id-list** — that
denormalized copy (which could disagree with the pool) is deleted. `fork --from <id> --as <name>`
writes a new ref rooted at any card: promote a branch into its own thread without copying. `render:` is
the doc-tier seam (only `scroll` implemented) — `a doc is a thread rendered at its head`.

`load_records(thread)` resolves the manifest root → the subtree from the pool; `write_record(card,
thread)` appends the card to the pool (written once) and the FIRST card into a rootless thread
bootstraps the root. `records_dir(thread)` returns the thread *name*, not a directory. Replies
(`reply_to`) are a single **global** graph and resolve across the pool, not within a partition.

Why derive: a thread can't drift from the pool (there's no second copy to disagree), and the model is
clean — a thread IS a subtree. The cost is the v1 `clone` "two id-lists diverge independently" semantic
**ceases to exist** (a second name over the same root can never diverge), so `clone` was **dropped**;
two people on one thread just both add cards to the same subtree. `test_golden.py` [3] pins the dedup
split (same body+parent → one card; same body+different parent → two) and [9] pins fork (subtree,
no re-addressing).

(There is no legacy migration: the burn-and-rebuild cutover to `enc:v2` left a single clean scheme —
`_migrate_if_needed`, the `records/` auto-convert, and every "migrate first" guard were deleted.)

---

## 5. Multi-device, multi-person, no server

Sync is **Obsidian Sync** (or any folder sync). Git distributes the *apparatus*; Obsidian
Sync distributes the *content*. They don't overlap (see `.gitignore` and the root README).

Why it doesn't corrupt under concurrent editing:

- **Two people add cards offline.** Different bodies → different ids → different files in the pool.
  On sync they merge as two new files. No conflict. Same body → same id → same file → sync sees
  identical content → still no conflict.
- **Membership merges by the graph.** A thread's manifest is just `{root, render}`; two devices each
  add new cards (different bodies/parents → different ids → different pool files, no conflict), and the
  thread's subtree(root) re-derives to include both — nothing to merge in the manifest itself. (The
  brief *draft* window two people might edit at once is advisory **presence**, a pending layer — not a lock.)
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
    `render --write`, a fork, a sync-triggered re-render on another device — can never clobber a
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
- **No deletion verb.** Removal is a deliberate manual act: delete the pooled card to retire it
  everywhere (its descendants then dangle until re-pointed). Membership is derived, so there is no
  per-thread id to drop. The store is append-only on purpose. (One internal exception: `_drop_pooled`,
  the interrupt-spam supersede + the summon-placeholder swap — not a general verb.)
- **Manifests resolve live and can't dangle** (membership is `subtree(root)` from the pool). The one
  edge: deleting a non-leaf pooled card leaves its descendants with a `reply_to` that no longer
  resolves — `validate`'s `referential` check flags it.
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
  and capture bind to *this* terminal's lane tip. Under `enc:v2` the parent is **in the id**, so a
  *guessed* parent is a *wrong id*: `record --reply-head` with no resolvable lane tip **refuses to mint**
  (surfaces the head as debt) rather than misattribute a reply. `capture` never refuses (a prompt is
  never dropped) and falls back to the head — safe now, because the (body, parent) address means the
  same prompt in two lanes is two distinct cards, not a silent cross-lane dedup. Reply-debt lists
  **every fish leaf**, one owed head per open lane. (A `SessionStart` lane-seed for a brand-new
  terminal's first card remains a deliberate follow-up; the refuse-to-mint path covers the gap safely.)
- **The edge is now IN the id (the hard seal — `enc:v2` shipped).** `id = hash(body, reply_to)`, a true
  Merkle-DAG: a card's name commits to its parent (git's commit-hashes-its-parent). A `reply_to`
  rewrite is no longer silent — the card stops hashing to its filename, so `validate` flags it. The v1
  "soft seal" (a separate edge-digest in `validate`) is therefore **gone** — redundant once the edge is
  hashed. This was a one-way door (re-addressing cascades to every descendant), taken by a deliberate
  burn-and-rebuild on operator sign-off, after the three questions were answered (repair: none/burn;
  dedup: softens; threat: full Merkle-DAG). The full contract is preserved at
  `_system/safekeeping/_enc-v2-contract.md` (the build spec + as-built notes alongside it).

---

## 7. Provenance

Distilled from the `stream-cleanroom` proof (P0–P3: content-addressing pinned against 115 records,
deterministic spine, render round-trip, sentinel daemon, reactive + Summon lanes). This deployable
strips it to the spine, pools cards globally with threads as derive-manifests for multi-thread/team
use, makes every path self-locating, and ships empty. It then took the one-way door the cleanroom
held open — folding `reply_to` into the address (`enc:v2`, the Merkle-DAG) by a clean burn-and-
rebuild, so the store proves a card *and its causal ancestry* as a unit.

Grounding: log-as-source-of-truth, content-addressing, change-data-capture, idempotence
(Kleppmann, *Designing Data-Intensive Applications*, ch. 11–12); the view-from-records split
is the same move as a materialized view over an event log.
