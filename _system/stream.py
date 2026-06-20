#!/usr/bin/env python3
"""
stream.py — the deterministic core of the stream-cards backend (enc:v1).

Stdlib only. No external dependencies, no install step, no daemon required to
use the CLI. Self-locating: works from wherever the vault is cloned.

TWO REPRESENTATIONS (DDIA derived data, ch. 11-12)
  _system/records/<thread>/<id>.md   SOURCE OF TRUTH — one immutable,
                    content-addressed card, partitioned by thread.
  notes/<thread>.md   the VIEW — a materialized projection that renders a
                    thread's records as Futaba-style callout posts. Regenerable;
                    never authoritative. Authoring happens *in* the view, so the
                    "run button" reconciles the view back to the records.

THE LOAD-BEARING CONTRACT  (enc:v1)
  id = sha256(normalize(body))[:8]
  normalize(body):  NFC the unicode -> rstrip each line -> join with "\n"
                    -> strip outer newlines -> append exactly one "\n".
  The id IS the checksum. Verified byte-exact against all records.

THE DIRTY FLAG
  A thread that has drifted from its records (operator edits) is flagged in its
  FRONTMATTER (`stream: dirty` + `stream_pending: N`) — never in card bodies, so
  hashes are untouched. Clean = keys absent (a clean view stays byte-exact).
  The full change-set is written to a per-note sidecar `.stream/changesets/<slug>.json`.

COMMANDS  (deterministic; the generative half is the LLM agent, not this file)
  id        read a body on stdin -> print its enc:v1 id
  validate  re-hash every record + referential integrity on reply_to
  render    regenerate one view from records (clears its dirty flag) [--check|--write]
  diff      one view -> change-set; writes sidecar + sets flag  [--quiet = preview only]
  extract   restore mutated interiors + persist self-consistent new cards
  run       THE RUN BUTTON, one thread: check -> scrub -> render -> clear flag
  scan      vault-wide dirty pass: flag every thread + write .stream/dirty.json

See ARCHITECTURE.md for the DDIA / Git / Datomic grounding.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import pathlib
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta

# ── paths (all relative to the vault root = parent of _system/) ──────────────
# The vault is self-locating: ROOT is derived from THIS file, never hardcoded,
# so the whole thing works from wherever it is cloned (Linus rule: no absolute
# paths baked into the binary).
ROOT = pathlib.Path(__file__).resolve().parent.parent
NOTES_DIR = ROOT / "notes"                  # the thread VIEWS live here, flat (no subfolders)
RECORDS_ROOT = ROOT / "_system" / "records" # vestigial legacy path (kept only for burn.py's backup sweep)
CARDS_DIR = ROOT / "_system" / "data" / "cards"     # the global content-addressed card POOL (id.md)
THREADS_DIR = ROOT / "_system" / "data" / "threads" # thread MANIFESTS: derive root + render policy
ENC_STAMP = ROOT / "_system" / "data" / "ENC"       # the pool's encoding version (`v2`); validate reads it
THREAD_DIR = NOTES_DIR                       # views are notes/<thread>.md, directly under notes/
DEFAULT_VIEW = NOTES_DIR / "main.md"         # used when --view is omitted
STREAM_DIR = ROOT / ".stream"               # local daemon/runtime state (never synced)
CHANGESETS_DIR = STREAM_DIR / "changesets"
DIRTY_INDEX = STREAM_DIR / "dirty.json"
API_LOG = STREAM_DIR / "api-log.jsonl"
SUMMON_INFLIGHT = STREAM_DIR / "summon-inflight.json"
SESSIONS_DIR = STREAM_DIR / "sessions"      # per-terminal lane pointers (local; never synced)
DASHBOARD = ROOT / "DASHBOARD.md"           # the derived status view (root)


def records_dir(view_stem: str) -> str:
    """A thread is a NAME. Cards live in one global content-addressed pool
    (`data/cards/<id>.md`); a thread is a manifest (`data/threads/<name>.md`) that includes
    card-ids in order. Kept named `records_dir` so the verb layer is unchanged — it now hands
    back the thread name, and `load_records`/`write_record` resolve it through the manifest."""
    return view_stem

# ════════════════════════════════════════════════════════════════════════════
#  enc:v2 : the canonicalization + Merkle-DAG address contract
# ════════════════════════════════════════════════════════════════════════════
#  id = sha256("enc:v2\n" + sha256(normalize(body)) + "\n" + (reply_to or "ROOT"))
#  The reply edge is folded INTO the address: the store is a true Merkle-DAG (a card's
#  name commits to its parent, like a git commit commits to its parent). Full 256-bit
#  (64-hex) ids stored everywhere; an 8-char prefix is human-display ONLY. Pinned
#  byte-for-byte by test_golden.py — a single wrong character silently re-addresses the
#  whole pool, so the encoding is frozen there first.

# Punctuation FOLDED INTO the address (enc:v2's stabilizer): bytes an editor/paste
# silently swaps for "the same character", so folding them is safe and makes a
# smart-quoted paste hash identically to its straight-ASCII source. Markdown emphasis
# (`*`↔`_`) is deliberately NOT here — that's legitimate, intentional content; `locate`'s
# `_cmp_fold` still folds it for MATCHING only (id strict, locate lenient — see §1.1).
_NORM_FOLD = str.maketrans({
    "“": '"', "”": '"', "„": '"', "‟": '"',          # curly double quotes -> "
    "‘": "'", "’": "'", "‚": "'", "‛": "'",          # curly single quotes / apostrophes -> '
    "′": "'", "″": '"',                                        # primes -> ' "
    "‐": "-", "‑": "-",                                        # typographic / non-breaking hyphen -> -
    " ": " ",                                                  # non-breaking space -> space
})

ID_RE = r"[0-9a-f]{64}"          # a structural enc:v2 id (full sha256). Display prefixes are 8-char.
ROOT_TOKEN = "ROOT"              # the parent sentinel for a root card — disjoint from the hex alphabet,
                                 # so parent=None can never alias a child whose parent is a real id.


def normalize(body: str) -> str:
    """enc:v2 canonical body. NFC -> fold editor-substituted punctuation to ASCII -> rstrip each
    line -> join with "\\n" -> strip outer blank lines -> append exactly one "\\n". Punctuation-stable:
    a smart-quoted paste hashes identically to its straight-ASCII source. Byte-stable across
    editors/OS or content-addressing breaks; pinned by test_golden.py. Do NOT touch without re-running
    the golden tests."""
    s = unicodedata.normalize("NFC", body).translate(_NORM_FOLD)
    lines = [ln.rstrip() for ln in s.split("\n")]
    s = "\n".join(lines).strip("\n")
    return s + "\n"


def card_id(body: str, reply_to: str | None = None) -> str:
    """The enc:v2 content address (a Merkle-DAG node name):

        body_hash = sha256(normalize(body))            # 64 hex, fixed width
        id        = sha256("enc:v2\\n" + body_hash + "\\n" + (reply_to or "ROOT"))

    Hash-of-hashes (git's move): the body is committed via its OWN fixed-width 64-hex hash, so body
    bytes never sit adjacent to the parent field — no body content can shift the boundary or forge a
    parent (naive `hash(body+parent)` is injectable; rejected). The parent is part of the address, so
    the same body under two different parents is two distinct cards (the v2 dedup-softening axiom),
    and a rewritten edge is a different id (tamper-evident, no soft seal needed). Returns the full
    64-hex id; `[:8]` is display only."""
    body_hash = hashlib.sha256(normalize(body).encode("utf-8")).hexdigest()
    parent_tok = reply_to if reply_to else ROOT_TOKEN
    preimage = "enc:v2\n" + body_hash + "\n" + parent_tok
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def short_id(cid: str) -> str:
    """The 8-char human-display prefix of a full 64-hex id (git's short-hash trick). Display ONLY —
    every stored/structural use (filenames, reply_to, manifest root, anchors) is the full id."""
    return cid[:8]


# ════════════════════════════════════════════════════════════════════════════
#  record model + IO
# ════════════════════════════════════════════════════════════════════════════

_FM_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)


def _split_frontmatter(text: str) -> tuple[dict, str, str]:
    """Return (fm_dict, fm_block_verbatim, body). Forgiving line parser;
    strips one layer of surrounding quotes from scalar values."""
    m = _FM_RE.match(text)
    if not m:
        return {}, "", text
    block = m.group(1)
    body = text[m.end():]
    fm: dict = {}
    for line in block.splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        k, _, v = line.partition(":")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        fm[k.strip()] = v
    return fm, block, body


# ── ordered frontmatter (round-trip preserving) + the dirty flag ─────────────

def _parse_fm_ordered(text: str):
    """Parse frontmatter into an ordered list of (key|None, value). Non-kv
    lines (blanks, comments) are preserved verbatim as (None, raw)."""
    m = _FM_RE.match(text)
    if not m:
        return [], text
    pairs = []
    for line in m.group(1).split("\n"):
        if ":" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition(":")
            pairs.append((k.strip(), v.strip()))
        else:
            pairs.append((None, line))
    return pairs, text[m.end():]


def _fm_inner(pairs) -> str:
    return "\n".join(v if k is None else f"{k}: {v}" for k, v in pairs)


def _fm_reattach(pairs, body: str) -> str:
    return f"---\n{_fm_inner(pairs)}\n---\n{body}"


def _set_key(pairs, key, value):
    out, done = [], False
    for k, v in pairs:
        if k == key:
            out.append((key, value)); done = True
        else:
            out.append((k, v))
    if not done:
        out.append((key, value))
    return out


def _without(pairs, keys):
    return [(k, v) for k, v in pairs if k not in keys]


DIRTY_KEYS = ("stream", "stream_pending")

# Pure control signals — `capture` skips carding these so triggers don't litter
# the thread. Whole-prompt exact match only (case-insensitive).
CONTROL_WORDS = {"bump", "go", "run it"}


def set_flag(view_path: pathlib.Path, pending: int) -> None:
    """Write the dirty signpost into the view's FRONTMATTER (never card bodies,
    so card hashes are untouched). pending<=0 clears it (clean = keys absent).
    No-op write if the bytes don't change (avoids needless mtime churn)."""
    text = view_path.read_text(encoding="utf-8")
    pairs, body = _parse_fm_ordered(text)
    if pending > 0:
        pairs = _set_key(pairs, "stream", "dirty")
        pairs = _set_key(pairs, "stream_pending", str(pending))
    else:
        pairs = _without(pairs, DIRTY_KEYS)
    new = _fm_reattach(pairs, body)
    if new != text:
        view_path.write_text(new, encoding="utf-8")


def find_stream_views(root: pathlib.Path = ROOT):
    """Every note that declares `type: stream` in frontmatter = a thread view."""
    skip = {".obsidian", ".stream", ".git", "_system", "records", "__pycache__"}
    out = []
    for p in sorted(root.rglob("*.md")):
        if any(part in skip for part in p.relative_to(root).parts):
            continue
        fm, _, _ = _split_frontmatter(p.read_text(encoding="utf-8"))
        if fm.get("type") == "stream":
            out.append(p)
    return out


@dataclass
class Card:
    id: str
    author: str = "claude"
    captured_at: str = ""
    reply_to: str | None = None
    flair: str = ""
    body: str = ""

    @property
    def computed_id(self) -> str:
        return card_id(self.body, self.reply_to)


def _card_path(cid: str) -> pathlib.Path:
    return CARDS_DIR / f"{cid}.md"


def _manifest_path(thread: str) -> pathlib.Path:
    return THREADS_DIR / f"{thread}.md"


def _card_text(card: Card) -> str:
    """A pool card's on-disk form — frontmatter + body. Only (body, reply_to) is hashed into the id;
    the rest is metadata. `reply_to` IS part of the address under enc:v2, so it is the one frontmatter
    field that can't be silently rewritten without `validate` catching it (a changed edge = a changed
    id = a hash mismatch)."""
    fm = ["---", f"hash: {card.id}", f"author: {card.author}",
          f"captured_at: {card.captured_at}"]
    if card.reply_to:
        fm.append(f"reply_to: {card.reply_to}")
    if card.flair:
        fm.append(f'flair: "{card.flair}"')
    fm.append("---\n")
    body = card.body if card.body.startswith("\n") else "\n" + card.body
    return "\n".join(fm) + body


def _load_card(cid: str) -> Card | None:
    p = _card_path(cid)
    if not p.exists():
        return None
    fm, _, body = _split_frontmatter(p.read_text(encoding="utf-8"))
    return Card(id=fm.get("hash") or cid, author=fm.get("author", "claude"),
                captured_at=fm.get("captured_at", ""),
                reply_to=(fm.get("reply_to") or None), flair=fm.get("flair", ""),
                body=body)


def _read_manifest(thread: str) -> dict:
    """Parse a thread manifest -> {root, render}. Membership is DERIVED, not stored: a thread is the
    subtree rooted at `root` (root + all its transitive reply_to-descendants, resolved live from the
    global pool). No id-list — that denormalized copy could disagree with the pool, so it's gone
    (the enc:v2 derive model). `render` is scroll|head (the doc-tier seam; only scroll is implemented)."""
    p = _manifest_path(thread)
    if not p.exists():
        return {"root": None, "render": "scroll"}
    fm, _, _ = _split_frontmatter(p.read_text(encoding="utf-8"))
    return {"root": fm.get("root") or None, "render": fm.get("render", "scroll")}


def _write_manifest(thread: str, man: dict) -> None:
    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    fm = ["---", "type: thread-manifest", f"thread: {thread}",
          f"root: {man.get('root') or ''}", f"render: {man.get('render', 'scroll')}", "---", ""]
    _manifest_path(thread).write_text("\n".join(fm), encoding="utf-8")


def _all_pool_cards() -> dict[str, Card]:
    out: dict[str, Card] = {}
    if CARDS_DIR.exists():
        for p in sorted(CARDS_DIR.glob("*.md")):
            c = _load_card(p.stem)
            if c:
                out[p.stem] = c
    return out


def _subtree_ids(root: str, cards: dict) -> set:
    """root + every card reachable downward from it via reply_to (its descendants)."""
    kids: dict = {}
    for c in cards.values():
        if c.reply_to:
            kids.setdefault(c.reply_to, []).append(c.id)
    seen, stack = set(), [root]
    while stack:
        x = stack.pop()
        if x not in seen:
            seen.add(x)
            stack += kids.get(x, [])
    return seen


def load_records(thread: str) -> dict[str, Card]:
    """Resolve a thread (its manifest root) against the global pool -> {id: Card} = the subtree rooted
    at `root`. A thread with no root (fresh/empty) is {}. Membership is derived live from the reply
    graph — there's no stored id-list to drift (enc:v2)."""
    man = _read_manifest(thread)
    if not man["root"]:
        return {}
    pool = _all_pool_cards()
    return {i: pool[i] for i in _subtree_ids(man["root"], pool) if i in pool}


def write_record(card: Card, thread: str) -> pathlib.Path:
    """Append a card to the global pool (content-addressed — written once). The thread includes it
    automatically iff it descends from the thread root (derive model); the FIRST card into a rootless
    thread bootstraps the root. On a dedup skip (the id already on disk), ASSERT the stored body
    matches — "same id ⇒ same content" must RAISE on a real collision, never silently no-op (§4)."""
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    p = _card_path(card.id)
    if p.exists():
        existing = _load_card(card.id)
        if existing and normalize(existing.body) != normalize(card.body):
            raise SystemExit(f"COLLISION at {card.id}: a different body already occupies this id — "
                             f"refusing to overwrite (enc:v2 write-equality assert)")
    else:
        p.write_text(_card_text(card), encoding="utf-8")
    man = _read_manifest(thread)
    if not man["root"]:
        man["root"] = card.id                        # the first card bootstraps the thread root
        _write_manifest(thread, man)
    return p


def _drop_pooled(thread: str, card: Card) -> None:
    """Retract a pooled card — the interrupt-spam supersede path ONLY (cmd_capture). Deletes the file;
    if it was the thread root, re-roots to the card's parent so the subtree stays connected (or clears
    it, letting the replacement re-bootstrap the root). NOT a general delete verb — the store is
    append-only; removal is a deliberate operator act (ARCH §6)."""
    _card_path(card.id).unlink(missing_ok=True)
    man = _read_manifest(thread)
    if man.get("root") == card.id:
        man["root"] = card.reply_to
        _write_manifest(thread, man)


# ════════════════════════════════════════════════════════════════════════════
#  P2 — render : records -> view  (the materialized projection)
# ════════════════════════════════════════════════════════════════════════════

def _sort_key(card: Card) -> str:
    """Deterministic thread order: chronological by captured_at."""
    return re.sub(r"\s*\([a-z]{3}\)", "", card.captured_at)


# ── view-only HTML-escape ─────────────────────────────────────────────────────
# A card body is raw markdown, hashed verbatim into its id. But when projected
# into the Obsidian *callout*, a bare angle-bracket token (e.g. <task-notification>)
# is parsed by Obsidian as an inline HTML tag — and an unclosed one swallows the
# rest of the callout and collapses the box. So render_card escapes &,<,> in the
# body, but only OUTSIDE *protected* regions, and parse_view applies the exact
# inverse — keeping render->parse a loss-free round-trip (the body still hashes to
# its id). Records stay untouched.
#
# Protected = code spans/fences (so `<id>` in backticks renders verbatim) AND math
# spans, both inline $...$ and display $$...$$ (so an inequality like $a < b$ keeps
# its literal '<': MathJax does not decode &lt;, and escaping it there breaks the
# math render). Angle brackets inside math/code cannot open an HTML tag, so leaving
# them literal is safe — the box only collapses on bare tags in prose, which still
# escape.
#
# Only '<' opens an HTML tag, so only '<' (plus '&', to keep the unescape unambiguous)
# is escaped. '>' is left ALONE: a bare '>' cannot open a tag, and escaping it would
# turn a body line that STARTS with '>' — an intentional nested callout / blockquote,
# which is the excerpt-quote shape the annotation feature emits — into a literal
# "&gt;" that markdown no longer nests.
_VIEW_ESCAPES = (("&", "&amp;"), ("<", "&lt;"))

# Within a normal line, these spans are skipped: inline code, single-line display
# math ($$...$$), inline math ($...$). Multi-line fenced code and standalone-$$
# display blocks are handled by the line-level state machine below. None of these
# delimiters (`, $) appear in _VIEW_ESCAPES, so the protected regions are byte-
# identical before and after the transform — see _xform_outside_protected.
_PROTECTED_SPAN = re.compile(r"(`+[^`]*`+|\$\$.*?\$\$|\$[^$\n]*\$)")


def _xform_outside_protected(text: str, pairs: tuple) -> str:
    """Apply `pairs` replacements to every region NOT inside a protected span — a
    fenced code block, an inline code span, or a math span ($...$ / $$...$$). The
    delimiters bounding those regions (backticks, fence markers, $) are never in
    `pairs`, so the protected regions are byte-identical before and after — which
    makes _view_escape / _view_unescape exact inverses of each other."""
    out, in_fence, in_math = [], False, False
    for line in text.split("\n"):
        if in_fence:
            out.append(line)                         # inside code fence: verbatim
            if line.lstrip().startswith(("```", "~~~")):
                in_fence = False
            continue
        if in_math:
            out.append(line)                         # inside $$ display block: verbatim
            if line.strip() == "$$":
                in_math = False
            continue
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = True
            out.append(line)                         # fence marker: verbatim
            continue
        if line.strip() == "$$":
            in_math = True
            out.append(line)                         # display-math fence: verbatim
            continue
        segs = re.split(_PROTECTED_SPAN, line)        # odd indices = protected spans
        for k in range(0, len(segs), 2):              # even = outside any span
            s = segs[k]
            for a, b in pairs:
                s = s.replace(a, b)
            segs[k] = s
        out.append("".join(segs))
    return "\n".join(out)


def _view_escape(body: str) -> str:
    """Record body -> callout-safe text (escape &,<,> outside code/math)."""
    return _xform_outside_protected(body, _VIEW_ESCAPES)


def _view_unescape(body: str) -> str:
    """Callout-safe text -> record body (the exact inverse of _view_escape)."""
    return _xform_outside_protected(body, tuple((b, a) for a, b in reversed(_VIEW_ESCAPES)))


def render_card(card: Card) -> str:
    """One card -> its callout post block + ^anchor (view grammar §3).

    enc:v2 ids are full 64-hex, but the operator should SEE the 8-char prefix — so links use the
    Obsidian alias form `[[<full64>|<short8>]]`: the full id is the (resolvable) link target and the
    on-disk anchor, the short id is the rendered label. `parse_view` recovers the full id from before
    the `|`. The body is HTML-escaped for the callout (outside code spans) so a bare angle-bracket
    token can't open an HTML tag and collapse the box; `parse_view` reverses it, so the rendered body
    still hashes (with reply_to) to its id."""
    sid = short_id(card.id)
    head = f"> [!{card.author}] {card.author} - {card.captured_at} | [[{card.id}|{sid}]]"
    if card.reply_to:
        head += f" >> [[#^{card.reply_to}|{short_id(card.reply_to)}]]"
    head += f" <br> {card.flair}"
    body = _view_escape(normalize(card.body).rstrip("\n"))
    body_lines = [(f"> {ln}" if ln else ">") for ln in body.split("\n")]
    return "\n".join([head, *body_lines, "", f"^{card.id}"])


def render_tui(card: Card) -> str:
    """The TERMINAL chrome for a card — the third surface alongside the Obsidian
    callout and the raw record. The left rail `┃` is the TUI's `> `: strip it here
    and the body is the record verbatim (the Obsidian callout additionally HTML-
    escapes the body for safe rendering — a reversible projection; see render_card).
    The footer carries the enc:v2 id (8-char display prefix) as the receipt pointer:
    hash the body WITH its reply_to and it equals the full id this prefixes. This is the
    frame an agent's reply is presented in, so what's said == what's stored, by construction."""
    rail = "┃"
    sid = short_id(card.id)
    head = f"┏━ [{card.author}] · {sid}" + (f" ↳ {short_id(card.reply_to)}" if card.reply_to else "")
    out = [head]
    if card.flair:
        out.append(f"{rail} {card.flair}")
    out.append(rail)
    for ln in normalize(card.body).rstrip("\n").split("\n"):
        out.append(f"{rail} {ln}" if ln else rail)
    out.append(f"┗━ enc:v2 {sid}")
    return "\n".join(out)


def _nav_link(target_id: str) -> str:
    """The thread's structural 'Jump to Bottom' header link: an Obsidian in-file block link to the
    LATEST card's anchor — which sits directly above the staging input zone — wrapped in a [!nav]
    callout that both view parsers (parse_view, fold_floating) skip, so it is never captured."""
    return f"> [!nav] [[#^{target_id}|⤓ Jump to Bottom]]"


def render_view(records: dict[str, Card], fm_block: str) -> str:
    """Regenerate the whole view note from records. Pure function of the
    record set + sort key — the view is a build artifact."""
    cards = sorted(records.values(), key=_sort_key)
    body = "\n\n".join(render_card(c) for c in cards)
    out = f"---\n{fm_block}\n---\n\n" if fm_block else ""
    if cards:                                    # structural header nav -> the staging input at the
        out += _nav_link(cards[-1].id) + "\n\n"  # bottom (the latest card's anchor sits just above it)
    out += body + "\n\n---\n"   # trailing reply-zone separator (card 3adb72d6)
    return out


def _staging(text: str) -> tuple:
    """Split a view at the cards' trailing `---` separator. Returns (head, staging): head is
    the frontmatter + cards + that separator line; staging is the operator's draft zone below
    it — which may hold several `---`-separated posts and `pull` scaffolds. The separator is
    the first bare `---` AFTER the last card anchor AND after the frontmatter, so neither the
    frontmatter `---` nor any `---` a card body contains (rail-prefixed, so never bare) is mistaken
    for it. The frontmatter guard matters for an EMPTY thread (no anchors): without it the search
    would land on the frontmatter's own `---` and report the whole note as staging. (text, '') if
    there's no staging area yet."""
    lines = text.split("\n")
    anchor = max((i for i, l in enumerate(lines) if _ANCHOR_RE.match(l)), default=-1)
    fm_end = (next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), -1)
              if lines and lines[0].strip() == "---" else -1)
    start = max(anchor + 1, fm_end + 1)              # past the cards AND past the frontmatter
    sep = next((i for i in range(start, len(lines)) if lines[i].strip() == "---"), -1)
    if sep == -1:
        return text, ""
    return "\n".join(lines[:sep + 1]), "\n".join(lines[sep + 1:])


