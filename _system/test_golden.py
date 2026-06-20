#!/usr/bin/env python3
"""
test_golden.py — the tests that must never go red (enc:v2, the Merkle-DAG).

The whole architecture rests on:
  1. enc:v2 is byte-stable     (id = sha256("enc:v2\\n" + sha256(normalize(body)) + "\\n" +
                                (reply_to or "ROOT")), a fixed point; punctuation-stable normalize)
  2. records are the truth     (render -> parse is a loss-free round-trip)
  3. the address commits to the parent — the store is a Merkle-DAG:
       same body + same parent  -> ONE card (root dedup survives)
       same body + diff  parent -> TWO distinct cards (the dedup-softening axiom)
plus the encoding is injection-proof and root-domain-separated (§1.4).

Self-contained: builds its own synthetic cards in a tempdir. Does NOT depend on any shipped
content, so it passes on a freshly-burned empty vault.

Run:  python3 _system/test_golden.py
"""
import hashlib
import io
import pathlib
import sys
import tempfile
from argparse import Namespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import stream  # noqa: E402


def main() -> int:
    fails: list[str] = []

    samples = [
        "the simplest possible body",
        "unicode: café — naïve — 日本語 — ✓",
        "multi\nline\n\nbody with a blank line",
        "trailing spaces   \nand a tab\tinside",
        "ok",                      # the short, repeatable body that used to collide
        "bare <task-notification> & `<id>` in a code span",  # view-escape round-trip
        "```\n<html> in a fence\n```\nand <bare> after",      # fence + bare token
        "inline $a < b$, display $$x > y$$, bare <tag>",      # math keeps <,>; prose escapes
        "block:\n$$\nP < 1\n$$\nthen <after>",                # multi-line $$ block
        "> [!quote] excerpt\n> quoted $a < b$\n\nmy note",    # nested callout (annotation shape)
        # a /latex suite entry: a railed `[!latex]` formulary box (math span + terms table) round-trips
        "intro prose\n\n> [!latex] Debye length\n> $$ \\lambda_D = \\sqrt{\\varepsilon_0 k_B T_e / (n_e q^2)} $$\n>\n> | Symbol | Name | Units | Typical Value |\n> |---|---|---|---|\n> | $\\lambda_D$ | Debye length | m | 1e-4 |\n\nclosing prose",
    ]

    # 1. enc:v2: normalize is a punctuation-stable fixed point; the id (over body+parent) reproduces.
    for s in samples:
        once = stream.normalize(s)
        if stream.normalize(once) != once:
            fails.append(f"idempotence: normalize not stable for {s!r}")
        if stream.card_id(s) != stream.card_id(stream.normalize(s)):
            fails.append(f"enc:v2: id not invariant under normalize for {s!r}")

    # 1a. punctuation-STABILITY of the id: a curly-quoted / NB-hyphenated body hashes IDENTICALLY to
    #     its straight-ASCII form (the v2 fold is now IN normalize). Emphasis (*/_) stays OUT (strict).
    straight = 'the "head" pinned, a non-breaking gap, a co-op'
    smart    = 'the “head” pinned, a non‑breaking gap, a co‑op'
    if smart == straight:
        fails.append("encfold: fixture not actually smartened (test is vacuous)")
    if stream.card_id(straight) != stream.card_id(smart):
        fails.append("encfold: a smart-quoted/NB-hyphen body must hash identically to its ASCII form")
    if stream.card_id("a *word* here") == stream.card_id("a _word_ here"):
        fails.append("encfold: emphasis (*/_) must NOT fold into the id (it's intentional content)")

    # 1b. canonical-encoding pin (verbatim preimage — a re-delimit fails loud) + the receipt property.
    for body, parent in (("hello", None), ("a reply", "a" * 64)):
        body_hash = hashlib.sha256(stream.normalize(body).encode("utf-8")).hexdigest()
        preimage = "enc:v2\n" + body_hash + "\n" + (parent or "ROOT")
        expect = hashlib.sha256(preimage.encode("utf-8")).hexdigest()
        if stream.card_id(body, parent) != expect:
            fails.append(f"encoding: card_id({body!r},{parent!r}) is not the pinned preimage hash")
        if len(stream.card_id(body, parent)) != 64:
            fails.append("encoding: a v2 id must be full 64-hex (256-bit)")

    # 1c. root domain-separation: parent=None ("ROOT") can never alias a child with a real-id parent.
    if stream.card_id("body", None) == stream.card_id("body", "f" * 64):
        fails.append("rootdomain: a root id must differ from the same body under a real parent")

    # 1d. injection: the body is committed via its OWN hash, so body bytes can't masquerade as the
    #     parent field. A body that ENDS in a hex-shaped tail (parent=None) != that tail-as-parent.
    tail = "a" * 64
    if stream.card_id("msg\n" + tail, None) == stream.card_id("msg", tail):
        fails.append("injection: a hex-tailed body must not collide with that tail used as the parent")

    # 2. render -> parse round-trip preserves every id, byte-exact, no floating. (All samples are
    #    roots: reply_to=None, so id = card_id(body, None).)
    records = {}
    for i, s in enumerate(samples):
        cid = stream.card_id(s, None)
        records[cid] = stream.Card(id=cid, author="fish", captured_at=f"2026-06-15 (mon)-12:00:0{i}",
                                   flair="", body=s)
    rendered = stream.render_view(records, "type: stream")
    _, parsed, floating = stream.parse_view(rendered)
    if floating:
        fails.append(f"roundtrip: {len(floating)} stray floating lines after render")
    if len(parsed) != len(records):
        fails.append(f"roundtrip: rendered {len(parsed)} cards, expected {len(records)}")
    for pc in parsed:
        if pc.mutated:
            fails.append(f"roundtrip: {pc.printed_id} not self-consistent after render")
        if pc.printed_id not in records:
            fails.append(f"roundtrip: {pc.printed_id} not a known record")
    # the full 64-hex id is the link target; the 8-char prefix is the displayed alias.
    a_id = next(iter(records))
    if f"[[{a_id}|{stream.short_id(a_id)}]]" not in rendered:
        fails.append("display: link must be [[<full64>|<short8>]] (full target, short label)")

    # 2b. view-escape semantics: math spans keep literal <,>; '<' in prose escapes; '>' never does.
    vcard = stream.Card(id="deadbeef", author="fish", captured_at="2026-06-15 (mon)-12:00:00",
                        flair="", body="$a < b$ and $$x > y$$ but a bare <tag> here")
    vrender = stream.render_card(vcard)
    for must in ("$a < b$", "$$x > y$$", "&lt;tag>"):
        if must not in vrender:
            fails.append(f"view-escape: expected {must!r} in rendered callout")
    if "&lt;" in vrender.replace("&lt;tag>", ""):
        fails.append("view-escape: a '<' escaped inside a math span")
    if "&gt;" in vrender:
        fails.append("view-escape: '>' was escaped (would break nested-callout excerpts)")

    # 2c. a leading '>' in a body survives as a NESTED callout (the excerpt-quote shape).
    ncard = stream.Card(id="deadbee2", author="fish", captured_at="2026-06-15 (mon)-12:00:00",
                        flair="", body="> [!quote] excerpt\n> quoted $a < b$")
    if "> > [!quote] excerpt" not in stream.render_card(ncard):
        fails.append("view-escape: leading '>' must nest (got a flattened/escaped quote)")

    # 2e. /latex suite: a railed `[!latex]` formulary entry inside a persona card nests as a boxed
    #     `[!latex]` callout (render class), and the math span + terms table survive verbatim. This is
    #     the B-form latex card — the equation wears the box, the persona keeps the voice.
    lcard = stream.Card(id="deadbee5", author="shizu", captured_at="2026-06-15 (mon)-12:00:00",
                        flair="", body="> [!latex] Debye length\n> $$ \\lambda_D = \\sqrt{x} $$\n>\n> | Symbol | Name | Units | Typical Value |")
    lrender = stream.render_card(lcard)
    if "> > [!latex] Debye length" not in lrender:
        fails.append("latex: a railed [!latex] entry must nest as a boxed callout under the persona")
    if "> > $$ \\lambda_D = \\sqrt{x} $$" not in lrender:
        fails.append("latex: the display-math span was altered (must survive verbatim, '<'/'>' untouched)")
    if "> > | Symbol | Name | Units | Typical Value |" not in lrender:
        fails.append("latex: the terms table row was altered (the '|' table must survive verbatim)")
    # (the full render->parse id round-trip for a [!latex] entry is pinned by the latex `samples` row above)

    # 2d. structural nav: render emits a Jump-to-Bottom header link to the LAST card's anchor; the view
    #     parser SKIPS it (it is never floating text, never a card body).
    last_id = sorted(records.values(), key=stream._sort_key)[-1].id
    if f"[[#^{last_id}|⤓ Jump to Bottom]]" not in rendered:
        fails.append("nav: render must emit a Jump-to-Bottom header link to the latest card's anchor")
    if any("Jump to Bottom" in (pc.body or "") for pc in parsed):
        fails.append("nav: the nav link leaked into a card body (the parser did not skip it)")

    # 3. dedup SPLITS on the parent (the v2 axiom). (3a) same body + same parent -> ONE pooled card;
    #    (3b) same body + DIFFERENT parent -> TWO distinct ids, two files; a rootless thread is empty.
    with tempfile.TemporaryDirectory() as td:
        stream.CARDS_DIR = pathlib.Path(td) / "cards"
        stream.THREADS_DIR = pathlib.Path(td) / "threads"
        root_body = "the conversation root"
        rid = stream.card_id(root_body, None)
        stream.write_record(stream.Card(id=rid, author="fish", captured_at="2026-06-15 (mon)-12:00:00",
                            body=root_body), "a")
        # (3a) the SAME (body, parent) recorded twice into two threads = one pooled file, both include it
        body = "ok"
        c_same = stream.card_id(body, rid)
        stream.write_record(stream.Card(id=c_same, author="fish", captured_at="2026-06-15 (mon)-12:00:01",
                            reply_to=rid, body=body), "a")
        stream.THREADS_DIR.joinpath("b.md")  # touch nothing; b shares the same root
        stream._write_manifest("b", {"root": rid, "render": "scroll"})
        if len(list((pathlib.Path(td) / "cards").glob("*.md"))) != 2:  # root + the shared reply
            fails.append("dedup3a: same (body,parent) should be ONE pooled card, not duplicated")
        if c_same not in stream.load_records("a") or c_same not in stream.load_records("b"):
            fails.append("dedup3a: both threads (same root) should include the shared reply")
        # (3b) same body, DIFFERENT parent -> a distinct id, a distinct file (the softening axiom)
        other_parent = "p" * 64
        c_diff = stream.card_id(body, other_parent)
        if c_diff == c_same:
            fails.append("dedup3b: same body under a different parent must yield a DIFFERENT id")
        if stream.load_records("never") != {}:
            fails.append("dedup3b: a thread with no root is not empty")

    # 4. annotation harvest: a `…`-sigil note lifts into a fish reply quoting the excerpt; a non-sigil
    #    edit is NOT harvested; the host is never the carrier.
    rec = "## Step 2 — plasma\n\n$$u_B = 3$$\n\nnext para"
    vw = "## Step 2 — plasma\n\n$$u_B = 3$$\n`my note`\n\nnext para"
    anns = stream._extract_annotations(rec, vw)
    if len(anns) != 1 or anns[0]["note"] != "my note":
        fails.append(f"harvest: expected one note 'my note', got {anns}")
    elif anns[0]["excerpt"] != "$$u_B = 3$$" or anns[0]["section"] != "Step 2 — plasma":
        fails.append(f"harvest: wrong anchor {anns[0]}")
    else:
        hbody = stream._annotation_card_body("abc12345", anns[0])
        hrender = stream.render_card(stream.Card(id="deadbee3", author="fish",
                  captured_at="2026-06-15 (mon)-12:00:00", flair="", body=hbody))
        if "> > [!quote] abc12345 · Step 2 — plasma" not in hrender:
            fails.append("harvest: excerpt is not a nested quote callout labelled by section")
        if "> > $$u_B = 3$$" not in hrender:
            fails.append("harvest: excerpt body missing from the harvested card")
        if "> my note" not in hrender:
            fails.append("harvest: the note is missing from the harvested card")
    if stream._extract_annotations("a\nb", "a\nedited b\nc"):
        fails.append("harvest: a non-sigil edit must NOT be harvested")

    # 4b. code-highlight: an inline `…` span (not a whole line) is the excerpt; already-code / whole-line
    #     spans are not highlights.
    hrec = "the quick brown fox jumps"
    hvw = "the quick `brown fox` jumps"
    hls = stream._extract_highlights(hrec, hvw)
    if [h["excerpt"] for h in hls] != ["brown fox"]:
        fails.append(f"highlight: expected ['brown fox'], got {[h['excerpt'] for h in hls]}")
    else:
        qbody = stream._highlight_card_body("abc12345", hls[0])
        qrender = stream.render_card(stream.Card(id="deadbee4", author="fish",
                  captured_at="2026-06-15 (mon)-12:00:00", flair="", body=qbody))
        if "> > [!quote] abc12345" not in qrender or "> > brown fox" not in qrender:
            fails.append("highlight: excerpt is not a nested quote of exactly the span")
    if stream._extract_highlights("x `kept` y", "x `kept` y"):
        fails.append("highlight: a span already code in the record must NOT be a highlight")
    if stream._extract_highlights("note here", "`note here`"):
        fails.append("highlight: a whole-line span is a sigil note, not a highlight")

    # 5. no-scrub: a floating draft is FOLDED into a card by the reconcile before any append re-renders.
    with tempfile.TemporaryDirectory() as td:
        stream.CARDS_DIR = pathlib.Path(td) / "cards"
        stream.THREADS_DIR = pathlib.Path(td) / "threads"
        view = pathlib.Path(td) / "draft.md"
        rdir = stream.records_dir("draft")
        hid = stream.card_id("a host card", None)
        stream.write_record(stream.Card(id=hid, author="claude-tui",
                            captured_at="2026-06-15 (mon)-12:00:00", body="a host card"), rdir)
        base = stream.render_view(stream.load_records(rdir), "type: stream")
        view.write_text(base + "\nmy staged draft reply\n", encoding="utf-8")   # operator drafts
        before = len(stream.load_records(rdir))
        stream._reconcile_view(view)                                            # the fix
        after = stream.load_records(rdir)
        if len(after) != before + 1:
            fails.append(f"no-scrub: draft not folded into a card (had {before}, now {len(after)})")
        elif not any("my staged draft reply" in c.body for c in after.values()):
            fails.append("no-scrub: the folded card does not contain the draft text")

    # 6. pull: a code-highlight is extracted into a ``` codeblock below the --- (scrub above, append
    #    below); re-running is a no-op; the scaffold survives a reconcile while prose beside it folds.
    with tempfile.TemporaryDirectory() as td:
        stream.CARDS_DIR = pathlib.Path(td) / "cards"
        stream.THREADS_DIR = pathlib.Path(td) / "threads"
        view = pathlib.Path(td) / "ex.md"
        rdir = stream.records_dir("ex")
        cid = stream.card_id("renka likes lychee with zero regrets here", None)
        stream.write_record(stream.Card(id=cid, author="renka", captured_at="2026-06-15 (mon)-12:00:00",
                            body="renka likes lychee with zero regrets here"), rdir)
        base = stream.render_view(stream.load_records(rdir), "type: stream")
        view.write_text(base.replace("zero regrets", "`zero regrets`"), encoding="utf-8")
        n0 = len(stream.load_records(rdir))
        stream._reconcile_view(view)
        stream._render_keep_scaffolds(view, stream.load_records(rdir))
        if "`zero regrets`" not in view.read_text(encoding="utf-8"):
            fails.append("pull: a code-highlight did not survive a reconcile (it was scrubbed)")
        if len(stream.load_records(rdir)) != n0:
            fails.append("pull: a reconcile harvested the highlight into a card (it belongs to pull)")
        r1 = stream.pull_highlights(view)
        out1 = view.read_text(encoding="utf-8")
        if r1["found"] != 1 or [e for _, e in r1["appended"]] != ["zero regrets"]:
            fails.append(f"pull: first run did not extract the highlight: {r1}")
        if f"```\n{cid}\nzero regrets\n```" not in out1:
            fails.append("pull: codeblock (id + excerpt) was not appended below the line")
        if "`zero regrets`" in out1:
            fails.append("pull: the highlight was not scrubbed from the card above the line")
        r2 = stream.pull_highlights(view)                       # spam the button
        if r2["found"] != 0 or view.read_text(encoding="utf-8") != out1:
            fails.append("pull: second run was not a no-op (idempotency)")
        view.write_text(view.read_text(encoding="utf-8") + "\na prose draft beside it\n", encoding="utf-8")
        stream._reconcile_view(view)
        stream._render_keep_scaffolds(view, stream.load_records(rdir))
        final = view.read_text(encoding="utf-8")
        if f"```\n{cid}\nzero regrets\n```" not in final:
            fails.append("pull: scaffold did not survive a reconcile + re-render")
        if not any("a prose draft beside it" in c.body for c in stream.load_records(rdir).values()):
            fails.append("pull: prose beside a scaffold was not folded into a card")

    # 7. gel: an annotated scaffold below the --- gels into a fish quote-reply card (codeblock -> nested
    #    callout in place); a bare scaffold gels; a scaffold before a later --- bar break still gels.
    with tempfile.TemporaryDirectory() as td:
        stream.CARDS_DIR = pathlib.Path(td) / "cards"
        stream.THREADS_DIR = pathlib.Path(td) / "threads"
        view = pathlib.Path(td) / "g.md"
        rdir = stream.records_dir("g")
        ref = stream.card_id("a card worth quoting", None)
        stream.write_record(stream.Card(id=ref, author="shizu", captured_at="2026-06-15 (mon)-12:00:00",
                            body="a card worth quoting"), rdir)
        base = stream.render_view(stream.load_records(rdir), "type: stream")
        view.write_text(base + f"\ni pulled this:\n```\n{ref}\nworth quoting\n```\n\nand here's my take\n",
                        encoding="utf-8")
        gelled = stream.gel_scaffolds(view, stream.load_records(rdir))
        g = stream.load_records(rdir).get(gelled[0][0]) if gelled else None
        if len(gelled) != 1 or not g or g.reply_to != ref:
            fails.append(f"gel: composed post did not gel into one quote-reply to the ref: {gelled}")
        elif not all(s in g.body for s in ("i pulled this:", f"> [!shizu] shizu | [[{ref}|{stream.short_id(ref)}]]", "worth quoting", "and here's my take")):
            fails.append(f"gel: composed card missing lead-in, nested quote, or commentary: {g.body!r}")
        elif "```" in g.body:
            fails.append("gel: the codeblock did NOT convert into a nested callout (still a fence)")
        if "```" in view.read_text(encoding="utf-8").rsplit("\n---\n", 1)[-1]:
            fails.append("gel: the staging area was not consumed")
        view.write_text(base + f"\n```\n{ref}\njust the quote\n```\n", encoding="utf-8")   # bare = ref only
        bare = stream.gel_scaffolds(view, stream.load_records(rdir))
        bg = stream.load_records(rdir).get(bare[0][0]) if bare else None
        if not bare or not bg or f"> [!shizu] shizu | [[{ref}|{stream.short_id(ref)}]]" not in bg.body:
            fails.append("gel: a bare scaffold (no prose) did not gel into a quote-only card")
        view.write_text(base + f"\n```\n{ref}\nquoted bit\n```\nmy reply\n---\njust a trailing note\n", encoding="utf-8")
        multi = stream.gel_scaffolds(view, stream.load_records(rdir))
        gm = stream.load_records(rdir).get(multi[0][0]) if multi else None
        if len(multi) != 1 or not gm or f"> [!shizu] shizu | [[{ref}|{stream.short_id(ref)}]]" not in gm.body or "my reply" not in gm.body:
            fails.append(f"gel: a scaffold before the last --- bar break did not gel: {multi}")
        if "just a trailing note" not in view.read_text(encoding="utf-8"):
            fails.append("gel: a trailing no-scaffold post was dropped instead of kept for fold")

    # 8. fan-out: a block typed beneath a card's ^caret replies to THAT card (branching), not the head;
    #    the branch's id != the root's id (different parent AND body).
    with tempfile.TemporaryDirectory() as td:
        stream.CARDS_DIR = pathlib.Path(td) / "cards"
        stream.THREADS_DIR = pathlib.Path(td) / "threads"
        view = pathlib.Path(td) / "f.md"
        rdir = stream.records_dir("f")
        a = stream.card_id("card A", None)                                   # the root
        b = stream.card_id("card B", a)                                      # head, replies to A
        stream.write_record(stream.Card(id=a, author="fish", captured_at="2026-06-15 (mon)-12:00:01", body="card A"), rdir)
        stream.write_record(stream.Card(id=b, author="fish", captured_at="2026-06-15 (mon)-12:00:02", reply_to=a, body="card B"), rdir)
        rendered = stream.render_view(stream.load_records(rdir), "type: stream")  # A then B (B = head)
        view.write_text(rendered.replace(f"^{a}\n", f"^{a}\n\nreply beneath A\n", 1), encoding="utf-8")
        stream.fold_floating(view, stream.load_records(rdir))
        branch = next((c for c in stream.load_records(rdir).values() if c.body.strip() == "reply beneath A"), None)
        if not branch or branch.reply_to != a:
            fails.append(f"fanout: block beneath A's caret did not reply to A (got {getattr(branch,'reply_to','-')}, head={b})")
        elif branch.id in (a, b):
            fails.append("fanout: the branch card's id must differ from the root/head ids")

    # 9. fork = a derive (subtree) manifest, resolved live: subtree(B) = {B, C}, not A/D. Forking does
    #    NOT re-address — a forked root keeps its GLOBAL id (parent-in-preimage is the global parent,
    #    not local-None). clone is GONE under v2 (a thread is its subtree; a second name can't diverge).
    with tempfile.TemporaryDirectory() as td:
        stream.CARDS_DIR = pathlib.Path(td) / "cards"
        stream.THREADS_DIR = pathlib.Path(td) / "threads"
        def _mk(body, parent, ts):
            cid = stream.card_id(body, parent)
            stream.write_record(stream.Card(id=cid, author="fish", captured_at=ts,
                                            reply_to=parent, body=body), "src")
            return cid
        a = _mk("root A",    None, "2026-06-15 (mon)-12:00:01")
        b = _mk("reply B",   a,    "2026-06-15 (mon)-12:00:02")
        c = _mk("reply C",   b,    "2026-06-15 (mon)-12:00:03")
        d = _mk("sibling D", a,    "2026-06-15 (mon)-12:00:04")     # branches off A, not under B
        stream._write_manifest("forked", {"root": b, "render": "scroll"})
        if set(stream.load_records("forked")) != {b, c}:           # B + descendant C; NOT A or D
            fails.append(f"fork: subtree(B) should be {{B,C}}, got {sorted(stream.load_records('forked'))}")
        if stream.load_records("forked")[b].id != b:
            fails.append("fork: forking re-addressed the root (must keep its global id)")
        if b != stream.card_id("reply B", a):
            fails.append("fork: a forked root's id must be hash(body, GLOBAL parent), not local-None")

    # 10. non-destructive re-render: a draft below the staging --- survives a plain re-render, while an
    #     in-view CARD-body edit is discarded (rule #1); the HARD path dissolves both.
    with tempfile.TemporaryDirectory() as td:
        stream.CARDS_DIR = pathlib.Path(td) / "cards"
        stream.THREADS_DIR = pathlib.Path(td) / "threads"
        view = pathlib.Path(td) / "keep.md"
        rdir = stream.records_dir("keep")
        cid = stream.card_id("canonical body", None)
        stream.write_record(stream.Card(id=cid, author="claude-tui",
                            captured_at="2026-06-15 (mon)-12:00:00", body="canonical body"), rdir)
        base = stream.render_view(stream.load_records(rdir), "type: stream")
        view.write_text(base.replace("canonical body", "TAMPERED body") + "\nmy uncommitted draft\n",
                        encoding="utf-8")
        out = stream._render_preserving(view, stream.load_records(rdir), "type: stream")
        if "TAMPERED" in out:
            fails.append("substrate: an in-view card-body edit must be discarded on re-render (rule #1)")
        if "canonical body" not in out:
            fails.append("substrate: the card body was not restored from the pool")
        if "my uncommitted draft" not in out:
            fails.append("substrate: the staging draft did not survive the re-render")
        hard = stream.render_view(stream.load_records(rdir), "type: stream")
        if "my uncommitted draft" in hard or "TAMPERED" in hard:
            fails.append("substrate: a HARD reset (flask/Restore) must dissolve the staging draft AND the edit")

    # 11. locate: a bare excerpt -> its source card id(s) by CONTENT, substring-scan only (the v1 O(1)
    #     `exact` tier is gone — can't hash a bare body without its parent). Full body, partial span,
    #     non-present, and a shared (ambiguous) span.
    with tempfile.TemporaryDirectory() as td:
        stream.CARDS_DIR = pathlib.Path(td) / "cards"
        stream.THREADS_DIR = pathlib.Path(td) / "threads"
        b1 = "the hipims target erodes in a racetrack pattern under the magnetron"
        b2 = "a racetrack pattern also shows up in tokamak limiter wear"     # shares 'racetrack pattern'
        c1, c2 = stream.card_id(b1, None), stream.card_id(b2, None)
        for cid, b in ((c1, b1), (c2, b2)):
            stream.write_record(stream.Card(id=cid, author="claude-tui",
                                captured_at="2026-06-15 (mon)-12:00:00", body=b), "loc")
        if stream._locate(b1) != ("contains", [c1]):
            fails.append("locate: a full body must resolve `contains` to its id (no O(1) exact tier)")
        if stream._locate("erodes in a racetrack") != ("contains", [c1]):
            fails.append("locate: a unique partial excerpt must resolve to its source card")
        if stream._locate("zirconium plasma sheath")[1] != []:
            fails.append("locate: a non-present excerpt must not match")
        if set(stream._locate("racetrack pattern")[1]) != {c1, c2}:
            fails.append("locate: a shared excerpt must return all matches (ambiguous), not guess one")

    # 12. lane binding (the load-bearing enc:v2 case): a reply binds to ITS session's lane tip (explicit
    #     parent, not a guess), concurrent lanes don't cross, reply-debt is per fish-LEAF, a session-less
    #     capture falls back to the head, and the SAME body in two different lanes (two parents) is TWO
    #     distinct cards — never a silent cross-lane dedup that would drop one operator's input.
    _saved = (stream.ROOT, stream.DASHBOARD, stream.DIRTY_INDEX, stream.SESSIONS_DIR,
              stream.find_stream_views)
    try:
        with tempfile.TemporaryDirectory() as td:
            td = pathlib.Path(td)
            stream.CARDS_DIR = td / "cards"; stream.THREADS_DIR = td / "threads"
            stream.SESSIONS_DIR = td / "sessions"
            stream.CHANGESETS_DIR = td / "cs"; stream.ROOT = td
            stream.DASHBOARD = td / "dash.md"; stream.DIRTY_INDEX = td / "dirty.json"
            view = td / "main.md"; view.write_text("---\ntype: stream\n---\n", encoding="utf-8")
            stream.find_stream_views = lambda root=None: [view]

            def _cap(sid, body):
                sys.stdin = io.StringIO(body)
                stream.cmd_capture(Namespace(view=str(view), session=sid))

            def _rec(sid, body, head=True):
                sys.stdin = io.StringIO(body)
                stream.cmd_record(Namespace(view=str(view), session=sid, author="claude-tui",
                                            reply_to=None, reply_head=head, flair="", ts=None))

            def _by(body):   # look up a card by its body (ids are parent-dependent, can't recompute blind)
                return next(c for c in stream.load_records(stream.records_dir("main")).values()
                            if c.body.strip() == body)

            _rec(None, "lane root", head=False)            # seed (root)
            _cap("A", "a1"); _cap("B", "b1")               # fresh lanes -> head fallback
            _rec("A", "ar1"); _rec("B", "br1")             # replies bind to own lane prompt
            _cap("A", "a2"); _cap("B", "b2")               # follow-ups bind to own lane tip
            if _by("ar1").reply_to != _by("a1").id:
                fails.append("lane: a reply must bind to its session's prompt, not the global head")
            if _by("br1").reply_to != _by("b1").id:
                fails.append("lane: concurrent lanes must not cross on reply")
            if _by("a2").reply_to != _by("ar1").id or _by("b2").reply_to != _by("br1").id:
                fails.append("lane: a follow-up prompt must bind to its lane tip, not the global head")
            debt = sorted(c.id for c in stream._reply_debt(
                stream.load_records(stream.records_dir("main"))))
            if debt != sorted([_by("a2").id, _by("b2").id]):
                fails.append("lane: reply-debt must list every fish LEAF (one per open lane)")
            _cap(None, "no-session")                       # no session -> prior global head
            if _by("no-session").reply_to != _by("b2").id:
                fails.append("lane: a session-less capture must fall back to the global head")
            # same body in two lanes -> two distinct parents -> two distinct cards, both owed as debt.
            _cap("A", "dup"); _cap("B", "dup")
            dups = [c for c in stream.load_records(stream.records_dir("main")).values()
                    if c.body.strip() == "dup"]
            if len({c.id for c in dups}) != 2:
                fails.append("lane: same body in two lanes must be TWO distinct cards (no cross-lane dedup)")
            debt2 = {c.id for c in stream._reply_debt(stream.load_records(stream.records_dir("main")))}
            if not {c.id for c in dups} <= debt2:
                fails.append("lane: both same-body-different-parent leaves must surface as debt")
    finally:
        (stream.ROOT, stream.DASHBOARD, stream.DIRTY_INDEX, stream.SESSIONS_DIR,
         stream.find_stream_views) = _saved

    # 13. locate punctuation/emphasis tolerance (MATCH-only, looser than the id): a hand-pasted excerpt
    #     whose editor smartened the quotes/hyphen (folded into the id now) AND flipped emphasis
    #     `*`->`_` (NOT folded into the id) still resolves to its straight-ASCII `*` source via _cmp_fold.
    with tempfile.TemporaryDirectory() as td:
        stream.CARDS_DIR = pathlib.Path(td) / "cards"
        stream.THREADS_DIR = pathlib.Path(td) / "threads"
        src = 'the *doc-link* edge with the "head" pinned'                    # straight-ASCII, * emphasis
        sid = stream.card_id(src, None)
        stream.write_record(stream.Card(id=sid, author="renka",
                            captured_at="2026-06-15 (mon)-12:00:00", body=src), "p")
        smart = 'the _doc‑link_ edge with the “head” pinned'             # _ emphasis + smart punct
        if smart == src:
            fails.append("locatefold: fixture is not actually smartened (test is vacuous)")
        if stream._locate(smart) != ("contains", [sid]):
            fails.append(f"locatefold: a smart/emphasis-flipped excerpt must still locate its source, got {stream._locate(smart)}")
        if stream.card_id(smart) == sid:
            fails.append("locatefold: emphasis differs, so the id must NOT match (locate is looser than the id)")

    # 14. acyclic-by-construction (the v2 integrity guard; the soft-seal edge digest is GONE — the edge
    #     is IN the id, so a rewrite is just a hash mismatch). `_find_cycles` flags any reply_to cycle.
    def _C(cid, parent=None):
        return stream.Card(id=cid, author="fish", captured_at="2026-06-15 (mon)-12:00:00",
                           reply_to=parent, body=cid)
    dag = {"a" * 64: _C("a" * 64), "b" * 64: _C("b" * 64, "a" * 64), "c" * 64: _C("c" * 64, "b" * 64)}
    if stream._find_cycles(dag):
        fails.append("acyclic: a DAG must report no cycles")
    cyclic = dict(dag); cyclic["a" * 64] = _C("a" * 64, "c" * 64)             # a->c->b->a
    if set(stream._find_cycles(cyclic)) != {"a" * 64, "b" * 64, "c" * 64}:
        fails.append(f"acyclic: a reply_to cycle must be detected, got {stream._find_cycles(cyclic)}")

    # 15. nested-codeblock highlight: an excerpt that itself CONTAINS a ``` fence is captured in FULL
    #     and rides a variable-length fence through pull-emit -> gel-read intact.
    rec15 = "the diff:\n```\ncode_inside()\n```\ndone"
    view15 = "`the diff:\n```\ncode_inside()\n```\ndone`"          # operator wraps the whole span in ` `
    h15 = stream._extract_highlights(rec15, view15)
    if len(h15) != 1 or h15[0]["excerpt"] != rec15:
        fails.append(f"nestfence: a highlight containing a ``` block must capture the WHOLE span, got {h15}")
    elif len(stream._fence(h15[0]["excerpt"])) < 4:
        fails.append(f"nestfence: scaffold fence must out-length the inner ``` run, got {stream._fence(h15[0]['excerpt'])!r}")
    else:
        with tempfile.TemporaryDirectory() as td:
            stream.CARDS_DIR = pathlib.Path(td) / "cards"
            stream.THREADS_DIR = pathlib.Path(td) / "threads"
            view = pathlib.Path(td) / "nf.md"
            rdir = stream.records_dir("nf")
            ref = stream.card_id(rec15, None)
            stream.write_record(stream.Card(id=ref, author="shizu", captured_at="2026-06-15 (mon)-12:00:00",
                                body=rec15), rdir)
            base = stream.render_view(stream.load_records(rdir), "type: stream")
            fnc = stream._fence(h15[0]["excerpt"])
            view.write_text(base + f"\n{fnc}\n{ref}\n{h15[0]['excerpt']}\n{fnc}\n", encoding="utf-8")
            gel15 = stream.gel_scaffolds(view, stream.load_records(rdir))
            g15 = stream.load_records(rdir).get(gel15[0][0]) if gel15 else None
            if not gel15 or not g15 or g15.reply_to != ref:
                fails.append(f"nestfence: variable-fence scaffold did not gel to a quote-reply: {gel15}")
            elif "code_inside()" not in g15.body or f"> [!shizu] shizu | [[{ref}|{stream.short_id(ref)}]]" not in g15.body:
                fails.append(f"nestfence: inner codeblock content lost through gel: {g15.body!r}")

    # 16. newline-vs-barline (the fix): blank lines are INTERNAL whitespace of ONE card; only a bare
    #     `---` barline splits a staged zone. (Two paragraphs -> one card, blank kept; --- -> two cards.)
    with tempfile.TemporaryDirectory() as td:
        stream.CARDS_DIR = pathlib.Path(td) / "cards"
        stream.THREADS_DIR = pathlib.Path(td) / "threads"
        view = pathlib.Path(td) / "z.md"
        rdir = stream.records_dir("z")
        hid = stream.card_id("zone host", None)
        stream.write_record(stream.Card(id=hid, author="claude-tui",
                            captured_at="2026-06-15 (mon)-12:00:00", body="zone host"), rdir)
        base = stream.render_view(stream.load_records(rdir), "type: stream")
        view.write_text(base + "\npara one\n\npara two\n", encoding="utf-8")
        stream.fold_floating(view, stream.load_records(rdir))
        fish = [c for c in stream.load_records(rdir).values() if c.author == "fish"]
        if len(fish) != 1:
            fails.append(f"zonefold: a blank-separated multi-paragraph post must fold to ONE card, got {len(fish)}")
        elif fish[0].body.strip() != "para one\n\npara two":   # .strip() drops the disk leading-\n artifact
            fails.append(f"zonefold: the internal blank line was not preserved as a paragraph break: {fish[0].body!r}")
    with tempfile.TemporaryDirectory() as td:
        stream.CARDS_DIR = pathlib.Path(td) / "cards"
        stream.THREADS_DIR = pathlib.Path(td) / "threads"
        view = pathlib.Path(td) / "z2.md"
        rdir = stream.records_dir("z2")
        hid = stream.card_id("zone host", None)
        stream.write_record(stream.Card(id=hid, author="claude-tui",
                            captured_at="2026-06-15 (mon)-12:00:00", body="zone host"), rdir)
        base = stream.render_view(stream.load_records(rdir), "type: stream")
        view.write_text(base + "\nfirst post\n---\nsecond post\n", encoding="utf-8")
        stream.fold_floating(view, stream.load_records(rdir))
        bodies = sorted(c.body.strip() for c in stream.load_records(rdir).values() if c.author == "fish")
        if bodies != ["first post", "second post"]:
            fails.append(f"barlinesplit: a bare --- must split a zone into two cards, got {bodies}")

    # 16b. nav skip is STRUCTURAL, not a loose prefix: an operator-typed `> [!nav] …` line (NOT the
    #      render-emitted block-link) is ordinary content and must NOT be silently dropped (append-only).
    with tempfile.TemporaryDirectory() as td:
        stream.CARDS_DIR = pathlib.Path(td) / "cards"
        stream.THREADS_DIR = pathlib.Path(td) / "threads"
        view = pathlib.Path(td) / "zn.md"
        rdir = stream.records_dir("zn")
        hid = stream.card_id("nav host", None)
        stream.write_record(stream.Card(id=hid, author="claude-tui",
                            captured_at="2026-06-15 (mon)-12:00:00", body="nav host"), rdir)
        base = stream.render_view(stream.load_records(rdir), "type: stream")
        view.write_text(base + "\n> [!nav] keep me\nand this line too\n", encoding="utf-8")
        stream.fold_floating(view, stream.load_records(rdir))
        kept = [c for c in stream.load_records(rdir).values() if c.author == "fish"]
        if not any("keep me" in c.body for c in kept):
            fails.append(f"navguard: an operator-typed [!nav] line was silently dropped (append-only violation): {[c.body for c in kept]}")

    print(f"samples under test: {len(samples)}")
    print(f"[1] enc:v2 fixed point + punc/encoding/root/injection pins "
          f"{'ok' if not any(f.startswith(('idempotence','enc:v2','encfold','encoding','rootdomain','injection')) for f in fails) else 'FAIL'}")
    print(f"[2] render round-trip + display{'  ok' if not any(f.startswith(('roundtrip','display')) for f in fails) else '  FAIL'}")
    print(f"[3] dedup splits on parent {'ok' if not any(f.startswith('dedup') for f in fails) else 'FAIL'}")
    print(f"[4] annotation harvest   {'ok' if not any(f.startswith(('harvest', 'highlight')) for f in fails) else 'FAIL'}")
    print(f"[5] no-scrub preserve    {'ok' if not any(f.startswith('no-scrub') for f in fails) else 'FAIL'}")
    print(f"[6] pull extraction      {'ok' if not any(f.startswith('pull') for f in fails) else 'FAIL'}")
    print(f"[7] gel quote-reply      {'ok' if not any(f.startswith('gel') for f in fails) else 'FAIL'}")
    print(f"[8] fan-out beneath caret{'  ok' if not any(f.startswith('fanout') for f in fails) else '  FAIL'}")
    print(f"[9] fork = derive subtree{'  ok' if not any(f.startswith('fork') for f in fails) else '  FAIL'}")
    print(f"[10] non-destructive render{'  ok' if not any(f.startswith('substrate') for f in fails) else '  FAIL'}")
    print(f"[11] locate excerpt->id  {'ok' if not any(f.startswith('locate') and not f.startswith('locatefold') for f in fails) else 'FAIL'}")
    print(f"[12] lane binding + debt {'ok' if not any(f.startswith('lane') for f in fails) else 'FAIL'}")
    print(f"[13] locate punct/emph fold {'ok' if not any(f.startswith('locatefold') for f in fails) else 'FAIL'}")
    print(f"[14] acyclic-by-construction {'ok' if not any(f.startswith('acyclic') for f in fails) else 'FAIL'}")
    print(f"[15] nested-fence pull   {'ok' if not any(f.startswith('nestfence') for f in fails) else 'FAIL'}")
    print(f"[16] newline-vs-barline fold {'ok' if not any(f.startswith(('zonefold','barlinesplit')) for f in fails) else 'FAIL'}")
    print(f"[17] structural jump-nav {'ok' if not any(f.startswith('nav') for f in fails) else 'FAIL'}")
    print(f"[18] latex formulary box {'ok' if not any(f.startswith('latex') for f in fails) else 'FAIL'}")
    if fails:
        print("\nFAILURES:")
        for f in fails:
            print(f"  ✗ {f}")
        print("\nGOLDEN TESTS FAILED ✗")
        return 1
    print("\nALL GOLDEN TESTS PASS ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
