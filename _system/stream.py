#!/usr/bin/env python3
"""
stream.py — the deterministic core of the stream-cards backend (enc:v1).

Stdlib only. No external dependencies, no install step, no daemon required to
use the CLI. Self-locating: works from wherever the vault is cloned.

TWO REPRESENTATIONS (DDIA derived data, ch. 11-12)
  notes/records/<thread>/<id>.md   SOURCE OF TRUTH — one immutable,
                    content-addressed card, partitioned by thread.
  notes/threads/*.md   the VIEW — a materialized projection that renders a
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
NOTES_DIR = ROOT / "notes"
RECORDS_ROOT = NOTES_DIR / "records"        # one SUBDIR per thread (partition key)
THREAD_DIR = NOTES_DIR / "threads"
DEFAULT_VIEW = THREAD_DIR / "main.md"       # used when --view is omitted
STREAM_DIR = ROOT / ".stream"               # local daemon/runtime state (never synced)
CHANGESETS_DIR = STREAM_DIR / "changesets"
DIRTY_INDEX = STREAM_DIR / "dirty.json"
API_LOG = STREAM_DIR / "api-log.jsonl"
SUMMON_INFLIGHT = STREAM_DIR / "summon-inflight.json"
DASHBOARD = ROOT / "DASHBOARD.md"           # the derived status view (root)


def records_dir(view_stem: str) -> pathlib.Path:
    """A thread's record store. Records are PARTITIONED by thread on the
    filesystem — `notes/records/<thread>/<id>.md` — so a card can never leak
    into another thread's view, and the same body in two threads is two files
    (same id, different dirs), not a content-hash collision that drops one."""
    return RECORDS_ROOT / view_stem

# ════════════════════════════════════════════════════════════════════════════
#  P0 — enc:v1 : the canonicalization contract
# ════════════════════════════════════════════════════════════════════════════

def normalize(body: str) -> str:
    """enc:v1 canonical form. Byte-stable across editors/OS or the whole
    content-addressing scheme breaks. Verified against all records."""
    s = unicodedata.normalize("NFC", body)
    lines = [ln.rstrip() for ln in s.split("\n")]
    s = "\n".join(lines).strip("\n")
    return s + "\n"


def card_id(body: str, length: int = 8) -> str:
    """The content address. id = sha256(normalize(body))[:length]."""
    return hashlib.sha256(normalize(body).encode("utf-8")).hexdigest()[:length]


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
    channel: str = "stream"
    captured_at: str = ""
    reply_to: str | None = None
    flair: str = ""
    thread: str = ""
    body: str = ""

    @property
    def computed_id(self) -> str:
        return card_id(self.body)


def load_records(rdir: pathlib.Path) -> dict[str, Card]:
    """Load every <id>.md record in ONE thread's store. Key = filename stem
    (the printed id). A thread with no cards yet has no dir — that's empty,
    not an error."""
    out: dict[str, Card] = {}
    if not rdir.exists():
        return out
    for p in sorted(rdir.glob("*.md")):
        fm, _, body = _split_frontmatter(p.read_text(encoding="utf-8"))
        out[p.stem] = Card(
            id=fm.get("hash") or p.stem,
            author=fm.get("author", "claude"),
            channel=fm.get("channel", "stream"),
            captured_at=fm.get("captured_at", ""),
            reply_to=(fm.get("reply_to") or None),
            flair=fm.get("flair", ""),
            thread=fm.get("thread", ""),
            body=body,
        )
    return out


def write_record(card: Card, rdir: pathlib.Path) -> pathlib.Path:
    """Write an immutable <id>.md record into a thread's store. Append-only by
    convention. Creates the thread's record dir on first write."""
    rdir.mkdir(parents=True, exist_ok=True)
    out = rdir / f"{card.id}.md"
    fm_lines = ["---", f"hash: {card.id}", f"author: {card.author}",
                f"channel: {card.channel}"]
    if card.thread:
        fm_lines.append(f'thread: "{card.thread}"')
    fm_lines.append(f"captured_at: {card.captured_at}")
    if card.reply_to:
        fm_lines.append(f"reply_to: {card.reply_to}")
    if card.flair:
        fm_lines.append(f'flair: "{card.flair}"')
    fm_lines.append("---\n")
    body = card.body if card.body.startswith("\n") else "\n" + card.body
    out.write_text("\n".join(fm_lines) + body, encoding="utf-8")
    return out