def _split_posts(staging: str) -> list:
    """Split a staging area into posts on any line that is a bare `---` (the same strip-rule
    `_staging`/`fold_floating` use — not a literal '\\n---\\n', which a padded or edge `---`
    would slip past, merging two posts into one bad card)."""
    posts, cur = [], []
    for ln in staging.split("\n"):
        if ln.strip() == "---":
            posts.append("\n".join(cur)); cur = []
        else:
            cur.append(ln)
    posts.append("\n".join(cur))
    return posts


def _max_backtick_run(s: str) -> int:
    """Length of the longest run of consecutive backticks in `s`."""
    best = run = 0
    for ch in s:
        run = run + 1 if ch == "`" else 0
        best = max(best, run)
    return best


def _fence(content: str) -> str:
    """A code-fence long enough to wrap `content` verbatim even when it embeds ``` runs —
    CommonMark closes a fence only with a run of >= its own length, so a 3-tick block inside an
    excerpt rides safely inside a >=4-tick scaffold. Used by `pull` (emit) and gel (read)."""
    return "`" * max(3, _max_backtick_run(content) + 1)


def _fence_open(line: str) -> int:
    """The fence length if `line` opens a code fence (a leading run of >=3 backticks, info string
    allowed), else 0 — so an inner, shorter fence inside an excerpt can't close the outer one."""
    s = line.strip()
    n = 0
    while n < len(s) and s[n] == "`":
        n += 1
    return n if n >= 3 else 0


