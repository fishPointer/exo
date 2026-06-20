# enc:v2 — build spec & to-do (the Merkle-DAG cutover)

**Status:** ratified, building. This is the apparatus-side copy of the contract the operator
signed off on; the full review doc + council derivation is preserved verbatim at
`_system/safekeeping/_enc-v2-contract.md` (and the source conversation at
`_system/safekeeping/main-thread-preburn.md`, council card `539f25e8`). Build to **this**;
deviate only with a noted reason.

> The cleanroom rebuild folds the reply edge into the address: the store becomes a true
> **Merkle-DAG** — each card's id commits to its parent, exactly like a Git commit commits to its
> parent. This is a **one-way door** (re-addressing cascades to every descendant), which is why it
> is gated, ratified, and done by burn-and-rebuild rather than in-place migration.

---

## 0 · Decisions (locked — do not re-litigate)

| # | decision | source |
|---|---|---|
| id width | **full sha256, 256-bit** stored; 8-char prefix for human display only | `19fdf2af` `539f25e8` `9198a49d` |
| repair | **none** — burn & rebuild clean, one scheme, no v1/v2 coexistence | `19fdf2af` |
| dedup | **softens** — same body + different parent = different card (accepted price) | `19fdf2af` `cf7ef18a` |
| structure | **full Merkle-DAG** — `id = hash(body, parent)` | `19fdf2af` |
| normalize | **punctuation-stabilized** — folded into v2 (the free re-address window) | `fc9f7856` |
| manifests | **derive** membership from the graph; drop the stored id-list | `fc9f7856` ("3. derive") |
| burn | **kept exactly as-is** — `validate` self-polices a v1 stray; no quiesce choreography | `fc9f7856` `9198a49d` |

Claim ceiling: v2 = **integrity** (a body *and its causal ancestry* are tamper-evident as a unit).
**Not** authenticity — `author` is forgeable plaintext, no signatures. Out of scope.

---

## 1 · The id scheme — the load-bearing contract (freeze in a golden test FIRST, code second)

### 1.1 `normalize_v2(body)` — punctuation-stable canonical body
1. NFC the unicode.
2. **Fold editor-substituted punctuation to canonical ASCII** (bytes an editor/paste silently swaps —
   "the same character", safe to fold into the address):
   - curly double quotes `“ ” „ ‟` → `"`
   - curly single quotes / apostrophes `‘ ’ ‚ ‛` (and primes `′ ″`) → `'` / `"`
   - typographic / non-breaking hyphen `‐ ‑` → `-`
   - non-breaking space `U+00A0` → space
3. rstrip each line; join with `\n`; strip leading/trailing blank lines; append exactly one `\n`.

**NOT folded into the id:** markdown emphasis `*` ↔ `_`. Those are legitimate content (`reply_to`,
`snake_case`) and an intentional author choice. `locate`'s `_cmp_fold` keeps folding them for
**matching only**. Id stays strict; locate stays lenient — deliberate split.

### 1.2 `card_id(body, reply_to)` — hash-of-hashes (git's move, injection-proof)
```
body_hash  = sha256(normalize_v2(body).encode("utf-8")).hexdigest()   # 64 hex, fixed width
parent_tok = reply_to if reply_to else "ROOT"                         # a real 64-hex id, OR "ROOT"
preimage   = "enc:v2\n" + body_hash + "\n" + parent_tok
id         = sha256(preimage.encode("utf-8")).hexdigest()             # 64 hex — the address
```
- **Injection-proof:** body committed via its own fixed-width hash, so body bytes never sit adjacent
  to the parent field — no body content can shift the boundary or forge a parent. (Naive
  `hash(body+parent)` is forgeable through the body — rejected.)
- **Root domain-separated:** `"ROOT"` is outside the hex alphabet, so `parent=None` can't alias a
  child whose parent is an all-hex id. (Empty-string root rejected.)
- **Version-tagged:** `enc:v2\n` cryptographically separates v1/v2 ids; a future v3 is a clean step.

