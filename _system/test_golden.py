#!/usr/bin/env python3
"""
test_golden.py — the tests that must never go red.

The whole architecture rests on two properties:
  1. enc:v1 is byte-stable     (id = sha256(normalize(body))[:8], a fixed point)
  2. records are the truth     (render -> parse is a loss-free round-trip)
plus the property that makes the team/multi-thread case safe:
  3. threads are isolated      (a card lives in its thread only; the same body
                                in two threads is two files, not a dropped one)

Self-contained: builds its own synthetic cards in a tempdir. Does NOT depend on
any shipped content, so it passes on a freshly-cloned empty vault.

Run:  python3 _system/test_golden.py
"""
import pathlib
import sys
import tempfile

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
    ]

    # 1. enc:v1: normalize is a fixed point, and the id is reproducible.
    for s in samples:
        once = stream.normalize(s)
        if stream.normalize(once) != once:
            fails.append(f"idempotence: normalize not stable for {s!r}")
        if stream.card_id(s) != stream.card_id(stream.normalize(s)):
            fails.append(f"enc:v1: id not invariant under normalize for {s!r}")

    # 2. render -> parse round-trip preserves every id, byte-exact, no floating.
    records = {}
    for i, s in enumerate(samples):
        cid = stream.card_id(s)
        records[cid] = stream.Card(id=cid, author="fish", captured_at=f"2026-06-15 (mon)-12:00:0{i}",
                                   flair="", body=s, thread="[[t]]")
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

    # 2b. view-escape semantics: math spans keep literal <,> (MathJax needs them);
    #     '<' in prose still escapes (a bare <tag> would collapse the callout), but
    #     '>' is never escaped — a leading '>' is an intentional nested callout.
    vcard = stream.Card(id="deadbeef", author="fish", captured_at="2026-06-15 (mon)-12:00:00",
                        flair="", body="$a < b$ and $$x > y$$ but a bare <tag> here", thread="[[t]]")
    vrender = stream.render_card(vcard)
    for must in ("$a < b$", "$$x > y$$", "&lt;tag>"):
        if must not in vrender:
            fails.append(f"view-escape: expected {must!r} in rendered callout")
    if "&lt;" in vrender.replace("&lt;tag>", ""):
        fails.append("view-escape: a '<' escaped inside a math span")
    if "&gt;" in vrender:
        fails.append("view-escape: '>' was escaped (would break nested-callout excerpts)")

    # 2c. a leading '>' in a body must survive as a NESTED callout — the excerpt-quote
    #     shape the annotation-harvest feature emits (render prefixes "> ", so a body
    #     line "> [!quote]" must become "> > [!quote]", not "> &gt; [!quote]").
    ncard = stream.Card(id="deadbee2", author="fish", captured_at="2026-06-15 (mon)-12:00:00",
                        flair="", body="> [!quote] excerpt\n> quoted $a < b$", thread="[[t]]")
    if "> > [!quote] excerpt" not in stream.render_card(ncard):
        fails.append("view-escape: leading '>' must nest (got a flattened/escaped quote)")

    # 3. global pool + inclusion: the SAME body in two threads is ONE card in the pool,
    #    included by BOTH manifests (dedup, not duplication); each thread loads it; a thread
    #    with no manifest is empty.
    with tempfile.TemporaryDirectory() as td:
        stream.CARDS_DIR = pathlib.Path(td) / "cards"        # the global pool
        stream.THREADS_DIR = pathlib.Path(td) / "threads"    # the manifests
        stream.RECORDS_ROOT = pathlib.Path(td) / "records"   # empty -> no legacy migration
        body = "ok"
        cid = stream.card_id(body)
        stream.write_record(stream.Card(id=cid, author="fish", captured_at="2026-06-15 (mon)-12:00:00", body=body, thread="[[a]]"), "a")
        stream.write_record(stream.Card(id=cid, author="fish", captured_at="2026-06-15 (mon)-12:00:01", body=body, thread="[[b]]"), "b")
        if len(list((pathlib.Path(td) / "cards").glob("*.md"))) != 1:
            fails.append("pool: identical body should be ONE pooled card, not duplicated")
        if cid not in stream.load_records("a") or cid not in stream.load_records("b"):
            fails.append("pool: both threads should include the shared card")
        if stream.load_records("never") != {}:
            fails.append("pool: a thread with no manifest is not empty")

    # 4. annotation harvest: a `…`-sigil note typed inside an edited body is lifted
    #    into a fish reply that quotes the excerpt as a nested callout; a non-sigil
    #    edit is NOT harvested (it is left to the restore); the host is never the carrier.
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
                  captured_at="2026-06-15 (mon)-12:00:00", flair="", body=hbody, thread="[[t]]"))
        if "> > [!quote] abc12345 · Step 2 — plasma" not in hrender:
            fails.append("harvest: excerpt is not a nested quote callout labelled by section")
        if "> > $$u_B = 3$$" not in hrender:
            fails.append("harvest: excerpt body missing from the harvested card")
        if "> my note" not in hrender:
            fails.append("harvest: the note is missing from the harvested card")
    if stream._extract_annotations("a\nb", "a\nedited b\nc"):
        fails.append("harvest: a non-sigil edit must NOT be harvested")

    # 4b. code-highlight: a span the operator wrapped in `…` inline (not a whole line)
    #     is the excerpt — harvested into a fish [!quote] of exactly that span. A span
    #     that was already code in the record is not a highlight; a whole-line span is a
    #     sigil note, not a highlight.
    hrec = "the quick brown fox jumps"
    hvw = "the quick `brown fox` jumps"
    hls = stream._extract_highlights(hrec, hvw)
    if [h["excerpt"] for h in hls] != ["brown fox"]:
        fails.append(f"highlight: expected ['brown fox'], got {[h['excerpt'] for h in hls]}")
    else:
        qbody = stream._highlight_card_body("abc12345", hls[0])
        qrender = stream.render_card(stream.Card(id="deadbee4", author="fish",
                  captured_at="2026-06-15 (mon)-12:00:00", flair="", body=qbody, thread="[[t]]"))
        if "> > [!quote] abc12345" not in qrender or "> > brown fox" not in qrender:
            fails.append("highlight: excerpt is not a nested quote of exactly the span")
    if stream._extract_highlights("x `kept` y", "x `kept` y"):
        fails.append("highlight: a span already code in the record must NOT be a highlight")
    if stream._extract_highlights("note here", "`note here`"):
        fails.append("highlight: a whole-line span is a sigil note, not a highlight")

    # 5. no-scrub: a floating draft left in the view is FOLDED into a card by the
    #    reconcile step before any append re-renders — staged text is never erased.
    with tempfile.TemporaryDirectory() as td:
        stream.CARDS_DIR = pathlib.Path(td) / "cards"
        stream.THREADS_DIR = pathlib.Path(td) / "threads"
        stream.RECORDS_ROOT = pathlib.Path(td) / "records"
        view = pathlib.Path(td) / "draft.md"
        rdir = stream.records_dir("draft")
        hid = stream.card_id("a host card")
        stream.write_record(stream.Card(id=hid, author="claude-tui",
                            captured_at="2026-06-15 (mon)-12:00:00", body="a host card",
                            thread="[[draft]]"), rdir)
        base = stream.render_view(stream.load_records(rdir), "type: stream")
        view.write_text(base + "\nmy staged draft reply\n", encoding="utf-8")   # operator drafts
        before = len(stream.load_records(rdir))
        stream._reconcile_view(view)                                            # the fix
        after = stream.load_records(rdir)
        if len(after) != before + 1:
            fails.append(f"no-scrub: draft not folded into a card (had {before}, now {len(after)})")
        elif not any("my staged draft reply" in c.body for c in after.values()):
            fails.append("no-scrub: the folded card does not contain the draft text")

    # 6. pull: a code-highlight is extracted into a ``` codeblock below the --- (scrub
    #    above, append below); re-running is a no-op (the highlight is scrubbed).
    with tempfile.TemporaryDirectory() as td:
        stream.CARDS_DIR = pathlib.Path(td) / "cards"
        stream.THREADS_DIR = pathlib.Path(td) / "threads"
        stream.RECORDS_ROOT = pathlib.Path(td) / "records"
        view = pathlib.Path(td) / "ex.md"
        rdir = stream.records_dir("ex")
        cid = stream.card_id("renka likes lychee with zero regrets here")
        stream.write_record(stream.Card(id=cid, author="renka", captured_at="2026-06-15 (mon)-12:00:00",
                            body="renka likes lychee with zero regrets here", thread="[[ex]]"), rdir)
        base = stream.render_view(stream.load_records(rdir), "type: stream")
        view.write_text(base.replace("zero regrets", "`zero regrets`"), encoding="utf-8")  # operator highlights
        # a highlight must SURVIVE a reconcile — not harvested into a card, not scrubbed
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
        # the scaffold must survive a reconcile + re-render, while prose beside it folds
        view.write_text(view.read_text(encoding="utf-8") + "\na prose draft beside it\n", encoding="utf-8")
        stream._reconcile_view(view)
        stream._render_keep_scaffolds(view, stream.load_records(rdir))
        final = view.read_text(encoding="utf-8")
        if f"```\n{cid}\nzero regrets\n```" not in final:
            fails.append("pull: scaffold did not survive a reconcile + re-render")
        if not any("a prose draft beside it" in c.body for c in stream.load_records(rdir).values()):
            fails.append("pull: prose beside a scaffold was not folded into a card")

    # 7. gel: an annotated scaffold below the --- gels into a fish quote-reply card
    #    a composed post (lead-in + embedded codeblock + commentary across a BLANK line)
    #    gels into ONE card replying to the ref, with the codeblock converted to a nested
    #    [!quote] callout in place; the staging area is consumed. A bare scaffold gels too.
    with tempfile.TemporaryDirectory() as td:
        stream.CARDS_DIR = pathlib.Path(td) / "cards"
        stream.THREADS_DIR = pathlib.Path(td) / "threads"
        stream.RECORDS_ROOT = pathlib.Path(td) / "records"
        view = pathlib.Path(td) / "g.md"
        rdir = stream.records_dir("g")
        ref = stream.card_id("a card worth quoting")
        stream.write_record(stream.Card(id=ref, author="shizu", captured_at="2026-06-15 (mon)-12:00:00",
                            body="a card worth quoting", thread="[[g]]"), rdir)
        base = stream.render_view(stream.load_records(rdir), "type: stream")
        # the operator's failing case: lead-in, embedded quote, then commentary across a blank line
        view.write_text(base + f"\ni pulled this:\n```\n{ref}\nworth quoting\n```\n\nand here's my take\n",
                        encoding="utf-8")
        gelled = stream.gel_scaffolds(view, stream.load_records(rdir))
        g = stream.load_records(rdir).get(gelled[0][0]) if gelled else None
        if len(gelled) != 1 or not g or g.reply_to != ref:
            fails.append(f"gel: composed post did not gel into one quote-reply to the ref: {gelled}")
        elif not all(s in g.body for s in ("i pulled this:", f"> [!shizu] shizu | [[{ref}]]", "worth quoting", "and here's my take")):
            fails.append(f"gel: composed card missing lead-in, nested quote, or commentary: {g.body!r}")
        elif "```" in g.body:
            fails.append("gel: the codeblock did NOT convert into a nested callout (still a fence)")
        if "```" in view.read_text(encoding="utf-8").rsplit("\n---\n", 1)[-1]:
            fails.append("gel: the staging area was not consumed")
        view.write_text(base + f"\n```\n{ref}\njust the quote\n```\n", encoding="utf-8")   # bare = ref only
        bare = stream.gel_scaffolds(view, stream.load_records(rdir))
        bg = stream.load_records(rdir).get(bare[0][0]) if bare else None
        if not bare or not bg or f"> [!shizu] shizu | [[{ref}]]" not in bg.body:
            fails.append("gel: a bare scaffold (no prose) did not gel into a quote-only card")
        # the operator's actual bug: a scaffold in a section BEFORE the last --- (a bar break
        # placed after it) must still gel — gel processes every ---separated post, not just the last.
        view.write_text(base + f"\n```\n{ref}\nquoted bit\n```\nmy reply\n---\njust a trailing note\n", encoding="utf-8")
        multi = stream.gel_scaffolds(view, stream.load_records(rdir))
        gm = stream.load_records(rdir).get(multi[0][0]) if multi else None
        if len(multi) != 1 or not gm or f"> [!shizu] shizu | [[{ref}]]" not in gm.body or "my reply" not in gm.body:
            fails.append(f"gel: a scaffold before the last --- bar break did not gel: {multi}")
        if "just a trailing note" not in view.read_text(encoding="utf-8"):
            fails.append("gel: a trailing no-scaffold post was dropped instead of kept for fold")

    # 8. fan-out: a block typed beneath a card's ^caret replies to THAT card (branching),
    #    not the head; a block below the last --- still posts as a new root.
    with tempfile.TemporaryDirectory() as td:
        stream.CARDS_DIR = pathlib.Path(td) / "cards"
        stream.THREADS_DIR = pathlib.Path(td) / "threads"
        stream.RECORDS_ROOT = pathlib.Path(td) / "records"
        view = pathlib.Path(td) / "f.md"
        rdir = stream.records_dir("f")
        a = stream.card_id("card A")
        b = stream.card_id("card B")
        stream.write_record(stream.Card(id=a, author="fish", captured_at="2026-06-15 (mon)-12:00:01", body="card A", thread="[[f]]"), rdir)
        stream.write_record(stream.Card(id=b, author="fish", captured_at="2026-06-15 (mon)-12:00:02", body="card B", thread="[[f]]"), rdir)
        rendered = stream.render_view(stream.load_records(rdir), "type: stream")  # A then B (B = head)
        view.write_text(rendered.replace(f"^{a}\n", f"^{a}\n\nreply beneath A\n", 1), encoding="utf-8")
        stream.fold_floating(view, stream.load_records(rdir))
        branch = next((c for c in stream.load_records(rdir).values() if c.body.strip() == "reply beneath A"), None)
        if not branch or branch.reply_to != a:
            fails.append(f"fanout: block beneath A's caret did not reply to A (got {getattr(branch,'reply_to','-')}, head={b})")

    # 9. fork = a subtree manifest (root + descendants, resolved live); clone = a copied
    #    manifest. Both are lenses over ONE pool, and the clone diverges independently.
    with tempfile.TemporaryDirectory() as td:
        stream.CARDS_DIR = pathlib.Path(td) / "cards"
        stream.THREADS_DIR = pathlib.Path(td) / "threads"
        stream.RECORDS_ROOT = pathlib.Path(td) / "records"
        def _mk(body, parent, ts):
            cid = stream.card_id(body)
            stream.write_record(stream.Card(id=cid, author="fish", captured_at=ts,
                                            reply_to=parent, body=body, thread="[[src]]"), "src")
            return cid
        a = _mk("root A",    None, "2026-06-15 (mon)-12:00:01")
        b = _mk("reply B",   a,    "2026-06-15 (mon)-12:00:02")
        c = _mk("reply C",   b,    "2026-06-15 (mon)-12:00:03")
        d = _mk("sibling D", a,    "2026-06-15 (mon)-12:00:04")     # branches off A, not under B
        stream._write_manifest("forked", {"kind": "subtree", "root": b, "ids": []})
        if set(stream.load_records("forked")) != {b, c}:           # B + descendant C; NOT A or D
            fails.append(f"fork: subtree(B) should be {{B,C}}, got {sorted(stream.load_records('forked'))}")
        stream._write_manifest("src2", stream._read_manifest("src"))   # clone = copy the manifest
        if set(stream.load_records("src2")) != {a, b, c, d}:
            fails.append("fork: clone should include the same cards as its source")
        e = _mk("only in src", d, "2026-06-15 (mon)-12:00:05")     # appended to src's manifest only
        if e in stream.load_records("src2") or e not in stream.load_records("src"):
            fails.append("fork: clone is not independent — a new card in src must not leak into it")

    # 10. non-destructive re-render (the substrate): a draft below the staging --- survives a plain
    #     re-render, while an in-view CARD-body edit is still discarded (rule #1).
    with tempfile.TemporaryDirectory() as td:
        stream.CARDS_DIR = pathlib.Path(td) / "cards"
        stream.THREADS_DIR = pathlib.Path(td) / "threads"
        stream.RECORDS_ROOT = pathlib.Path(td) / "records"
        view = pathlib.Path(td) / "keep.md"
        rdir = stream.records_dir("keep")
        cid = stream.card_id("canonical body")
        stream.write_record(stream.Card(id=cid, author="claude-tui",
                            captured_at="2026-06-15 (mon)-12:00:00", body="canonical body",
                            thread="[[keep]]"), rdir)
        base = stream.render_view(stream.load_records(rdir), "type: stream")
        # operator tampers a card body (must be discarded) AND leaves a draft below --- (must survive)
        view.write_text(base.replace("canonical body", "TAMPERED body") + "\nmy uncommitted draft\n",
                        encoding="utf-8")
        out = stream._render_preserving(view, stream.load_records(rdir), "type: stream")
        if "TAMPERED" in out:
            fails.append("substrate: an in-view card-body edit must be discarded on re-render (rule #1)")
        if "canonical body" not in out:
            fails.append("substrate: the card body was not restored from the pool")
        if "my uncommitted draft" not in out:
            fails.append("substrate: the staging draft did not survive the re-render")
        # the flask/Restore HARD path (render_view) dissolves BOTH the edit and the draft -> canonical
        hard = stream.render_view(stream.load_records(rdir), "type: stream")
        if "my uncommitted draft" in hard or "TAMPERED" in hard:
            fails.append("substrate: a HARD reset (flask/Restore) must dissolve the staging draft AND the edit")

    # 11. locate: a bare excerpt -> its source card id(s) by content. A full body resolves `exact`
    #     (O(1) via the hash); a partial span resolves `contains` by scan; a non-present span is none;
    #     a span shared by two cards is ambiguous (both returned).
    with tempfile.TemporaryDirectory() as td:
        stream.CARDS_DIR = pathlib.Path(td) / "cards"
        stream.THREADS_DIR = pathlib.Path(td) / "threads"
        stream.RECORDS_ROOT = pathlib.Path(td) / "records"
        b1 = "the hipims target erodes in a racetrack pattern under the magnetron"
        b2 = "a racetrack pattern also shows up in tokamak limiter wear"     # shares 'racetrack pattern'
        c1, c2 = stream.card_id(b1), stream.card_id(b2)
        for cid, b in ((c1, b1), (c2, b2)):
            stream.write_record(stream.Card(id=cid, author="claude-tui",
                                captured_at="2026-06-15 (mon)-12:00:00", body=b, thread="[[loc]]"), "loc")
        if stream._locate(b1) != ("exact", [c1]):
            fails.append("locate: a full body must resolve `exact` to its id")
        if stream._locate("erodes in a racetrack") != ("contains", [c1]):
            fails.append("locate: a unique partial excerpt must resolve to its source card")
        if stream._locate("zirconium plasma sheath")[1] != []:
            fails.append("locate: a non-present excerpt must not match")
        if set(stream._locate("racetrack pattern")[1]) != {c1, c2}:
            fails.append("locate: a shared excerpt must return all matches (ambiguous), not guess one")

    print(f"samples under test: {len(samples)}")
    print(f"[1] enc:v1 fixed point   {'ok' if not any(f.startswith(('idempotence','enc:v1')) for f in fails) else 'FAIL'}")
    print(f"[2] render round-trip    {'ok' if not any(f.startswith('roundtrip') for f in fails) else 'FAIL'}")
    print(f"[3] global pool + incl.  {'ok' if not any(f.startswith('pool') for f in fails) else 'FAIL'}")
    print(f"[4] annotation harvest   {'ok' if not any(f.startswith(('harvest', 'highlight')) for f in fails) else 'FAIL'}")
    print(f"[5] no-scrub preserve    {'ok' if not any(f.startswith('no-scrub') for f in fails) else 'FAIL'}")
    print(f"[6] pull extraction      {'ok' if not any(f.startswith('pull') for f in fails) else 'FAIL'}")
    print(f"[7] gel quote-reply      {'ok' if not any(f.startswith('gel') for f in fails) else 'FAIL'}")
    print(f"[8] fan-out beneath caret{'  ok' if not any(f.startswith('fanout') for f in fails) else '  FAIL'}")
    print(f"[9] fork + clone         {'ok' if not any(f.startswith('fork') for f in fails) else 'FAIL'}")
    print(f"[10] non-destructive render{'  ok' if not any(f.startswith('substrate') for f in fails) else '  FAIL'}")
    print(f"[11] locate excerpt->hash{'  ok' if not any(f.startswith('locate') for f in fails) else '  FAIL'}")
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
