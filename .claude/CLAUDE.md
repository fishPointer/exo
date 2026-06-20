# Operating contract — exo (stream-cards vault)

You are an agent working in a **stream-cards** vault: an append-only, content-addressed
thread store. Read `_system/ARCHITECTURE.md` once if you haven't; it's short.

## The one model you must hold

- **Cards are the truth.** `_system/data/cards/<id>.md` — immutable, one file per card in a single
  **global pool**, `id = sha256("enc:v2\n" + sha256(normalize(body)) + "\n" + (reply_to or "ROOT"))`
  — a full 64-hex **Merkle-DAG** node name (the parent is part of the address; an 8-char prefix is
  display only). The id IS the content *in its causal context*: same body + same parent → one card;
  same body + **different** parent → a different card. No thread partition: the same (body, parent) in
  two threads is ONE pooled card, cited twice.
- **Threads are manifests.** `_system/data/threads/<thread>.md` — a thread is *second-order*:
  `{root, render}`, its membership **derived** as `subtree(root)` (root + all `reply_to`-descendants),
  resolved live from the pool. No id-list. The view `notes/<thread>.md` (flat, no subfolders) is a
  *rendering* of the manifest against the pool — derived, regenerable, never authoritative. You author
  *in* the view; `run` reconciles it back.

## Iron rules (these are about correctness, not taste)