# ════════════════════════════════════════════════════════════════════════════
#  P2 — render : records -> view  (the materialized projection)
# ════════════════════════════════════════════════════════════════════════════

def _sort_key(card: Card) -> str:
    """Deterministic thread order: chronological by captured_at."""
    return re.sub(r"\s*\([a-z]{3}\)", "", card.captured_at)


def render_card(card: Card) -> str:
    """One card -> its callout post block + ^anchor (view grammar §3)."""
    head = f"> [!{card.author}] {card.author} - {card.captured_at} | [[{card.id}]]"
    if card.reply_to:
        head += f" >> [[#^{card.reply_to}|{card.reply_to}]]"
    head += f" <br> {card.flair}"
    body = normalize(card.body).rstrip("\n")
    body_lines = [(f"> {ln}" if ln else ">") for ln in body.split("\n")]
    return "\n".join([head, *body_lines, "", f"^{card.id}"])


def render_tui(card: Card) -> str:
    """The TERMINAL chrome for a card — the third surface alongside the Obsidian
    callout and the raw record. The left rail `┃` is the TUI's `> `: strip the
    chrome on either surface and the BODY is byte-identical. The footer carries
    the enc:v1 id as the receipt. This is the frame an agent's reply is presented
    in, so what's said == what's stored, by construction."""
    rail = "┃"
    head = f"┏━ [{card.author}] · {card.id}" + (f" ↳ {card.reply_to}" if card.reply_to else "")
    out = [head]
    if card.flair:
        out.append(f"{rail} {card.flair}")
    out.append(rail)
    for ln in normalize(card.body).rstrip("\n").split("\n"):
        out.append(f"{rail} {ln}" if ln else rail)
    out.append(f"┗━ enc:v1 {card.id}")
    return "\n".join(out)


def render_view(records: dict[str, Card], fm_block: str) -> str:
    """Regenerate the whole view note from records. Pure function of the
    record set + sort key — the view is a build artifact."""
    cards = sorted(records.values(), key=_sort_key)
    body = "\n\n".join(render_card(c) for c in cards)
    out = f"---\n{fm_block}\n---\n\n" if fm_block else ""
    out += body + "\n\n---\n"   # trailing reply-zone separator (card 3adb72d6)
    return out


# ════════════════════════════════════════════════════════════════════════════
#  view parser  (the CDC capture: read changes that entered through the view)
# ════════════════════════════════════════════════════════════════════════════

_HEADER_RE = re.compile(
    r"^> \[!(?P<author>[\w-]+)\]\s+[\w-]+\s+-\s+(?P<ts>.+?)\s+\|\s+"
    r"\[\[(?P<id>[0-9a-f]+)\]\]"
    r"(?:\s+>>\s+\[\[#\^(?P<parent>[0-9a-f]+)\|[0-9a-f]+\]\])?"
    r"\s+<br>\s+(?P<flair>.*)$"
)
_ANCHOR_RE = re.compile(r"^\^([0-9a-f]+)\s*$")


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
        return card_id(self.body)

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
                body="\n".join(body_lines).strip("\n"), anchor=anchor))
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
    print(card_id(sys.stdin.read()))
    return 0