### 1.3 Display vs. structure
- **Structural** (filenames `<id>.md`, `reply_to`, manifest `root`): full 64-hex everywhere.
- **Human display**: 8-char prefix. Rail footer `┗━ enc:v2 a1b2c3d4`; citations via Obsidian alias
  `[[<full64>|a1b2c3d4]]` (short shows, full resolves). A prefix clash is cosmetic; the address is
  full-width.

### 1.4 Golden pins for §1 (write these FIRST)
1. `normalize_v2` fixed point + **punc-stability**: curly-quoted body and straight-quoted body → identical id.
2. The `preimage` string for a known `(body, parent)` pinned **verbatim** (any re-delimit fails loud).
3. **Root domain:** `id(body, None) ≠ id(body, <any real id>)`.
4. **Injection:** a body ending in a hex-shaped tail + `parent=None` ≠ that tail-as-parent.
5. `id` reproduces from the stored body (the receipt property, now over the pair).

---

## 2 · Data model — node + ref (derive)

- **NODE** — immutable bytes, `id = card_id(body, parent)`, one file per id in one global pool.
- **REF** — a named pointer + render policy: `{ name, root: <id>, render: scroll|head }`. A thread and
  a doc are the same ref; they differ only by `render`.

**Membership is derived, never stored.** A thread = `subtree(root)` = the root + all transitive
`reply_to`-descendants, resolved live from the graph. **No id-list.** `fork --from <id> --as <name>`
= a new ref rooted at that card. Seed `render:` now (only `scroll` implemented) — the seam the
doc-tier hangs off; one key now, no schema migration later.

*Open build detail (flagged, not blocking):* `clone`'s v1 "diverge two id-lists" semantic doesn't
exist under derive — a thread *is* its subtree. Lean: drop `clone`, or redefine as a pure root-alias.
A fresh thread with no cards yet has `root: ` empty; the first recorded card becomes the root.

---

## 3 · Kill list (the burn deletes these; v2 ships *smaller* than v1)

- [ ] **Legacy `_system/records` migration** — `_migrate_if_needed`, `RECORDS_ROOT`, every
      "migrate first" guard in `load_records`/`write_record`/`validate`/`clone`. Biggest dead-code cut.
- [ ] **Soft-seal edge digest** (`_edge_digest`) — redundant once the edge is *in* the id.
- [ ] **The v1/v2 version boundary** — rejected. One scheme; no `if enc==1` branch anywhere.
- [ ] **`locate`'s O(1) `exact` tier** — structurally dead (can't hash a bare excerpt without its
      parent). Locate is substring-scan only. Retire the "O(1) exact" claim in docs.
- [ ] **The `thread:` card field** — vestigial; a pooled card belongs to no thread.
- [ ] **The `channel:` card field** — dead; always `"stream"`, never branched on.
- [ ] **The global-head reply fallback** — under v2 a *guessed* parent is a *wrong id* (see §4).

## 4 · Keep / change list

- [ ] **`_find_cycles` / acyclicity → HARD `validate` failure** (and must not infinite-loop on a
      hand-written cycle).
- [ ] **`write_record` → assert body-equality on the dedup skip.** "same id ⇒ same content" must
      *raise* on a real collision, never silently no-op.
- [ ] **Lane pointer → kept, but the *guess* dies.** Bind capture/record to *this terminal's* lane
      tip = the explicit, correct parent v2 needs. Unknown-session fallback must **not** mint against
      the global head: use a **`SessionStart` lane-seed** (now **required**) or **refuse to mint and
      surface the head as debt**. Explicit parent always; no silent wrong-parent.
- [ ] **Idempotence across a lane advance → fixed.** The `cid not in records` dedup check must hash
      with the *resolved* parent (re-running `record` after the lane moved must not duplicate). Pin it.
- [ ] **`pull` / `gel` / `annotate`** — kept, incl. the nested-fence robustness.
- [ ] **Render modes** (`_render_preserving` / `_render_hard` / `_render_keep_scaffolds`) — kept; the
      derived-card / authored-staging split is orthogonal to the id scheme.
- [ ] **Id-matching regexes** — every hardcoded `[0-9a-f]{8}` (`_read_manifest`, `cmd_fork`,
      `_compose_post`) → `[0-9a-f]{64}`; `_HEADER_RE`/`_ANCHOR_RE` already accept `+`-length.

## 5 · Golden-test rethink (rewrite `test_golden.py` to this)