def _fence_close(line: str, fence_len: int) -> bool:
    """True if `line` closes a fence of length `fence_len` — a bare run of >= that many backticks."""
    s = line.strip()
    return bool(s) and set(s) == {"`"} and len(s) >= fence_len


def _surviving_drafts(view: pathlib.Path) -> str:
    """Fenced ``` blocks anywhere in the staging area (across every `---`-separated post) that
    a re-render must KEEP — `pull` scaffolds the operator is still annotating against. Prose is
    not returned (a reconcile folds that into a card); only the fences survive, verbatim, so
    re-clicking buttons mid-draft never erases a scaffold."""
    lines = _staging(view.read_text(encoding="utf-8"))[1].split("\n")
    out, i, n = [], 0, len(lines)
    while i < n:
        if (fl := _fence_open(lines[i])):
            blk = [lines[i]]; i += 1
            while i < n and not _fence_close(lines[i], fl):
                blk.append(lines[i]); i += 1
            if i < n:
                blk.append(lines[i]); i += 1                # closing fence
            out.append("\n".join(blk))
        else:
            i += 1
    return ("\n" + "\n\n".join(out) + "\n") if out else ""


def _reapply_highlights(rendered: str, hl_by_card: dict) -> str:
    """After a reconcile re-renders cards to canonical, re-wrap each still-pending
    code-highlight in backticks (within its own card) so it survives until `pull`
    extracts it — a reconcile must not consume a highlight the operator hasn't pulled."""
    for cid, excerpts in hl_by_card.items():
        a, z = rendered.find(f"[[{cid}"), rendered.find(f"\n^{cid}\n")   # link is [[full|short]]
        if a == -1 or z == -1:
            continue
        a = rendered.find("\n", a) + 1            # skip the header line — re-wrap in the BODY only
        region = rendered[a:z]
        for exc in excerpts:
            if exc in region and f"`{exc}`" not in region:
                region = region.replace(exc, f"`{exc}`", 1)
        rendered = rendered[:a] + region + rendered[z:]
    return rendered


def _render_keep_scaffolds(view: pathlib.Path, records: dict) -> None:
    """Re-render the cards from records, but (1) re-apply any pending code-highlights so a
    reconcile doesn't consume them before `pull` runs, and (2) append the surviving
    draft-zone scaffolds below the `---` — so a reconcile/append (record, capture, run, …)
    rebuilds the cards without erasing a highlight or a `pull` scaffold. (`render --write` uses
    `_render_preserving` instead: it folds nothing but carries the WHOLE staging zone verbatim.)"""
    text = view.read_text(encoding="utf-8")
    hl_by_card = {}
    for mut in build_changeset(text, load_records(records_dir(view.stem)))["mutated"]:
        hs = [h["excerpt"] for h in _extract_highlights(mut["record_body"], mut["view_body"])]
        if hs:
            hl_by_card[mut["id"]] = hs
    rendered = _reapply_highlights(render_view(records, _clean_fm_block(view)), hl_by_card)
    view.write_text(rendered + _surviving_drafts(view), encoding="utf-8")


def _render_preserving(view: pathlib.Path, records: dict, fm_block: str) -> str:
    """The non-destructive-reconcile substrate. Rebuild the CARD region from the pool (the pool wins;
    in-view card-body edits are discarded, rule #1) but carry the STAGING region — everything below the
    cards' `---` — VERBATIM. The staging is the operator's not-yet-carded draft; a plain regenerate must
    never clobber it, including a `render --write` (Restore) fired on another device after sync. Only an
    explicit fold (`run`/`fold`/`gel`) turns staging into cards; a plain re-render just carries it.

    This is what makes the view safe to regenerate at any time: the card region is derived (rebuildable
    from records), the staging is the only authored-but-uncommitted state, and it survives untouched."""
    head = render_view(records, fm_block)                                  # cards + trailing "---"
    staging = _staging(view.read_text(encoding="utf-8"))[1] if view.exists() else ""
    return head + staging


# ════════════════════════════════════════════════════════════════════════════
#  view parser  (the CDC capture: read changes that entered through the view)
# ════════════════════════════════════════════════════════════════════════════

# The id group captures the FULL 64-hex (before any `|short` alias); the optional `|...` is the
# human display prefix and is discarded on parse. Same for the parent. So parse-back recovers the
# full structural id the card_id(body, parent) recompute needs.
_HEADER_RE = re.compile(
    r"^> \[!(?P<author>[\w-]+)\]\s+[\w-]+\s+-\s+(?P<ts>.+?)\s+\|\s+"
    r"\[\[(?P<id>[0-9a-f]+)(?:\|[0-9a-f]+)?\]\]"
    r"(?:\s+>>\s+\[\[#\^(?P<parent>[0-9a-f]+)(?:\|[0-9a-f]+)?\]\])?"
    r"\s+<br>\s+(?P<flair>.*)$"
)
_ANCHOR_RE = re.compile(r"^\^([0-9a-f]+)\s*$")
# The rendered "Jump to Bottom" header link (render_view emits it; both view parsers skip it). Matched
# by its EXACT structural form — the `[[#^<hex>|⤓` block-link — NOT a loose `[!nav]` prefix, so an
# operator who happens to start a line with `[!nav]` keeps it as content (append-only: NEVER silently
# drop operator input). render_view's nav always matches this; nothing an operator hand-types does.
_NAV_RE = re.compile(r"^>\s*\[!nav\]\s*\[\[#\^[0-9a-f]+\|⤓")


@dataclass
class ParsedCard:
    printed_id: str
    author: str
    captured_at: str
    reply_to: str | None
    flair: str
    body: str
    anchor: str | None

    @property
    def computed_id(self) -> str:
        return card_id(self.body, self.reply_to)

    @property
    def mutated(self) -> bool:
        return self.computed_id != self.printed_id


def parse_view(text: str) -> tuple[str, list[ParsedCard], list[str]]:
    """Parse a view note into (fm_block, cards, floating_text_lines).

    Floating text = any non-empty line that is NOT frontmatter, NOT inside a
    callout, NOT an ^anchor, and NOT the trailing `---` separator. That is the
    operator's sanctioned write channel — the prompt typed between the cards."""
    _, fm_block, body_text = _split_frontmatter(text)
    lines = body_text.split("\n")
    cards: list[ParsedCard] = []
    floating: list[str] = []
    i, n = 0, len(lines)

    while i < n:
        line = lines[i]
        if _NAV_RE.match(line):              # structural header nav — not a card, not floating
            i += 1
            continue
        hm = _HEADER_RE.match(line)
        if hm:
            i += 1
            body_lines: list[str] = []
            while i < n and lines[i].startswith(">"):
                bl = lines[i]
                body_lines.append("" if bl == ">" else
                                  bl[2:] if bl.startswith("> ") else bl[1:])
                i += 1
            while i < n and lines[i].strip() == "":
                i += 1
            anchor = None
            if i < n and (am := _ANCHOR_RE.match(lines[i])):
                anchor = am.group(1)
                i += 1
            cards.append(ParsedCard(
                printed_id=hm.group("id"), author=hm.group("author"),
                captured_at=hm.group("ts"), reply_to=hm.group("parent"),
                flair=hm.group("flair"),
                body=_view_unescape("\n".join(body_lines).strip("\n")),
                anchor=anchor))
        elif line.strip() in ("", "---"):
            i += 1
        else:
            floating.append(line)
            i += 1

    return fm_block, cards, floating


def build_changeset(view_text: str, records: dict[str, Card]) -> dict:
    """The run button's detect-and-distill half (deterministic, DDIA CDC)."""
    _, cards, floating = parse_view(view_text)
    mutated, new_cards, dangling = [], [], []
    seen_ids = set()
    for c in cards:
        seen_ids.add(c.printed_id)
        if c.printed_id in records:
            if c.mutated:                       # interior edited -> records win
                mutated.append({"id": c.printed_id,
                                "recomputed_id": c.computed_id,
                                "view_body": c.body,
                                "record_body": records[c.printed_id].body.strip("\n")})
        else:
            new_cards.append({"printed_id": c.printed_id,
                              "computed_id": c.computed_id,
                              "self_consistent": not c.mutated,
                              "author": c.author, "reply_to": c.reply_to,
                              "flair": c.flair, "body": c.body})
        if c.reply_to and c.reply_to not in records and c.reply_to not in seen_ids:
            dangling.append({"id": c.printed_id, "reply_to": c.reply_to})

    return {
        "summary": {"cards_in_view": len(cards), "mutated": len(mutated),
                    "new_cards": len(new_cards), "floating_lines": len(floating),
                    "dangling": len(dangling)},
        "mutated": mutated,        # restore these from records
        "new_cards": new_cards,    # extract these into records
        "floating": floating,      # route to the LLM agent as instructions
        "dangling": dangling,      # quarantine; never auto-fabricate parents
    }


# ════════════════════════════════════════════════════════════════════════════
#  command layer
# ════════════════════════════════════════════════════════════════════════════

def _resolve_view(view_arg: str | None) -> pathlib.Path:
    if not view_arg:
        return DEFAULT_VIEW
    p = pathlib.Path(view_arg)
    return p if p.is_absolute() else (ROOT / p)


def _is_thread(view: pathlib.Path) -> bool:
    """Only `type: stream` notes are threads. Guards run/render/fold/diff from
    mangling a non-thread the operator happens to have focused (e.g. hitting
    Render while viewing DASHBOARD.md — which would overwrite it as a thread)."""
    if not view.exists():
        return False
    fm, _, _ = _split_frontmatter(view.read_text(encoding="utf-8"))
    return fm.get("type") == "stream"


