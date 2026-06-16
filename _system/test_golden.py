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

    # 3. thread isolation: the SAME body written to two threads is two files (one
    #    per thread dir, same id), and each thread loads only its own.
    with tempfile.TemporaryDirectory() as td:
        stream.RECORDS_ROOT = pathlib.Path(td)        # redirect the store
        a, b = stream.records_dir("a"), stream.records_dir("b")
        body = "ok"
        cid = stream.card_id(body)
        stream.write_record(stream.Card(id=cid, author="fish", body=body, thread="[[a]]"), a)
        stream.write_record(stream.Card(id=cid, author="fish", body=body, thread="[[b]]"), b)
        if not (a / f"{cid}.md").exists() or not (b / f"{cid}.md").exists():
            fails.append("isolation: identical body did not land in both thread dirs")
        ra, rb = stream.load_records(a), stream.load_records(b)
        if cid not in ra or cid not in rb:
            fails.append("isolation: a thread failed to load its own copy")
        if stream.load_records(stream.records_dir("never")) != {}:
            fails.append("isolation: a thread with no dir is not empty")

    print(f"samples under test: {len(samples)}")
    print(f"[1] enc:v1 fixed point   {'ok' if not any(f.startswith(('idempotence','enc:v1')) for f in fails) else 'FAIL'}")
    print(f"[2] render round-trip    {'ok' if not any(f.startswith('roundtrip') for f in fails) else 'FAIL'}")
    print(f"[3] thread isolation     {'ok' if not any(f.startswith('isolation') for f in fails) else 'FAIL'}")
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