- [1] enc fixed point → **REWRITE** as `(body, parent)` pair fixed point + punc-stability.
- [2/2b/2c] render/escape → **KEEP, refixture** (every hardcoded id + `card_id(body)` recomputes).
- [3] dedup → **SPLIT:** (3a) same body + same parent → one card; (3b) **new** — same body + diff
      parent → two distinct ids (the softening axiom).
- [4–8, 10] → **KEEP, refixture** ids; strengthen [8] (branch id ≠ root id).
- [9] fork → **HARDEN:** forking does **not** re-address the subtree (parent-in-preimage is the
      *global* parent, not local-None).
- [11] locate → **REDESIGN** without the O(1) `exact` tier (substring-scan, parent-agnostic).
- [12] lane → **REFIXTURE** + same-body-different-parent leaves both surface as debt.
- [13] punct-fold → **KEEP** (locate match tolerance).
- [14] soft seal → **REWRITE** as acyclic-by-construction; **drop** the edge-digest assertions.
- [15] nested-fence → **KEEP.**
- **NEW:** canonical-encoding pin, root-token, injection (the §1.4 set).

## 6 · Burn procedure (kept simple)

1. Preserve design notes → `_system/safekeeping/` (done), then `/burn` as today (backs up to
   `.stream/burns/<ts>/` first — that path is the only copy of the raw pool).
2. Stamp the fresh pool `_system/data/ENC = v2`; `validate` refuses a pool whose cards don't
   reproduce under v2 (already true via the hash check; the stamp makes the error *say* "v1 stray").
3. Rebuild on the v2 spine; `validate` green = clean cutover.

---

## Build sequence (the live to-do)

- [x] **S1 · Spine + golden pins.** `normalize` (punc-stable) + `card_id(body, reply_to)`, full 64-hex,
      `short_id`; §1.4 pins in golden [1].
- [x] **S2 · Derive manifests** (`{root, render}`, subtree-only, no id-list); first card bootstraps the
      root; `render: scroll` seeded.
- [x] **S3 · Kill list (§3) + keep/change (§4):** legacy migration / edge-digest / `thread:` / `channel:`
      removed; hard acyclicity; `write_record` body-equality assert; idempotence-with-resolved-parent;
      id regexes widened to 64-hex; `_drop_pooled` for the interrupt-spam/summon retract.
- [x] **S4 · Golden tests rewritten (§5) → 15/15 green.**
- [x] **S5 · Docs:** ARCHITECTURE §1/§2/§3/§4/§5/§6/§7, CLAUDE.md model+rules+verbs, initialize skill,
      doctor labels, watch.py summon placeholder all updated to enc:v2.
- [x] **S6 · Burned → rebuilt → stamped `_system/data/ENC = v2` → `validate` + `doctor` + end-to-end
      CLI smoke all green.** (Burn ran right after the spec was stored, per operator; empty seed = 0
      cards, so validate was trivially green until v2 cards minted; re-validated after S1–S5.)

### As-built deviations (the two flagged open details, decided)
- **`clone` DROPPED** (not kept as a root-alias). Under derive a second name over one root can never
  diverge, so the verb had no coherent semantic — removed entirely (verb, dispatch, golden [9]).
- **SessionStart lane-seed DEFERRED** — the contract offered "lane-seed (required) **OR** refuse to
  mint." Chose the refuse arm: `record --reply-head` with no resolvable lane tip **refuses** (surfaces
  the head as debt); `capture` never refuses (a prompt is never dropped) and falls back to the head —
  safe under v2 because (body, parent) addressing makes the same prompt in two lanes two distinct cards.
  A `SessionStart` seed remains a clean future nicety, no longer load-bearing.

### Out of scope for this cutover (separate owed work, preserved in safekeeping)
- The **doc-tier** (`render: head`, distillation/precipitation verb, name-pinned doc-links) —
  `_dual-channel-primitive.md`. v2 only **seeds** the `render:` key.
- **personas.md + portable skills** — `_personas-review/`.
- **CSS theme-independence** (cards render identically regardless of Obsidian theme) — raised in
  `9ffe34ca` pt.5; renka reported a pass already. Separate from the id scheme.