1. **Never hand-edit a pooled card file (`_system/data/cards/<id>.md`), and never edit a card's
   body in a thread.** The body is hashed into the id; changing it breaks the address. If you edit
   a card body in a view, `run`/`render` will restore it from the pool — your edit is discarded by design.
   (That is card *bodies* only — your uncommitted draft below the staging `---` is **not** a card; every
   plain re-render carries the staging zone verbatim — the substrate. Only the explicit flask/**Restore**
   button (`render --hard`) dissolves it, and only because you clicked it.)
2. **Mint your reply with `record`, then re-emit its frame — no `ctrl+o`.** Your turn-final
   reply IS a card, and `record` is the *only* path to it. Compose the body as clean markdown and
   pipe it through `record`; it content-addresses the bytes, writes the immutable record,
   re-renders the view, and prints the `render_tui` callout — the `┏━ … ┃ … ┗━ enc:v2 <short>` frame
   (`<short>` is the 8-char display prefix of the full 64-hex id):
   ```
   printf '%s' "your reply body" | python3 _system/stream.py record --author claude-tui --reply-head
   ```
   Then **stream that exact frame as your message** — the `┃` rail binding every line — so the
   operator reads the bound callout directly, with no `ctrl+o`. The body between the rails == the
   stored record, by construction; the footer hash is the receipt — strip the rails, hash the body
   **with its `reply_to`** (`enc:v2`), and you get the full id this `<short>` prefixes, or it isn't
   this card. **Divergence is forbidden** — the frame you stream MUST be the verbatim, untruncated
   stdout of `record`/`render-tui`, byte-for-byte, or you have forged the receipt. Three failures, all
   forbidden: (a) leaving the frame only in the collapsed Bash result (the `ctrl+o` trap — re-emit it
   as your message); (b) typing a separate, embellished prose twin (the card body must be exactly what
   sits inside the bars); (c) **truncating the frame** — never pipe `record`/`render-tui` through
   `tail`/`head`/`sed`. If the full frame isn't in front of you, you will reconstruct it from memory and
   abridge it — that is exactly how three carded replies came to diverge from their terminal frames. Let
   it print whole, then copy it verbatim; to re-show a card, `render-tui --id <id>` (untruncated) and
   emit exactly that. There is **no Stop hook** scraping the transcript — that was a misfeature, it raced,
   and it's gone (ARCHITECTURE.md §6); a reply you don't `record` simply isn't carded, and the
   dashboard's reply-debt shows the unanswered head(s) — one per open lane. To re-show an existing card use
   `render-tui --id <id>` (omit `--id` for the head); author into another thread/persona with
   `--view notes/<t>.md` / `--author <name>`. Optional `--flair "◈ …"` sets the italic header
   glance-line — keep it to **3–6 words**, never a summary (the body carries the detail). Never
   transcribe a card by hand.
3. **The store is append-only.** New cards only. Membership is *derived* (a thread is `subtree(root)`),
   so there's no per-thread id to drop — to remove something, that's the operator's call: delete the
   pooled card to retire it everywhere (its descendants then dangle until re-pointed), then
   `render --write` the view.
4. **No autonomous loops.** You reply when asked. The `Summon` button is the only API path
   and it fires on an explicit human click. Do not wire up anything that replies on a timer
   or on file-change without the operator saying yes to that specific loop. (Mirroring an
   already-spoken turn — your reply or the operator's prompt — into a card is *capture*, not a
   reply: it generates nothing and calls no model. That's the capture layer, not a loop.)

## The verbs (all `python3 _system/stream.py <verb>`)

| verb | what it does |
|---|---|
| `record` | single-source emit: stdin body → record + re-render + echo the card |
| `run --view <t>` | reconcile one thread: fold typed-in text → cards (a draft beneath a card's `^caret` replies to THAT card — fan-out), gel staging posts that quote a `pull` scaffold, restore edited bodies, re-render |
| `annotate --view <t>` | harvest in-body `` `…` `` sigil notes from edited cards into fish reply cards quoting the excerpt; restores the hosts (deterministic capture, no LLM) |
| `pull --view <t>` | extract code-highlighted excerpts from the cards into ``` codeblocks below the `---` (scrub above / append below); idempotent — re-running does nothing |
| `gel` (inside `run`/`fold`) | each `---`-separated staging post embedding a `pull` scaffold → one fish quote-reply card; the codeblock becomes a nested callout in the quoted author's style, prose kept |
| `fork --from <id> --as <t>` | new thread = the reply-subtree rooted at a card — writes a `{root, render}` derive manifest, resolved live from the pool; no cards copied. (`clone` is **gone** under enc:v2 — a thread IS its subtree, so a second name can't diverge.) |
| `validate` | re-hash every card under `enc:v2` (`hash(body, reply_to)`); check every reply link resolves (globally); assert the reply graph is **acyclic** (hard fail). The edge is *in* the id, so a rewrite is just a hash mismatch — no separate edge digest. Self-polices a v1 stray |
| `render --view <t> --write` | rebuild the cards from manifest + the pool; the staging draft below `---` is **carried verbatim** (only in-view card-body edits are discarded) |
| `render … --write --hard` | the flask/**Restore** button: discriminate every local change, then dissolve in-view edits AND the staging draft, rebuilding canonical — the deliberate wash (overrides the carry; diffs first, never silent) |
| `scan` | vault-wide: flag every thread that has drifted, write `.stream/dirty.json` |
| `bump` | the heartbeat: reconcile every dirty thread (no scrub) + print the reply-debt queue — **every fish leaf** (one owed head per open lane; concurrent terminals branch the thread) — with each head's text |
| `id` | print the `enc:v2` id of a body on stdin (`--reply-to <id>` for a reply's address; omit for a root) |
| `locate` | resolve a stdin **excerpt** → its source card id(s) by pool **substring-scan** (`contains`, **punctuation/emphasis-tolerant** via `_cmp_fold` — for matching only). The v1 O(1) `exact` tier is gone: an id commits to its parent, so a bare body can't be hashed to an id. A shared span lists every match (ambiguous, never guessed) |

`DASHBOARD.md` (vault root) is the live status view — daemon, dirty threads, reply debt.

## Bump — your heartbeat

`bump` is your clock cycle, and it's a **reflex, not a procedure**. When the operator says
**bump** (or `/bump`): run `python3 _system/stream.py bump` — it reconciles every dirty thread
(folds staged drafts into cards; code-highlights survive for `pull`), refreshes `DASHBOARD.md`,
and prints the **reply-debt** queue — **every fish leaf**, one owed head per open lane (concurrent terminals branch the thread) — with each owed head's text. Then `record` one reply per head
it lists and re-emit the frame (rule #2). That's it — no scanning, no re-validating, no narrating
the steps; the verb already put the rails in front of you. Clean queue → say "no debt" and stop.
It is **not** an autonomous loop — it fires only on an explicit bump, like `Summon` (rule #4).
The `/bump` skill is the same two moves.

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