def cmd_validate(args) -> int:
    """Re-hash every record in every thread + check reply_to edges resolve
    WITHIN their thread (replies are intra-thread). Vault-wide integrity."""
    dirs = sorted(d for d in RECORDS_ROOT.glob("*") if d.is_dir()) if RECORDS_ROOT.exists() else []
    n = 0
    bad_hash: list[tuple[str, str, str]] = []
    bad_ref: list[tuple[str, str, str]] = []
    for d in dirs:
        records = load_records(d)
        n += len(records)
        for r, c in records.items():
            if c.computed_id != r:
                bad_hash.append((d.name, r, c.computed_id))
            if c.reply_to and c.reply_to not in records:
                bad_ref.append((d.name, r, c.reply_to))
    print(f"threads:            {len(dirs)}")
    print(f"records:            {n}")
    print(f"hash integrity:     {n - len(bad_hash)}/{n} reproduce their id")
    for th, r, got in bad_hash:
        print(f"  ✗ {th}/{r}: body hashes to {got}")
    print(f"referential:        {n - len(bad_ref)}/{n} reply_to edges resolve")
    for th, r, tgt in bad_ref:
        print(f"  ✗ {th}/{r}: reply_to -> {tgt} (missing in thread)")
    ok = not bad_hash and not bad_ref
    print("VALID ✓" if ok else "INVALID ✗")
    return 0 if ok else 1


def cmd_render(args) -> int:
    view = _resolve_view(getattr(args, "view", None))
    if not _is_thread(view):
        print(f"{view.name}: not a stream thread — skipping")
        return 0
    rendered = render_view(load_records(records_dir(view.stem)), _clean_fm_block(view))
    if args.write:
        view.write_text(rendered, encoding="utf-8")
        _refresh_dirty(view)
        print(f"rendered -> {view.relative_to(ROOT)} (dirty flag cleared)")
        return 0
    if args.check:
        current = view.read_text(encoding="utf-8") if view.exists() else ""
        same = current == rendered
        print("view MATCHES records ✓" if same else
              "view DIFFERS from records ✗ (run `render --write`)")
        return 0 if same else 1
    sys.stdout.write(rendered)
    return 0


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
                              reply_to=nc["reply_to"], flair=nc["flair"],
                              body=nc["body"], thread=f"[[{view.stem}]]"), rdir)
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

    folded = fold_floating(view, records)          # scoop floating -> fish cards (preserve)
    wrote = _file_new_cards(cs, load_records(rdir), view)
    # re-render from records: edited interiors snap back, floating is now cards, flag cleared
    view.write_text(render_view(load_records(rdir), _clean_fm_block(view)), encoding="utf-8")
    _refresh_dirty(view)

    print(f"[run] folded {len(folded)} floating block(s); "
          f"restored {s['mutated']} edited card(s); filed {wrote} new.")
    if folded or cs["mutated"]:
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
    now = datetime.now()
    latest = None
    for c in records.values():
        t = _parse_ts(c.captured_at)
        if t and (latest is None or t > latest):
            latest = t
    base = now if (latest is None or now > latest) else latest + timedelta(seconds=1)
    return (base + timedelta(seconds=offset)).strftime("%Y-%m-%d (%a)-%H:%M:%S").lower()


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
    cid = card_id(body)                                   # the content address
    view = _resolve_view(getattr(args, "view", None))
    rdir = records_dir(view.stem)
    records = load_records(rdir)
    reply_to = args.reply_to
    if reply_to is None and getattr(args, "reply_head", False) and records:
        reply_to = max(records.values(), key=_sort_key).id   # thread onto the head
    if cid not in records:                               # idempotent: same body = same card
        write_record(Card(id=cid, author=args.author,
                          captured_at=args.ts or _next_ts(records),
                          reply_to=reply_to, flair=args.flair or "",
                          body=body, thread=f"[[{view.stem}]]"), rdir)
        records = load_records(rdir)
    # re-render so every VIEW (Obsidian + dashboard) is a projection of records
    view.write_text(render_view(records, _clean_fm_block(view)), encoding="utf-8")
    if _is_thread(view):
        _refresh_dirty(view)
        DASHBOARD.write_text(render_dashboard(), encoding="utf-8")
    # emit the TUI callout — the response frame; its body == the record body
    sys.stdout.write(render_tui(records[cid]) + "\n")
    sys.stderr.write(f"[recorded {cid} · author={args.author} · "
                     f"reply_to={reply_to or '-'} · {view.stem}]\n")
    return 0