def _write_changeset(view: pathlib.Path, cs: dict) -> pathlib.Path:
    CHANGESETS_DIR.mkdir(parents=True, exist_ok=True)
    out = CHANGESETS_DIR / f"{view.stem}.json"
    out.write_text(json.dumps(cs, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def _pending_count(cs: dict) -> int:
    s = cs["summary"]
    return s["mutated"] + s["floating_lines"] + s["new_cards"] + s["dangling"]


def _refresh_dirty(view: pathlib.Path) -> None:
    """Keep dirty.json + the frontmatter flag current for ONE thread after any
    op that changed it — so the dashboard can't show stale drift (the bug where
    a Render dropped floating but dirty.json still listed it). Touches the drift
    signal only, not the reply-debt sidecar."""
    cs = build_changeset(view.read_text(encoding="utf-8"), load_records(records_dir(view.stem)))
    pending = _pending_count(cs)
    set_flag(view, pending)
    idx = _read_json(DIRTY_INDEX, {})
    rel = str(view.relative_to(ROOT))
    if pending > 0:
        idx[rel] = {"pending": pending, **cs["summary"]}
    else:
        idx.pop(rel, None)
    STREAM_DIR.mkdir(parents=True, exist_ok=True)
    DIRTY_INDEX.write_text(json.dumps(idx, indent=2, ensure_ascii=False), encoding="utf-8")


def _clean_fm_block(view: pathlib.Path) -> str:
    """The view's frontmatter with the dirty keys stripped (render is clean)."""
    if not view.exists():
        return "type: stream"
    pairs, _ = _parse_fm_ordered(view.read_text(encoding="utf-8"))
    return _fm_inner(_without(pairs, DIRTY_KEYS))


def cmd_id(args) -> int:
    """Print the enc:v2 id of a body on stdin. The id commits to the parent, so pass `--reply-to <id>`
    for a reply's address; omit it for a root (parent = ROOT sentinel)."""
    print(card_id(sys.stdin.read(), getattr(args, "reply_to", None)))
    return 0


# Content-MATCH folding (compare-only; never stored, never hashed). enc:v2's `normalize` already
# folds quotes / hyphens / NBSP INTO the id, so a smart-quoted paste hashes to its source. `_cmp_fold`
# is the looser MATCH form `locate`/`gel` use: it also folds markdown emphasis `_`<->`*` — which
# `normalize` deliberately does NOT (emphasis is intentional content, so it stays strict in the id).
# So the id is strict (only the "same character" punctuation folds); locate is lenient (emphasis too).
_CMP_FOLD = str.maketrans({
    "“": '"', "”": '"', "„": '"', "‟": '"',          # “ ” „ ‟  -> "
    "‘": "'", "’": "'", "‚": "'", "‛": "'",          # ‘ ’ ‚ ‛  -> '
    "′": "'", "″": '"',                                        # ′ ″ primes
    "‐": "-", "‑": "-",                                        # ‐ ‑ hyphen / non-breaking hyphen
    " ": " ",                                                       # NBSP -> space
    "_": "*",                                                            # markdown emphasis fold
})


def _cmp_fold(s: str) -> str:
    """Punctuation-tolerant form for CONTENT MATCHING ONLY — see `_CMP_FOLD`. Never hashed."""
    return s.translate(_CMP_FOLD)


def _locate(excerpt: str) -> tuple:
    """Resolve a bare EXCERPT to the source card id(s) by CONTENT — the out-of-band counterpart to
    `annotate`/`pull`, which know an excerpt's source by its POSITION in the view. Substring-scan only:
    under enc:v2 an id commits to its parent, so a bare body (no parent in hand) can't be hashed to an
    id — the v1 O(1) `exact` tier is structurally gone. Match after the same per-line rstrip the id's
    `normalize` does, through the lenient `_cmp_fold`, so copy-paste whitespace / smart punctuation /
    emphasis don't defeat it. Returns (kind, ids): kind in {'empty','contains'}; ids = [] when nothing
    matches, >1 when ambiguous (a shared span — listed, never guessed)."""
    if not excerpt.strip():
        return ("empty", [])
    needle = _cmp_fold("\n".join(l.rstrip() for l in excerpt.strip().split("\n")))
    hits = [c.id for c in sorted(_all_pool_cards().values(), key=_sort_key)
            if needle in _cmp_fold("\n".join(l.rstrip() for l in c.body.split("\n")))]
    return ("contains", hits)


def cmd_locate(args) -> int:
    """Print the id(s) of the pooled card(s) a stdin excerpt is `contains`ed in (substring-scan).
    Determine-the-source for an excerpt handed over out of band; ambiguous spans list every match."""
    kind, ids = _locate(sys.stdin.read())
    if kind == "empty":
        print("locate: empty excerpt", file=sys.stderr)
        return 1
    if not ids:
        print("locate: no pooled card contains that excerpt", file=sys.stderr)
        return 1
    for i in ids:
        print(f"{i}\t{kind}")
    if len(ids) > 1:
        print(f"locate: {len(ids)} cards match — ambiguous; quote with more context", file=sys.stderr)
    return 0 if len(ids) == 1 else 2


def _find_cycles(pool: dict) -> list:
    """Card ids whose `reply_to` chain forms a cycle. The reply graph MUST be a DAG: enc:v2
    can't hash a cycle (A's id needs B's needs A's), and a cycle is corruption regardless. Each
    card has at most one parent (a functional graph), so a chain that revisits a node loops."""
    bad = []
    for start in pool:
        seen, cur = set(), start
        while cur in pool and pool[cur].reply_to:
            cur = pool[cur].reply_to
            if cur == start or cur in seen:
                bad.append(start)
                break
            seen.add(cur)
    return sorted(set(bad))


def cmd_validate(args) -> int:
    """Re-hash every pooled card under enc:v2 (id = hash(body, reply_to)) + check every reply_to edge
    resolves + assert the reply graph is ACYCLIC (a hard failure — a cycle is unmintable by honest
    hashing and breaks the Merkle-DAG). The edge is IN the id now, so a rewritten edge is just a hash
    mismatch — no separate soft-seal digest. Self-polices a v1 stray: an 8-hex v1 id can't reproduce as
    a 64-hex hash(body, parent), so it lands in bad_hash → INVALID on sight. Vault-wide."""
    pool = _all_pool_cards()
    n = len(pool)
    enc = (ENC_STAMP.read_text(encoding="utf-8").strip() if ENC_STAMP.exists() else "(unstamped)")
    bad_hash = [(cid, c.computed_id) for cid, c in pool.items() if c.computed_id != cid]
    bad_ref = [(cid, c.reply_to) for cid, c in pool.items() if c.reply_to and c.reply_to not in pool]
    threads = sorted(p.stem for p in THREADS_DIR.glob("*.md")) if THREADS_DIR.exists() else []
    print(f"pool enc:           {enc}")
    print(f"threads:            {len(threads)}")
    print(f"records:            {n}")
    print(f"hash integrity:     {n - len(bad_hash)}/{n} reproduce their id (enc:v2: hash(body, reply_to))")
    for cid, got in bad_hash:
        print(f"  ✗ {short_id(cid)}: (body, reply_to) hashes to {short_id(got)} — corrupt or a v1 stray")
    print(f"referential:        {n - len(bad_ref)}/{n} reply_to edges resolve")
    for cid, tgt in bad_ref:
        print(f"  ✗ {short_id(cid)}: reply_to -> {short_id(tgt)} (missing from pool)")
    cyclic = _find_cycles(pool)
    n_edges = sum(1 for c in pool.values() if c.reply_to)
    print(f"acyclic:            {'yes' if not cyclic else 'NO'} ({n_edges} edges form a DAG)")
    for cid in cyclic:
        print(f"  ✗ {short_id(cid)}: reply_to chain forms a cycle")
    # causal order: captured_at must not DECREASE along a reply edge — a reply cannot predate the
    # card it answers. Append-only creation guarantees this; a delete+restore (or any out-of-band
    # re-stamp) that dates a parent AFTER its existing child violates it and silently mis-orders the
    # chronological render — and head/debt selection, which are max-by-captured_at. Flag it loud.
    noncausal = sorted(
        (c.id, c.reply_to) for c in pool.values()
        if c.reply_to in pool and _sort_key(c) < _sort_key(pool[c.reply_to])
    )
    print(f"causal order:       {n - len(noncausal)}/{n} replies dated at-or-after their parent")
    for cid, tgt in noncausal:
        print(f"  ✗ {short_id(cid)}: captured_at {pool[cid].captured_at!r} precedes parent "
              f"{short_id(tgt)} {pool[tgt].captured_at!r}")
    ok = not bad_hash and not bad_ref and not cyclic and not noncausal
    print("VALID ✓" if ok else "INVALID ✗")
    return 0 if ok else 1


def _render_hard(view: pathlib.Path, records: dict, fm_block: str, write: bool) -> int:
    """The flask/Restore button: the DELIBERATE hard reset. Discriminate every local change against the
    pool — edited card bodies, uncommitted typed-in cards, and the staged draft below the `---` — then
    dissolve ALL of it and rebuild the pure canonical view (staging is NOT carried). This is the one path
    that overrides the non-destructive substrate, and only because the operator asked for it by clicking:
    a plain/synced re-render carries the draft (you never lose work to a regenerate); the flask button is
    the explicit 'wash this note back to what the records say', diffed first so the wash is never silent."""
    text = view.read_text(encoding="utf-8") if view.exists() else ""
    cs = build_changeset(text, records)
    n_mut, n_new = len(cs["mutated"]), len(cs["new_cards"])
    staged = bool(_staging(text)[1].strip())
    rendered = render_view(records, fm_block)                  # pure canonical — the staging is dissolved
    bits = []
    if n_mut:
        bits.append(f"{n_mut} edited card bod{'y' if n_mut == 1 else 'ies'} restored")
    if n_new:
        bits.append(f"{n_new} uncommitted typed card{'' if n_new == 1 else 's'} dropped")
    if staged:
        bits.append("staged draft dissolved")
    detail = "; ".join(bits) if bits else "already canonical — nothing to dissolve"
    if not write:
        print(f"would reset {view.name}: {detail}")
        return 0
    view.write_text(rendered, encoding="utf-8")
    _refresh_dirty(view)
    print(f"reset -> {view.relative_to(ROOT)} ({detail}; cards rebuilt from the pool)")
    return 0


def cmd_render(args) -> int:
    view = _resolve_view(getattr(args, "view", None))
    if not _is_thread(view):
        print(f"{view.name}: not a stream thread — skipping")
        return 0
    records, fm = load_records(records_dir(view.stem)), _clean_fm_block(view)
    if getattr(args, "hard", False):                     # the flask button: dissolve edits + staging
        return _render_hard(view, records, fm, write=args.write)
    # non-destructive (the substrate): rebuild the cards from the pool, carry the staging draft verbatim.
    rendered = _render_preserving(view, records, fm)
    if args.write:
        view.write_text(rendered, encoding="utf-8")
        _refresh_dirty(view)
        print(f"rendered -> {view.relative_to(ROOT)} (cards rebuilt, staging kept, dirty flag cleared)")
        return 0
    if args.check:
        current = view.read_text(encoding="utf-8") if view.exists() else ""
        cs = build_changeset(current, records)           # discriminate: are the CARD bodies canonical?
        if cs["mutated"] or cs["new_cards"]:             # a real divergence — edited/new card bodies
            print("view DIFFERS from records ✗ — card bodies edited or typed-in; run `render --write`")
            return 1
        staged = bool(_staging(current)[1].strip())      # a staged draft is expected, not drift
        print("view MATCHES records ✓" + (" (staged draft present)" if staged else ""))
        return 0
    sys.stdout.write(rendered)
    return 0


def _new_thread_view(name: str) -> pathlib.Path:
    """Materialize a freshly-manifested thread into its view (`notes/<name>.md`)."""
    view = NOTES_DIR / f"{name}.md"
    view.write_text(render_view(load_records(name), f"type: stream\ntitle: {name}"),
                    encoding="utf-8")
    return view


def cmd_fork(args) -> int:
    """Promote a reply-subtree into its own thread: write a derive manifest `{root: <id>, render}`,
    resolved LIVE from the global pool — no cards are copied, the fork is a lens on the graph. The
    root's own `reply_to` (its parent in the source thread) is kept; it just dangles in the fork's
    view, marking where the branch split off. New replies in the fork are still global cards, and
    any descendant of the root appears here automatically — the privileged fork 4chan can't do."""
    root = getattr(args, "root", None)
    if not root or not re.fullmatch(ID_RE, root):
        print("fork: --from must be a full 64-hex card id", file=sys.stderr)
        return 1
    if root not in _all_pool_cards():
        print(f"fork: no card {short_id(root)} in the pool", file=sys.stderr)
        return 1
    name = getattr(args, "as_", None) or f"fork-{short_id(root)}"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        print(f"fork: bad thread name {name!r}", file=sys.stderr)
        return 1
    if _manifest_path(name).exists():
        print(f"fork: thread '{name}' already exists", file=sys.stderr)
        return 1
    _write_manifest(name, {"root": root, "render": "scroll"})
    view = _new_thread_view(name)
    print(f"forked -> {view.relative_to(ROOT)}  (subtree of {short_id(root)}: {len(load_records(name))} cards)")
    return 0


# NOTE: `clone` is GONE under enc:v2. v1's clone copied a thread's id-LIST so two names could diverge
# independently. Under the derive model a thread IS its subtree (membership is derived, not stored), so
# a second name over the same root can never diverge — the semantic doesn't exist. Two people on one
# thread just both add cards to the same subtree. `fork` (a new ref rooted at a branch) is the one
# meaningful derive operation; clone was removed rather than kept as a confusing no-op alias.


def cmd_diff(args) -> int:
    view = _resolve_view(getattr(args, "view", None))
    if not _is_thread(view):
        print(f"{view.name}: not a stream thread — skipping")
        return 0
    cs = build_changeset(view.read_text(encoding="utf-8"), load_records(records_dir(view.stem)))
    pending = _pending_count(cs)
    if not getattr(args, "quiet", False):     # default: record state for the agent
        _write_changeset(view, cs)
        set_flag(view, pending)
    print(json.dumps(cs, indent=2, ensure_ascii=False))
    return 0 if pending == 0 else 2


def _file_new_cards(cs: dict, records: dict, view: pathlib.Path) -> int:
    rdir = records_dir(view.stem)
    wrote = 0
    for nc in cs["new_cards"]:
        if nc["self_consistent"] and nc["computed_id"] not in records:
            write_record(Card(id=nc["computed_id"], author=nc["author"],
                              captured_at=_next_ts(records, wrote),   # never leak captured_at='' into a record
                              reply_to=nc["reply_to"], flair=nc["flair"],
                              body=nc["body"]), rdir)
            wrote += 1
    return wrote


def cmd_extract(args) -> int:
    view = _resolve_view(getattr(args, "view", None))
    records = load_records(records_dir(view.stem))
    cs = build_changeset(view.read_text(encoding="utf-8"), records)
    wrote = _file_new_cards(cs, records, view)
    print(f"restored {len(cs['mutated'])} mutated; wrote {wrote} new record(s); "
          f"{cs['summary']['floating_lines']} floating left for the agent.")
    return 0


def cmd_run(args) -> int:
    """THE RUN BUTTON (one thread) — the deterministic SWEEP: check -> FOLD
    floating into fish cards (preserve operator input, don't drop it) -> restore
    any edited card interiors (records win) -> re-render -> clear flag. Stashes
    the change-set sidecar as the agent's reply payload. The reply itself
    (sidecar -> reply cards) is the LLM agent step, NOT done here."""
    view = _resolve_view(getattr(args, "view", None))
    if not _is_thread(view):
        print(f"{view.name}: not a stream thread — skipping")
        return 0
    rdir = records_dir(view.stem)
    records = load_records(rdir)
    cs = build_changeset(view.read_text(encoding="utf-8"), records)
    sidecar = _write_changeset(view, cs)
    s = cs["summary"]
    print(f"[run] {view.relative_to(ROOT)} — pending={_pending_count(cs)} "
          f"(mutated={s['mutated']} floating={s['floating_lines']} "
          f"new={s['new_cards']} dangling={s['dangling']})")

    gelled = gel_scaffolds(view, records)          # annotated pull-scaffolds -> quote-reply cards
    folded = fold_floating(view, load_records(rdir))   # scoop remaining floating -> fish cards
    wrote = _file_new_cards(cs, load_records(rdir), view)
    # re-render from records: edited interiors snap back, floating is now cards, flag cleared
    _render_keep_scaffolds(view, load_records(rdir))
    _refresh_dirty(view)

    print(f"[run] gelled {len(gelled)} scaffold(s); folded {len(folded)} floating block(s); "
          f"restored {s['mutated']} edited card(s); filed {wrote} new.")
    if folded or gelled or cs["mutated"]:
        print(f"[run] reply payload -> {sidecar.relative_to(ROOT)} — agent's turn")
    else:
        sidecar.unlink(missing_ok=True)
        print("[run] clean; nothing to reply to.")
    return 0


def cmd_scan(args) -> int:
    """THE BRAIN BUTTON: refresh the vault-wide dirty list. Diffs every
    `type: stream` thread, sets/clears each one's frontmatter flag + sidecar,
    and writes .stream/dirty.json — the agent-facing index of work to do."""
    views = find_stream_views()
    index = {}
    for view in views:
        cs = build_changeset(view.read_text(encoding="utf-8"), load_records(records_dir(view.stem)))
        pending = _pending_count(cs)
        set_flag(view, pending)
        rel = str(view.relative_to(ROOT))
        if pending > 0:
            _write_changeset(view, cs)
            index[rel] = {"pending": pending, **cs["summary"]}
        else:
            (CHANGESETS_DIR / f"{view.stem}.json").unlink(missing_ok=True)
    STREAM_DIR.mkdir(parents=True, exist_ok=True)
    DIRTY_INDEX.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"scanned {len(views)} thread(s); {len(index)} dirty -> {DIRTY_INDEX.relative_to(ROOT)}")
    for rel, info in index.items():
        print(f"  ● {rel}  pending={info['pending']}")
    if not index:
        print("  (all clean)")
    return 0


def _now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d (%a)-%H:%M:%S").lower()


def _parse_ts(ts: str):
    clean = re.sub(r"\s*\([a-z]{3}\)", "", ts or "")
    try:
        return datetime.strptime(clean, "%Y-%m-%d-%H:%M:%S")
    except ValueError:
        return None


def _next_ts(records: dict, offset: int = 0) -> str:
    """Monotonic timestamp: a new card is ALWAYS chronologically after the
    current head, regardless of wall-clock skew or manual timestamps — so a
    reply can never sort before its parent. (Wall clocks don't order a log;
    the head does. DDIA ch. 8: don't trust clocks for ordering.)"""
    now = datetime.now().replace(microsecond=0)   # compare at the stored 1s resolution, so a
    latest = None                                 # same-second mint always advances to latest+1s
    for c in records.values():
        t = _parse_ts(c.captured_at)
        if t and (latest is None or t > latest):
            latest = t
    base = now if (latest is None or now > latest) else latest + timedelta(seconds=1)
    return (base + timedelta(seconds=offset)).strftime("%Y-%m-%d (%a)-%H:%M:%S").lower()


# ── per-terminal lane pointers ────────────────────────────────────────────────
# A reply must bind to the turn it ACTUALLY answers, not to whatever card another
# concurrent terminal happened to append last. The terminal's Claude session id is
# the lane key (reachable at both ends: the hook's stdin `session_id`, and
# `CLAUDE_CODE_SESSION_ID` in the env `record` runs in). `.stream/sessions/<sid>.json`
# tracks this session's tip per thread; absent → callers fall back to the global head,
# so pure-CLI and single-terminal use are byte-for-byte unchanged.

def _session_lane(sid: str | None, thread: str) -> str | None:
    """The id of THIS terminal's last card in `thread` (its lane tip), or None when the
    session is unknown or has posted nothing here yet."""
    if not sid:
        return None
    data = _read_json(SESSIONS_DIR / f"{sid}.json", {})
    return (data.get("lanes") or {}).get(thread)


def _session_advance(sid: str | None, thread: str, cid: str) -> None:
    """Record `cid` as this session's new lane tip in `thread`. Local state; no-op without a sid."""
    if not sid:
        return
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    p = SESSIONS_DIR / f"{sid}.json"
    data = _read_json(p, {})
    data.setdefault("lanes", {})[thread] = cid
    data["updated"] = _now_ts()
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _reply_debt(records: dict) -> list:
    """Owed heads = every FISH card that is a lane LEAF (no card replies to it). Under
    concurrent terminals one thread branches into a lane per terminal, so reply-debt is
    per-leaf — not the single globally-latest card, which would surface one lane and bury
    the rest. Chronological."""
    children = {c.reply_to for c in records.values() if c.reply_to}
    return sorted((c for c in records.values() if c.id not in children and c.author == "fish"),
                  key=_sort_key)


def cmd_record(args) -> int:
    """THE DETERMINISM PRIMITIVE — single-source emit.

    The body is read ONCE from stdin and is the sole authored artifact. We
    content-address it, write the immutable record, re-render the view (so the
    Obsidian projection updates), and echo the SAME bytes to stdout. The
    terminal output and the thread card are therefore the same string by
    construction — never a re-typed summary — and `id` is the receipt:
    `card_id(what-you-saw) == stored id`, or it isn't the same card.

    This is how every card is born, in BOTH lanes: the operator/agent pipes the
    body through here; nobody ever transcribes a card by hand again."""
    body = sys.stdin.read()
    view = _resolve_view(getattr(args, "view", None))
    rdir = records_dir(view.stem)
    records = _reconcile_view(view)                       # fold staged drafts FIRST — never append over them
    sid = getattr(args, "session", None)
    # Resolve the parent FIRST — under enc:v2 the id commits to reply_to, so it must be final before we
    # can name the card. Explicit --reply-to wins; else --reply-head binds to THIS terminal's lane tip.
    # The v1 global-head GUESS is dead: a guessed parent is a wrong id, so an unresolvable --reply-head
    # REFUSES to mint (surfacing the head as debt) rather than misattribute a reply (§4).
    reply_to = args.reply_to
    if reply_to is None and getattr(args, "reply_head", False):
        lane = _session_lane(sid, view.stem)
        if lane and lane in records:
            reply_to = lane
        elif records:
            print("record: --reply-head but no lane tip for this session — refusing to guess a parent "
                  "(enc:v2: a guessed parent is a wrong id). Pass --reply-to <id> explicitly.",
                  file=sys.stderr)
            return 1
        # else: empty thread → reply_to stays None → this card is the root (a real, explicit ROOT).
    cid = card_id(body, reply_to)                         # NOW the address is final (parent-committed)
    if cid not in records:                               # idempotent: same body + same parent = same card
        write_record(Card(id=cid, author=args.author,
                          captured_at=args.ts or _next_ts(records),
                          reply_to=reply_to, flair=args.flair or "",
                          body=body), rdir)
        records = load_records(rdir)
    _session_advance(sid, view.stem, cid)                # this card is now my lane's tip
    # re-render so every VIEW (Obsidian + dashboard) is a projection of records
    _render_keep_scaffolds(view, records)
    if _is_thread(view):
        _refresh_dirty(view)
        DASHBOARD.write_text(render_dashboard(), encoding="utf-8")
    # emit the TUI callout — the response frame; its body == the record body
    sys.stdout.write(render_tui(records[cid]) + "\n")
    sys.stderr.write(f"[recorded {short_id(cid)} · author={args.author} · "
                     f"reply_to={short_id(reply_to) if reply_to else '-'} · {view.stem}]\n")
    return 0


def _compose_post(post: str, records: dict) -> tuple:
    """A single staging-area post -> (composed_body, first_ref). Each ``` scaffold (card-id
    then excerpt) is converted IN PLACE into a nested callout that INHERITS the quoted card's
    author callout type (`[!shizu]`, `[!claude-tui]`, …) so the vault's per-author CSS styles
    the quote like a mini of that author's card; the surrounding prose (blank lines and all)
    is kept. Returns (post, None) unchanged if it holds no scaffold — plain prose, left for
    `fold`."""
    lines, body, refs, i, n = post.split("\n"), [], [], 0, len(post.split("\n"))
    while i < n:
        if (fl := _fence_open(lines[i])):
            opener, fence, i = lines[i], [], i + 1
            while i < n and not _fence_close(lines[i], fl):
                fence.append(lines[i]); i += 1
            closer = lines[i] if i < n else None               # None if the operator left it unterminated
            if closer is not None:
                i += 1
            # Resolve which card this fence quotes. Two grammars:
            #   (a) explicit `pull` scaffold — first line is a bare card-id;
            #   (b) bare excerpt — no id line (operator pasted just the quoted span), so
            #       LOCATE its source by content. Unique hit → quote it; ambiguous (>1) or
            #       none → keep verbatim, never guess (the contract's locate rule).
            ref = fence[0].strip() if fence else ""
            if re.fullmatch(ID_RE, ref) and ref in records:
                quote_ref, excerpt = ref, fence[1:]
            else:
                kind, ids = _locate("\n".join(fence))
                quote_ref = (ids[0] if kind == "contains"
                             and len(ids) == 1 and ids[0] in records else None)
                excerpt = fence
            if quote_ref is not None:
                refs.append(quote_ref)
                author = records[quote_ref].author
                if body and body[-1].strip():
                    body.append("")                            # blank line before the callout
                body.append(f"> [!{author}] {author} | [[{quote_ref}|{short_id(quote_ref)}]]")   # author's card style
                body += [f"> {e}" if e else ">" for e in "\n".join(excerpt).strip("\n").split("\n")]
                body.append("")                                # blank line after the callout
            else:                                              # not a scaffold — keep VERBATIM, never
                body += [opener, *fence] + ([closer] if closer is not None else [])  # fabricate a close
        else:
            body.append(lines[i]); i += 1
    if not refs:
        return post, None
    return re.sub(r"\n{3,}", "\n\n", "\n".join(body)).strip("\n"), refs[0]


def gel_scaffolds(view: pathlib.Path, records: dict) -> list:
    """Gel the staging area (below the cards' trailing `---`) into composed QUOTE-REPLY cards —
    the other half of `pull`. The staging area is a sequence of posts separated by `---` bar
    breaks; EACH post that embeds a ``` scaffold (a card-id then the excerpt) gels into its own
    fish card, with the codeblock converted IN PLACE into a nested `[!quote]` callout and the
    lead-in / trailing prose (blank lines and all) kept. Posts with no scaffold are left to
    `fold`. Run/fold only — the incidental reconcile (record/capture/bump) leaves scaffolds
    alone, so drafting survives. Returns [(id, ref), …]."""
    head, staging = _staging(view.read_text(encoding="utf-8"))
    if not staging.strip():                                     # no staging area below the cards
        return []
    rdir, made, kept = records_dir(view.stem), [], []
    for post in _split_posts(staging):                          # each ---separated section = a post
        composed, ref = _compose_post(post, records)
        if not ref:
            kept.append(post)                                  # plain prose -> leave for fold
            continue
        cid = card_id(composed, ref)
        if cid not in records:
            write_record(Card(id=cid, author="fish", captured_at=_next_ts(records, len(made)),
                              reply_to=ref, flair="✎ *quote-reply*", body=composed), rdir)
        made.append((cid, ref))
    if not made:
        return []
    rest = "\n---\n".join(kept)
    view.write_text(head + ("\n" + rest if rest.strip() else "\n"), encoding="utf-8")
    return made


def fold_floating(view: pathlib.Path, records: dict) -> list:
    """Deterministic capture: floating operator text -> fish cards (no LLM, the
    author is known). A block typed beneath a card's `^caret` replies to THAT card —
    so you can answer any card in place and branch it into its own thread (fan-out),
    not just the head. A block BELOW the last `---` (the staging zone) attaches to the
    head (the enc:v2 derive model keeps a thread one connected subtree; the first post of
    an empty thread becomes the root). Everything in one zone — from an upstream ^caret (or the
    staging boundary) down to the next callout — is ONE card; internal blank lines are kept as
    paragraph breaks. Only a bare `---` barline (or a `pull` code-scaffold, or the next callout)
    splits a zone into multiple cards. Writes records only; the caller re-renders.
    Returns [(id, reply_to), ...]."""
    _, _, body_text = _split_frontmatter(view.read_text(encoding="utf-8"))
    lines = body_text.split("\n")
    n, i = len(lines), 0
    blocks, sep_idx, last_anchor = [], -1, None
    cur, cur_start = [], None

    def flush():
        nonlocal cur, cur_start
        block = cur
        while block and not block[0].strip():        # trim leading blank lines
            block.pop(0)
        while block and not block[-1].strip():        # trim trailing blanks (INTERNAL ones are kept)
            block.pop()
        if block:
            blocks.append((cur_start, "\n".join(block), last_anchor))   # the caret it sits beneath
        cur, cur_start = [], None

    while i < n:
        line = lines[i]
        if _NAV_RE.match(line):                    # structural header nav — never fold it
            i += 1; continue
        if _HEADER_RE.match(line):                 # skip a whole card
            flush(); i += 1
            while i < n and lines[i].startswith(">"): i += 1
            while i < n and lines[i].strip() == "": i += 1
            if i < n and (am := _ANCHOR_RE.match(lines[i])):
                last_anchor = am.group(1); i += 1  # now beneath this card's caret
            continue
        s = line.strip()
        if (fl := _fence_open(line)):              # a ``` codeblock (e.g. a `pull` scaffold)
            flush(); i += 1                        # is NOT prose to fold — skip it intact
            while i < n and not _fence_close(lines[i], fl): i += 1
            i += 1                                 # consume the closing fence
            continue
        if s == "---":
            flush()
            if sep_idx == -1:                      # staging boundary = the FIRST bare --- after the
                sep_idx = i                        # cards; later --- (bar breaks) only separate blocks
            i += 1; continue
        if s == "":                                # a blank line is INTERNAL whitespace of the current
            if cur:                                # post — a multi-paragraph reply stays ONE card; it
                cur.append(line)                   # never splits (only `---`/a callout/a scaffold does)
            i += 1; continue
        if cur_start is None:
            cur_start = i
        cur.append(line); i += 1
    flush()

    if not blocks:
        return []
    rdir = records_dir(view.stem)
    head = max(records.values(), key=_sort_key).id if records else None
    made = []
    for idx, (start, body, anchor) in enumerate(blocks):
        # A block beneath a card's ^caret replies to THAT card (branch/fan-out). A staging-zone block
        # (below the last ---) attaches to the head — under the enc:v2 derive model a thread is one
        # subtree, so a post stays connected to the tree (a truly disconnected root would be a separate
        # thread). The very first post of an empty thread has head=None → reply_to=None → it is the root.
        reply_to = (anchor or head)
        flair = "⚛️ *folded reply*" if reply_to else "⚛️ *folded post*"
        cid = card_id(body, reply_to)
        if cid not in records:
            write_record(Card(id=cid, author="fish", captured_at=_next_ts(records, idx),
                              reply_to=reply_to, flair=flair, body=body), rdir)
        made.append((cid, reply_to))
    return made


# ── annotation harvest ────────────────────────────────────────────────────────
# An operator can mark up a card body in the view. That edit makes the card `mutated`
# (its body no longer hashes to its id), so run/render would restore it and DISCARD
# the markup (iron rule #1). Harvest instead lifts each mark OUT into its own fish
# reply card BEFORE the restore: the host card stays immutable, and the mark becomes
# an append-only card that quotes what it points at. Two grammars, both backtick-based:
#
#   1. CODE-HIGHLIGHT (the primary gesture): select a span and hit the code-tick key —
#      `like this` — inline, just like bold/italic. The wrapped span IS the excerpt the
#      operator is quoting; harvest emits a fish `[!quote]` card of exactly that span,
#      replying to the host. The reply to it is the agent's job on the next `bump`.
#   2. SIGIL NOTE: a line that is SOLELY a `…` span is a note; its excerpt is the block
#      above and the span text is the note body (see _extract_annotations).
#
# Every other in-body edit is left to the restore (records win), so an accidental
# typo-fix is never mistaken for markup.
_SIGIL_RE = re.compile(r"^`([^`]+)`$")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")


def _extract_annotations(record_body: str, view_body: str) -> list[dict]:
    """Pure: diff a card's immutable body against its edited view body and return
    one dict per sigil annotation — {note, excerpt, section}. Deterministic, no IO.

      note    = the text inside the `…` marker.
      excerpt = the original block (contiguous non-blank record lines) immediately
                above where the note was inserted — the thing being annotated.
      section = the nearest record heading above the insertion, or "" if none.
    """
    a, b = record_body.split("\n"), view_body.split("\n")
    found: list[dict] = []
    for tag, i1, _i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        for j in range(j1, j2):                       # inserted / changed view lines
            m = _SIGIL_RE.match(b[j].strip())
            if not m:
                continue
            k = i1 - 1                                 # the record line the note sits under
            while k >= 0 and not a[k].strip():         # skip blank lines up to the block
                k -= 1
            block = []
            while k >= 0 and a[k].strip():             # gather the contiguous block
                block.append(a[k]); k -= 1
            block.reverse()
            section = ""
            for h in range(i1 - 1, -1, -1):            # nearest heading above = section label
                hm = _HEADING_RE.match(a[h].strip())
                if hm:
                    section = hm.group(1).strip(); break
            found.append({"note": m.group(1).strip(),
                          "excerpt": "\n".join(block).strip("\n"),
                          "section": section})
    return found


def _annotation_card_body(host_id: str, ann: dict) -> str:
    """Build a harvested card's body: the annotated excerpt as a nested `[!quote]`
    callout (render_card prefixes the `> ` rail, so it nests under the fish card),
    then the note. The nearest section heading becomes the callout label; if the
    excerpt IS that heading, it is dropped from the body to avoid repeating it."""
    note, excerpt, section = ann["note"], ann["excerpt"], ann["section"]
    hm = _HEADING_RE.match(excerpt.strip()) if excerpt else None
    if hm and hm.group(1).strip() == section:          # heading already shown as the label
        excerpt = ""
    title = f"{host_id} · {section}" if section else host_id
    lines = [f"> [!quote] {title}"]
    for ln in (excerpt.split("\n") if excerpt else []):
        lines.append(f"> {ln}" if ln else ">")
    lines += ["", note]
    return "\n".join(lines)


def _highlight_ticks(record_body: str, view_body: str):
    """If `view_body` is `record_body` with only backtick chars inserted, return the sorted
    record-offsets of those inserted ticks (each a highlight boundary); else None — the body was
    also edited, so tick-pairing can't be trusted (the caller falls back to a per-line scan).
    Greedy: a ` in the view that matches the record's own ` is taken as content, not a marker, so
    an excerpt that itself CONTAINS a ``` codeblock (non-tick boundaries) pairs on the wrap ticks
    alone — the internal fence matches record-to-view and is never mistaken for a boundary."""
    i = j = 0
    R, V, ticks = len(record_body), len(view_body), []
    while j < V:
        if i < R and view_body[j] == record_body[i]:
            i += 1; j += 1
        elif view_body[j] == "`":
            ticks.append(i); j += 1
        else:
            return None
    return ticks if i == R else None


def _extract_highlights(record_body: str, view_body: str) -> list[dict]:
    """Pure: spans the operator code-highlighted — wrapped in a `…` pair in the view but not code
    in the record (the inline-highlight gesture, like bolding). One dict per highlight,
    {excerpt, section}. Driven by the DIFF (record vs view), NOT a backtick regex, so an excerpt
    that itself holds inline ticks or a whole ``` codeblock is captured VERBATIM — the wrap ticks
    are the only inserted bytes, and everything between a pair is the excerpt, newlines and fences
    and all. A whole-line single-line span is a sigil note (left to _extract_annotations). Falls
    back to a per-line scan when the view also carries non-highlight edits (tick-pairing then
    being ambiguous)."""
    ticks = _highlight_ticks(record_body, view_body)
    if ticks is None or len(ticks) < 2:
        return _extract_highlights_inline(record_body, view_body)
    rlines = record_body.split("\n")
    starts, off = [], 0
    for ln in rlines:
        starts.append(off); off += len(ln) + 1
    def _line_of(offset):
        return max((k for k, s in enumerate(starts) if s <= offset), default=0)
    def _section(offset):
        for h in range(_line_of(offset), -1, -1):
            hm = _HEADING_RE.match(rlines[h].strip())
            if hm:
                return hm.group(1).strip()
        return ""
    out, seen = [], set()
    for a, b in zip(ticks[0::2], ticks[1::2]):           # pair (open, close); excerpt = record[a:b]
        excerpt = record_body[a:b].strip("\n")
        if not excerpt.strip() or excerpt in seen:
            continue
        if "\n" not in excerpt and rlines[_line_of(a)].strip() == excerpt.strip():
            continue                                     # whole-line span = sigil note, not a highlight
        seen.add(excerpt)
        out.append({"excerpt": excerpt, "section": _section(a)})
    return out


def _extract_highlights_inline(record_body: str, view_body: str) -> list[dict]:
    """Fallback for a view that mixes highlights with other edits: the per-line inline-code scan.
    Single-line spans only — a multi-line / inner-tick span needs the diff path above."""
    rec_spans = set(_INLINE_CODE_RE.findall(record_body))
    vlines = view_body.split("\n")
    out, seen = [], set()
    for idx, line in enumerate(vlines):
        for m in _INLINE_CODE_RE.finditer(line):
            raw, span = m.group(1), m.group(1).strip()
            if not span or span in rec_spans:            # already code in the record
                continue
            if line.strip() == f"`{raw}`":               # whole-line span = sigil note
                continue
            if span in seen:
                continue
            seen.add(span)
            section = ""
            for h in range(idx, -1, -1):                 # nearest heading above = label
                hm = _HEADING_RE.match(vlines[h].strip())
                if hm:
                    section = hm.group(1).strip(); break
            out.append({"excerpt": span, "section": section})
    return out


def _highlight_card_body(host_id: str, hl: dict) -> str:
    """A code-highlighted excerpt -> a fish 'quote' card body: the highlighted span as a nested
    `[!quote]` callout that replies to the host (each line rail-prefixed, so a multi-line excerpt
    or an embedded codeblock nests intact). No note — the highlight is the operator pointing at
    exactly what they are quoting; the reply is the agent's job on the next `bump`."""
    title = f"{host_id} · {hl['section']}" if hl["section"] else host_id
    lines = [f"> [!quote] {title}"]
    for ln in hl["excerpt"].split("\n"):
        lines.append(f"> {ln}" if ln else ">")
    return "\n".join(lines)


def harvest_annotations(view: pathlib.Path, records: dict) -> list:
    """Deterministic capture: in-body sigil notes -> fish reply cards (no LLM, like
    fold_floating). For every mutated host card, lift each `…`-marked note out of the
    diff into its own fish card that quotes the annotated excerpt as a nested callout
    and replies to the host. Writes records only; the caller re-renders (which also
    restores the host bodies — records win). Returns [(id, host_id), …]."""
    cs = build_changeset(view.read_text(encoding="utf-8"), records)
    rdir = records_dir(view.stem)
    made, idx = [], 0

    def emit(body: str, flair: str) -> None:
        nonlocal idx
        cid = card_id(body, host_id)
        if cid not in records and cid not in {m[0] for m in made}:
            write_record(Card(id=cid, author="fish", captured_at=_next_ts(records, idx),
                              reply_to=host_id, flair=flair, body=body), rdir)
            idx += 1
        made.append((cid, host_id))

    for mut in cs["mutated"]:
        host_id = mut["id"]
        for hl in _extract_highlights(mut["record_body"], mut["view_body"]):
            emit(_highlight_card_body(host_id, hl), "✎ *quoted*")
        for ann in _extract_annotations(mut["record_body"], mut["view_body"]):
            emit(_annotation_card_body(host_id, ann), "✎ *annotation*")
    return made


def _reconcile_view(view: pathlib.Path) -> dict:
    """Preserve every staged operator gesture BEFORE a caller re-renders — so the
    re-render that follows an append (a reply or a captured prompt) can never erase a
    drafted reply the operator left sitting in the view unsaved. The cleaning routine
    `run` performs, minus the final render: fold floating drafts into fish cards and
    file any self-consistent new card typed straight into the view. Records win, but
    only AFTER the draft is safely carded. Code-highlights are deliberately NOT touched
    here — they belong to `pull` (the re-render re-applies them, see `_render_keep_scaffolds`),
    so a reconcile never consumes a highlight before the operator clicks Pull. Returns the
    refreshed record set. Idempotent — a clean view is a no-op (fold and file are
    content-addressed)."""
    rdir = records_dir(view.stem)
    if not _is_thread(view):
        return load_records(rdir)
    cs = build_changeset(view.read_text(encoding="utf-8"), load_records(rdir))
    fold_floating(view, load_records(rdir))          # floating drafts -> fish cards
    _file_new_cards(cs, load_records(rdir), view)    # full cards typed in the view -> records
    return load_records(rdir)


def _is_resend(old: str, new: str) -> bool:
    """True if `new` is a resend/extension of `old` — the Ctrl+C-interrupt spam
    pattern, where each submit captured a longer PREFIX of the same message. Only a
    containment relationship qualifies: the survivor must SUBSUME the body it deletes,
    or the one record-delete path could unlink a distinct prompt (records-are-truth)."""
    old, new = old.strip(), new.strip()
    if not old or not new:
        return False
    return new.startswith(old) or old.startswith(new)


def cmd_capture(args) -> int:
    """Capture an operator prompt as a fish card. Binds to THIS terminal's lane tip (the turn it
    follows), falling back to the global head when the session is unknown. Collapses the Ctrl+C
    interrupt-spam prefix chain — consecutive unanswered fish resends supersede each other; an exact
    resubmit of the tip is a no-op. enc:v2: the id commits to the RESOLVED parent (resolved BEFORE the
    body is hashed), so the same prompt in two terminals (two lanes → two parents) is two distinct
    cards, not a silent cross-lane dedup that drops one operator's input — the bug v2 was built to kill.
    Capture never refuses (a prompt is never dropped); only `record` refuses to guess a reply parent."""
    body = sys.stdin.read()
    if not body.strip():
        return 0
    bl = body.strip().lower().lstrip("/")             # /bump (slash-command form) == bump
    if bl in CONTROL_WORDS or bl.startswith("bump"):  # pure trigger (e.g. "bump the thread X")
        sys.stderr.write(f"[capture: control phrase {body.strip()!r}, not carded]\n")
        return 0
    view = _resolve_view(getattr(args, "view", None))
    rdir = records_dir(view.stem)
    records = load_records(rdir)
    sid = getattr(args, "session", None)
    lane = _session_lane(sid, view.stem)              # this terminal's last card here (None if unknown)

    def _tip():                                       # my lane tip if known, else the global head
        return (records.get(lane) if (sid and lane) else
                (max(records.values(), key=_sort_key) if records else None))

    tip = _tip()
    if tip and tip.body.strip() == body.strip():      # exact resubmit of the tip -> no-op
        sys.stderr.write(f"[capture: exact dup {short_id(tip.id)}, skipped]\n")
        return 0

    superseded = []
    # Collapse the Ctrl+C interrupt-spam chain — with a session ONLY within MY lane (never another
    # terminal's card that happens to be the head — that would unlink a live prompt); else the head walk.
    while records:
        prev = _tip()
        if (prev and prev.author == "fish" and prev.body.strip() != body.strip()
                and _is_resend(prev.body, body)):
            _drop_pooled(rdir, prev)                  # retract the superseded resend from the pool
            superseded.append(prev.id)
            if sid:
                lane = prev.reply_to                  # reconnect up my lane to the grandparent
            records = load_records(rdir)
        else:
            break
    records = _reconcile_view(view)                   # fold staged drafts before carding the prompt
    reply_to = (lane if (sid and lane and lane in records)   # bind to MY lane tip, not the global head
                else (max(records.values(), key=_sort_key).id if records else None))  # fallback: head
    cid = card_id(body, reply_to)                     # enc:v2: address committed to the resolved parent
    if cid not in records:
        write_record(Card(id=cid, author="fish", captured_at=_next_ts(records),
                          reply_to=reply_to, flair="", body=body), rdir)
        records = load_records(rdir)
    _session_advance(sid, view.stem, cid)             # this prompt is now my lane's tip
    if _is_thread(view):
        _render_keep_scaffolds(view, records)
        _refresh_dirty(view)
        DASHBOARD.write_text(render_dashboard(), encoding="utf-8")
    sys.stderr.write(f"[captured {short_id(cid)}; superseded {superseded or 'none'}]\n")
    return 0


def cmd_fold(args) -> int:
    view = _resolve_view(getattr(args, "view", None))
    if not _is_thread(view):
        print(f"{view.name}: not a stream thread — skipping")
        return 0
    gelled = gel_scaffolds(view, load_records(records_dir(view.stem)))
    made = fold_floating(view, load_records(records_dir(view.stem)))
    if not made and not gelled:
        print("fold: no floating text to fold")
        return 0
    _render_keep_scaffolds(view, load_records(records_dir(view.stem)))
    _refresh_dirty(view)
    for cid, ref in gelled:
        print(f"gelled -> fish {cid} (quote-reply to {ref})")
    for cid, rt in made:
        print(f"folded -> fish {cid} ({'reply to ' + rt if rt else 'new post (nonreply)'})")
    return 0


def cmd_annotate(args) -> int:
    """THE ANNOTATE BUTTON (one thread): lift in-body `…` sigil notes out of edited
    cards into fish reply cards that quote the annotated excerpt, then re-render —
    which restores every host body to canonical (records win). Deterministic, no LLM;
    host ids are never touched (iron rule #1). Distinct from `run`/`fold`, which
    handle floating text between cards, not notes typed inside one."""
    view = _resolve_view(getattr(args, "view", None))
    if not _is_thread(view):
        print(f"{view.name}: not a stream thread — skipping")
        return 0
    rdir = records_dir(view.stem)
    made = harvest_annotations(view, load_records(rdir))
    # re-render from records: harvested notes appear as cards, host bodies restored
    _render_keep_scaffolds(view, load_records(rdir))
    _refresh_dirty(view)
    if not made:
        print("annotate: no `…`-sigil annotations found")
        return 0
    for cid, host in made:
        print(f"harvested -> fish {cid} (annotates {host})")
    return 0


def pull_highlights(view: pathlib.Path) -> dict:
    """Core of the extraction button (testable; touches only the view, no records, no
    flag I/O). SCRUB ABOVE / APPEND BELOW the last `---` barline: find the spans the
    operator code-highlighted in the cards, restore those cards to canonical (the
    highlight is consumed), and append one ``` codeblock per NEW highlight to the draft
    zone below the line — `<card-id>` then the excerpt — as scaffolds to annotate
    against. Append-only below; a highlight already sitting below is skipped, and if no
    highlight was found nothing is written at all. Returns {found, appended:[(id,exc)]}."""
    rdir = records_dir(view.stem)
    records = load_records(rdir)
    text = view.read_text(encoding="utf-8")
    _, staging = _staging(text)                           # the WHOLE draft zone (every section)

    found, new, seen = 0, [], staging
    for mut in build_changeset(text, records)["mutated"]:
        for hl in _extract_highlights(mut["record_body"], mut["view_body"]):
            found += 1
            block = f"{mut['id']}\n{hl['excerpt']}"
            if block not in seen:                         # dedup: never duplicate in the draft zone
                new.append((mut["id"], hl["excerpt"]))
                seen += "\n" + block
    if not found:                                         # nothing highlighted -> write nothing
        return {"found": 0, "appended": []}

    above = render_view(records, _clean_fm_block(view))   # scrub: cards restored, ends with the barline
    def _scaffold(cid, exc):                              # variable-length fence so an excerpt that itself
        f = _fence(f"{cid}\n{exc}")                       # embeds a ``` block rides a longer (>=4) fence
        return f"\n{f}\n{cid}\n{exc}\n{f}\n"
    appended = "".join(_scaffold(cid, exc) for cid, exc in new)
    view.write_text(above + staging + appended, encoding="utf-8")   # keep every section, append at the end
    return {"found": found, "appended": new}


def cmd_pull(args) -> int:
    """THE EXTRACTION BUTTON: pull code-highlighted excerpts out of the cards and down
    into the draft zone as ``` codeblocks to annotate against. Scrub above the `---`,
    append below it; nothing appended if no highlight was found; re-clicking is a no-op
    after the first (the highlight is scrubbed, and duplicates below are skipped)."""
    view = _resolve_view(getattr(args, "view", None))
    if not _is_thread(view):
        print(f"{view.name}: not a stream thread — skipping")
        return 0
    res = pull_highlights(view)
    if res["found"] == 0:
        print("pull: no code-highlighted excerpts found — nothing appended")
        return 0
    _refresh_dirty(view)
    for cid, exc in res["appended"]:
        print(f"pull: {cid} ← `{exc}`  → codeblock below the line")
    if not res["appended"]:
        print("pull: highlights already extracted below — nothing appended (scrubbed above)")
    return 0


def _read_json(p: pathlib.Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def render_dashboard() -> str:
    """A derived VIEW (not a record) that compresses everything the buttons
    queue and relay — daemon status, the dirty worklist, the agent's reply debt,
    the thread inventory, and the last result — into one note both the operator
    and the agent read. Regenerated on every action; never authoritative."""
    daemon = _read_json(STREAM_DIR / "daemon.json", {})
    dirty = _read_json(DIRTY_INDEX, {})
    result = _read_json(STREAM_DIR / "result.json", {})
    pid = daemon.get("pid")
    alive = bool(pid) and pathlib.Path(f"/proc/{pid}").exists()
    status = f"🟢 live (pid {pid})" if alive else "🔴 down"
    la = daemon.get("last_action")
    last = (f"{la} → {'ok' if daemon.get('last_ok') else 'err'} "
            f"@ {daemon.get('last_action_at', '-')}" if la else "—")

    o = ["---", "type: dashboard", "---", "",
         "# stream · dashboard", "",
         f"> [!note] daemon {status} · last: {last} · `updated {_now_ts()}`", ""]

    o.append("## status — dirty threads")
    if dirty:
        o += ["| thread | pending | edited | floating | new | dangling |",
              "|---|--:|--:|--:|--:|--:|"]
        for th, i in dirty.items():
            o.append(f"| `{th}` | {i.get('pending',0)} | {i.get('mutated',0)} | "
                     f"{i.get('floating_lines',0)} | {i.get('new_cards',0)} | {i.get('dangling',0)} |")
    else:
        o.append("all clean ✓")
    o.append("")

    # to-do + threads derive from RECORDS (the truth) — never the derived view, which can be
    # hand-dirtied and disagree. If the newest record is the operator's, the agent owes a reply.
    # (Same source as `bump`, so the dashboard and the heartbeat can never contradict.)
    threads = []
    for v in find_stream_views():
        recs = load_records(records_dir(v.stem))
        head = max(recs.values(), key=_sort_key) if recs else None
        threads.append((v, head, recs))
    o.append("## to-do — reply debt (agent's queue)")
    todo = []
    for v, _head, recs in threads:
        for head in _reply_debt(recs):                # every fish LEAF — one owed head per open lane
            snip = " ".join(head.body.split())
            snip = snip[:200] + ("…" if len(snip) > 200 else "")
            todo.append(f"- `{v.stem}` — reply to [[{head.id}]] (re: {head.reply_to or '—'}): {snip}")
    o += todo if todo else ["nothing queued ✓"]
    o.append("")

    o.append("## threads")
    for v, head, recs in threads:
        rel = v.relative_to(ROOT)
        o.append(f"- `{rel}` — {len(recs)} cards · head [[{head.id}]] ({head.author})" if head
                 else f"- `{rel}` — empty")
    o.append("")

    # API — summon calls (user-triggered only; never automatic)
    o.append("## API — summon calls (manual only)")
    inflight = _read_json(SUMMON_INFLIGHT, None)
    if inflight:
        o.append(f"⏳ **summon in flight** (since {inflight.get('started','?')}) · churning… "
                 f"— live elapsed in the status-bar chip")
    entries, total = [], 0
    if API_LOG.exists():
        lines = [ln for ln in API_LOG.read_text(encoding="utf-8").splitlines() if ln.strip()]
        total = len(lines)
        for ln in lines[-5:]:
            try:
                entries.append(json.loads(ln))
            except ValueError:
                pass
    if entries:
        last = entries[-1]
        errs = sum(1 for e in entries if not e.get("ok"))
        health = f"last **{'ok' if last.get('ok') else 'ERR'}**"
        if last.get("duration_s") is not None:
            health += f" · {last['duration_s']}s"
        o.append(f"health: {health} · {total} calls logged · {errs}/{len(entries)} recent errors")
        for e in reversed(entries):
            row = f"- {e.get('ts','?')} · {'ok' if e.get('ok') else 'ERR'}"
            if e.get("duration_s") is not None:
                row += f" · {e['duration_s']}s"
            if e.get("chars"):
                row += f" · {e['chars']} chars"
            if e.get("error"):
                row += f" · {e['error']}"
            o.append(row)
    else:
        o.append("no calls yet — hit ⚡ Summon (your click only; never automatic)")
    o.append("")

    o.append("## recent")
    if result:
        o.append(f"- last result: **{result.get('action')}** → "
                 f"{'ok' if result.get('ok') else 'err'} (nonce `{result.get('nonce','-')}`)")
    else:
        o.append("- (no runs yet)")
    o.append("")
    return "\n".join(o)


def cmd_dashboard(args) -> int:
    md = render_dashboard()
    if args.write:
        DASHBOARD.write_text(md, encoding="utf-8")
        print(f"dashboard -> {DASHBOARD.relative_to(ROOT)}")
    else:
        sys.stdout.write(md)
    return 0


def cmd_render_tui(args) -> int:
    view = _resolve_view(getattr(args, "view", None))
    records = load_records(records_dir(view.stem))
    if not records:
        print(f"{view.stem}: no records yet", file=sys.stderr)
        return 1
    if getattr(args, "id", None):
        want = args.id
        card = records.get(want)
        if card is None:                                  # accept the 8-char display prefix too
            matches = [c for cid, c in records.items() if cid.startswith(want)]
            if len(matches) == 1:
                card = matches[0]
            elif len(matches) > 1:
                print(f"render-tui: id prefix {want!r} is ambiguous ({len(matches)} cards)", file=sys.stderr)
                return 1
        if card is None:
            print(f"no record {want}", file=sys.stderr)
            return 1
    else:
        card = max(records.values(), key=_sort_key)      # --last
    print(render_tui(card))
    return 0


def cmd_bump(args) -> int:
    """THE HEARTBEAT, as one reflex. Reconcile every dirty thread (fold staged drafts,
    harvest in-card notes/highlights, restore — nothing scrubbed), refresh the
    dashboard, then print the reply-debt queue with each owed head's text. The agent
    runs this once and answers each head it prints — no scan, no deliberation, the
    rails are the output."""
    reconciled = []
    for view in find_stream_views():
        records = load_records(records_dir(view.stem))
        cs = build_changeset(view.read_text(encoding="utf-8"), records)
        if _pending_count(cs) > 0:                       # something staged → preserve it
            _reconcile_view(view)                        # harvest + fold + file (no scrub)
            _render_keep_scaffolds(view, load_records(records_dir(view.stem)))  # restore + keep scaffolds
            _refresh_dirty(view)
            reconciled.append(view.stem)
    DASHBOARD.write_text(render_dashboard(), encoding="utf-8")
    print(f"[bump] reconciled: {', '.join(reconciled) or 'nothing dirty'}")

    debts = []
    for view in find_stream_views():
        records = load_records(records_dir(view.stem))
        for head in _reply_debt(records):             # every fish LEAF — one owed head per open lane
            debts.append((view.stem, head))
    if not debts:
        print("[bump] reply-debt: none — clean beat, stop.")
        return 0
    print(f"[bump] reply-debt: {len(debts)} — answer each, then stop:")
    for stem, head in debts:
        snip = " ".join(head.body.split())
        snip = snip[:280] + ("…" if len(snip) > 280 else "")
        print(f"  → [{head.id}] in {stem} (re: {head.reply_to or '—'}):  {snip}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="stream-cards backend (enc:v2 — Merkle-DAG)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pid = sub.add_parser("id", help="enc:v2 id of a body on stdin (pass --reply-to for a reply's address)")
    pid.add_argument("--reply-to", dest="reply_to", default=None, help="parent id (omit for a root)")
    sub.add_parser("locate", help="resolve a stdin excerpt to its source card id(s) (substring-scan)")
    sub.add_parser("validate", help="re-hash records + referential integrity")
    sub.add_parser("scan", help="vault-wide dirty pass (Brain): flag every thread + dirty.json")
    pr = sub.add_parser("render", help="records -> view (clears the dirty flag)")
    pr.add_argument("--view"); pr.add_argument("--check", action="store_true")
    pr.add_argument("--write", action="store_true")
    pr.add_argument("--hard", action="store_true",
                    help="the flask/Restore button: dissolve edits AND the staging draft, rebuild canonical")
    pd = sub.add_parser("diff", help="view -> change-set + sidecar + flag")
    pd.add_argument("--view"); pd.add_argument("--quiet", action="store_true",
                    help="preview only; don't write sidecar/flag")
    pe = sub.add_parser("extract", help="restore mutated + persist new cards")
    pe.add_argument("--view")
    prun = sub.add_parser("run", help="the run button: check -> scrub -> render, one thread")
    prun.add_argument("--view")
    prec = sub.add_parser("record", help="single-source emit: stdin body -> record + re-render + echo")
    prec.add_argument("--author", default="claude")
    prec.add_argument("--reply-to", dest="reply_to", default=None)
    prec.add_argument("--flair", default="")
    prec.add_argument("--ts", default=None)
    prec.add_argument("--reply-head", dest="reply_head", action="store_true",
                      help="reply to the current head when --reply-to is omitted")
    prec.add_argument("--session", default=os.environ.get("CLAUDE_CODE_SESSION_ID"),
                      help="terminal/session id — binds --reply-head to THIS lane's tip, not the global head")
    prec.add_argument("--view")
    pt = sub.add_parser("render-tui", help="print a card as the TUI callout (the reply frame)")
    pt.add_argument("--id", default=None, help="card id (default: latest)")
    pt.add_argument("--view", help="thread to read from (default: main)")
    pf = sub.add_parser("fold", help="deterministically fold floating text into fish cards")
    pf.add_argument("--view")
    pann = sub.add_parser("annotate", help="harvest in-body `…` notes into fish reply cards")
    pann.add_argument("--view")
    ppull = sub.add_parser("pull", help="extract code-highlighted excerpts into ``` codeblocks below the ---")
    ppull.add_argument("--view")
    pc = sub.add_parser("capture", help="capture a prompt as a fish card (collapses interrupt-spam)")
    pc.add_argument("--view")
    pc.add_argument("--session", default=os.environ.get("CLAUDE_CODE_SESSION_ID"),
                    help="terminal/session id (the hook passes the prompt's session_id)")
    pdb = sub.add_parser("dashboard", help="compile .stream state -> DASHBOARD.md")
    pdb.add_argument("--write", action="store_true")
    sub.add_parser("bump", help="the heartbeat: reconcile all dirty threads + print the reply-debt")
    pfk = sub.add_parser("fork", help="new thread = the reply-subtree rooted at a card (derive manifest)")
    pfk.add_argument("--from", dest="root", help="full card id to root the subtree at")
    pfk.add_argument("--as", dest="as_", help="new thread name (default: fork-<short>)")
    args = ap.parse_args()
    return {"id": cmd_id, "validate": cmd_validate, "scan": cmd_scan,
            "render": cmd_render, "diff": cmd_diff, "extract": cmd_extract,
            "run": cmd_run, "record": cmd_record, "render-tui": cmd_render_tui,
            "fold": cmd_fold, "dashboard": cmd_dashboard,
            "capture": cmd_capture, "annotate": cmd_annotate, "bump": cmd_bump,
            "pull": cmd_pull, "fork": cmd_fork,
            "locate": cmd_locate}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