def fold_floating(view: pathlib.Path, records: dict) -> list:
    """Deterministic capture: floating operator text -> fish cards (no LLM, the
    author is known). Honors the reply-zone vs new-post-zone split at the last
    `---`: a block ABOVE it replies to the head card; a block BELOW it posts as a
    new root (the operator's 'nonreply'). Each blank-separated block = one card.
    Writes records only; the caller re-renders. Returns [(id, reply_to), ...]."""
    _, _, body_text = _split_frontmatter(view.read_text(encoding="utf-8"))
    lines = body_text.split("\n")
    n, i = len(lines), 0
    blocks, sep_idx = [], -1
    cur, cur_start = [], None

    def flush():
        nonlocal cur, cur_start
        if cur:
            blocks.append((cur_start, "\n".join(cur)))
            cur, cur_start = [], None

    while i < n:
        line = lines[i]
        if _HEADER_RE.match(line):                 # skip a whole card
            flush(); i += 1
            while i < n and lines[i].startswith(">"): i += 1
            while i < n and lines[i].strip() == "": i += 1
            if i < n and _ANCHOR_RE.match(lines[i]): i += 1
            continue
        s = line.strip()
        if s == "---":
            flush(); sep_idx = i; i += 1; continue
        if s == "":
            flush(); i += 1; continue
        if cur_start is None:
            cur_start = i
        cur.append(line); i += 1
    flush()

    if not blocks:
        return []
    rdir = records_dir(view.stem)
    head = max(records.values(), key=_sort_key).id if records else None
    made = []
    for idx, (start, body) in enumerate(blocks):
        reply_to = head if (sep_idx == -1 or start < sep_idx) else None
        flair = "⚛️ *folded reply*" if reply_to else "⚛️ *folded post*"
        cid = card_id(body)
        if cid not in records:
            write_record(Card(id=cid, author="fish", captured_at=_next_ts(records, idx),
                              reply_to=reply_to, flair=flair, body=body,
                              thread=f"[[{view.stem}]]"), rdir)
        made.append((cid, reply_to))
    return made


def _is_resend(old: str, new: str) -> bool:
    """True if `new` is a resend/extension of `old` — the Ctrl+C-interrupt spam
    pattern, where each submit captured a longer PREFIX of the same message."""
    old, new = old.strip(), new.strip()
    if not old or not new:
        return False
    if new.startswith(old) or old.startswith(new):
        return True
    return difflib.SequenceMatcher(None, old, new).ratio() > 0.9


def cmd_capture(args) -> int:
    """Capture an operator prompt as a fish card, collapsing the interrupt-spam
    prefix chain: while the head is a fish card that this body is a resend of,
    supersede it (delete + replace with the longer/latest). A claude reply
    between two fish cards breaks the chain, so only consecutive un-answered
    resends collapse. Exact dups are no-ops (content-addressing)."""
    body = sys.stdin.read()
    if not body.strip():
        return 0
    bl = body.strip().lower()
    if bl in CONTROL_WORDS or bl.startswith("bump"):  # pure trigger (e.g. "bump the thread X")
        sys.stderr.write(f"[capture: control phrase {body.strip()!r}, not carded]\n")
        return 0
    view = _resolve_view(getattr(args, "view", None))
    rdir = records_dir(view.stem)
    cid = card_id(body)
    records = load_records(rdir)
    if cid in records:
        sys.stderr.write(f"[capture: exact dup {cid}, skipped]\n")
        return 0
    superseded = []
    while records:
        head = max(records.values(), key=_sort_key)
        if head.author == "fish" and _is_resend(head.body, body):
            (rdir / f"{head.id}.md").unlink(missing_ok=True)
            superseded.append(head.id)
            records = load_records(rdir)
        else:
            break
    reply_to = max(records.values(), key=_sort_key).id if records else None
    write_record(Card(id=cid, author="fish", captured_at=_next_ts(records),
                      reply_to=reply_to, flair="", body=body,
                      thread=f"[[{view.stem}]]"), rdir)
    records = load_records(rdir)
    if _is_thread(view):
        view.write_text(render_view(records, _clean_fm_block(view)), encoding="utf-8")
        _refresh_dirty(view)
        DASHBOARD.write_text(render_dashboard(), encoding="utf-8")
    sys.stderr.write(f"[captured {cid}; superseded {superseded or 'none'}]\n")
    return 0


def cmd_fold(args) -> int:
    view = _resolve_view(getattr(args, "view", None))
    if not _is_thread(view):
        print(f"{view.name}: not a stream thread — skipping")
        return 0
    made = fold_floating(view, load_records(records_dir(view.stem)))
    if not made:
        print("fold: no floating text to fold")
        return 0
    view.write_text(render_view(load_records(records_dir(view.stem)), _clean_fm_block(view)), encoding="utf-8")
    _refresh_dirty(view)
    for cid, rt in made:
        print(f"folded -> fish {cid} ({'reply to ' + rt if rt else 'new post (nonreply)'})")
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

    # to-do derives from the THREAD HEAD (robust, never stale): if the newest
    # card is the operator's, the agent owes a reply.
    views = [(v, parse_view(v.read_text(encoding="utf-8"))[1]) for v in find_stream_views()]
    o.append("## to-do — reply debt (agent's queue)")
    todo = [f"- `{v.stem}` — head is **{cards[-1].author}** ([[{cards[-1].printed_id}]]) → reply pending"
            for v, cards in views if cards and cards[-1].author == "fish"]
    o += todo if todo else ["nothing queued ✓"]
    o.append("")

    o.append("## threads")
    for v, cards in views:
        rel = v.relative_to(ROOT)
        if cards:
            h = cards[-1]
            o.append(f"- `{rel}` — {len(cards)} cards · head [[{h.printed_id}]] ({h.author})")
        else:
            o.append(f"- `{rel}` — empty")
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
    if getattr(args, "id", None):
        card = records.get(args.id)
        if card is None:
            print(f"no record {args.id}", file=sys.stderr)
            return 1
    else:
        card = max(records.values(), key=_sort_key)      # --last
    print(render_tui(card))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="stream-cards backend (enc:v1)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("id", help="enc:v1 id of a body read from stdin")
    sub.add_parser("validate", help="re-hash records + referential integrity")
    sub.add_parser("scan", help="vault-wide dirty pass (Brain): flag every thread + dirty.json")
    pr = sub.add_parser("render", help="records -> view (clears the dirty flag)")
    pr.add_argument("--view"); pr.add_argument("--check", action="store_true")
    pr.add_argument("--write", action="store_true")
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
    prec.add_argument("--view")
    pt = sub.add_parser("render-tui", help="print a card as the TUI callout (the reply frame)")
    pt.add_argument("--id", default=None, help="card id (default: latest)")
    pt.add_argument("--view", help="thread to read from (default: main)")
    pf = sub.add_parser("fold", help="deterministically fold floating text into fish cards")
    pf.add_argument("--view")
    pc = sub.add_parser("capture", help="capture a prompt as a fish card (collapses interrupt-spam)")
    pc.add_argument("--view")
    pdb = sub.add_parser("dashboard", help="compile .stream state -> DASHBOARD.md")
    pdb.add_argument("--write", action="store_true")
    args = ap.parse_args()
    return {"id": cmd_id, "validate": cmd_validate, "scan": cmd_scan,
            "render": cmd_render, "diff": cmd_diff, "extract": cmd_extract,
            "run": cmd_run, "record": cmd_record, "render-tui": cmd_render_tui,
            "fold": cmd_fold, "dashboard": cmd_dashboard,
            "capture": cmd_capture}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
