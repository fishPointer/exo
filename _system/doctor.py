#!/usr/bin/env python3
"""
doctor.py — verify the vault, and with --fix, repair every safely-fixable fault.

  python3 _system/doctor.py          # read-only: diagnose and report
  python3 _system/doctor.py --fix    # repair what's safely fixable, then re-verify

Deterministic: no LLM, no network. The /initialize skill runs this with --fix.

Fix policy (deliberately conservative): only repairs that are unambiguous and
LOSSLESS are applied — config wiring, a flipped safety flag, a stale/unflagged
thread (reconciled with `run`, which folds any typed-in text into cards rather
than discarding it). Genuine corruption (a record whose body no longer hashes to
its name), a broken clone (missing source), or a failing golden test are NOT
auto-"fixed" — that would hide data loss. Those get a clear ✗ and the exact
remedy. Threads already flagged `dirty` (known unsaved work) are left untouched.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SYS = ROOT / "_system"
sys.path.insert(0, str(SYS))
import stream  # noqa: E402

PY = sys.executable
STREAM = SYS / "stream.py"
FIX = "--fix" in sys.argv[1:]

_req_fail = 0
_fixes: list[str] = []
_lines: list[str] = []


# ── reporting ────────────────────────────────────────────────────────────────

def _emit(mark: str, name: str, detail: str = "") -> None:
    _lines.append(f"  {mark} {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    _lines.append("")
    _lines.append(title)


def req(name: str, ok: bool, remedy: str = "") -> bool:
    """A required check with no safe auto-fix. Failure → non-zero exit."""
    global _req_fail
    _emit("✓" if ok else "✗", name, "" if ok else remedy)
    if not ok:
        _req_fail += 1
    return ok


def info(name: str, detail: str) -> None:
    """Optional / environmental. Reported, never failed."""
    _emit("·", name, detail)


def fixable(name: str, ok_fn, fix_fn, remedy: str = "") -> bool:
    """A check that CAN be repaired. In --fix mode, attempt the repair and
    re-check; otherwise report ✗ with a hint."""
    global _req_fail
    if ok_fn():
        _emit("✓", name)
        return True
    if FIX:
        try:
            fix_fn()
        except Exception as e:               # fix refused (e.g. malformed JSON) — report it
            _emit("✗", name, f"could not auto-fix: {e}")
            _req_fail += 1
            return False
        if ok_fn():
            _emit("✓", name, "FIXED")
            _fixes.append(name)
            return True
    _emit("✗", name, remedy + ("" if FIX else "  (run with --fix to repair)"))
    _req_fail += 1
    return False


# ── json helpers (load raises on malformed → fix refuses to clobber) ──────────

def _load(p: pathlib.Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _save(p: pathlib.Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


HOOK_CMD = 'python3 "$CLAUDE_PROJECT_DIR/_system/capture_prompt.py"'

# ── individual checks + their repairs ────────────────────────────────────────

def _guard_ok() -> bool:
    p = ROOT / ".obsidian" / "app.json"
    if not p.exists():
        return False
    try:
        return _load(p).get("alwaysUpdateLinks") is False
    except ValueError:
        return False


def _guard_fix() -> None:
    p = ROOT / ".obsidian" / "app.json"
    d = _load(p) if p.exists() else {"promptDelete": False, "showUnsupportedFiles": True}
    d["alwaysUpdateLinks"] = False
    _save(p, d)


def _hook_ok() -> bool:
    p = ROOT / ".claude" / "settings.json"
    if not p.exists():
        return False
    try:
        cfg = _load(p)
    except ValueError:
        return False
    cmds = [h.get("command", "")
            for blk in cfg.get("hooks", {}).get("UserPromptSubmit", [])
            for h in blk.get("hooks", [])]
    return any("capture_prompt.py" in c for c in cmds)


def _hook_fix() -> None:
    p = ROOT / ".claude" / "settings.json"
    cfg = _load(p) if p.exists() else {"$schema": "https://json.schemastore.org/claude-code-settings.json"}
    cfg.setdefault("hooks", {}).setdefault("UserPromptSubmit", []).append(
        {"hooks": [{"type": "command", "command": HOOK_CMD}]})
    _save(p, cfg)


def _manifest_ok() -> bool:
    p = ROOT / ".obsidian" / "plugins" / "exo-ribbon" / "manifest.json"
    if not p.exists():
        return False
    try:
        return _load(p).get("id") == "exo-ribbon"
    except ValueError:
        return False


def _manifest_fix() -> None:
    p = ROOT / ".obsidian" / "plugins" / "exo-ribbon" / "manifest.json"
    if not p.exists():
        raise RuntimeError("manifest.json missing — `git checkout -- .obsidian/plugins/exo-ribbon/`")
    d = _load(p)
    d["id"] = "exo-ribbon"
    _save(p, d)


# canonical claude-tui / claude-api callout styling. Appended to persona-cards.css
# if absent so the terminal-voice (violet) and summon (red) cards don't fall back
# to the default callout background. Self-heal source for a snippet imported
# without these blocks — same pattern as HOOK_CMD above.
TUI_CALLOUT_CSS = """
/* ---------- claude-tui (terminal replies, violet) + claude-api (summoned, soft dark red) ---------- */
.callout[data-callout="claude-tui"],
.callout[data-callout="claude-api"] { border-radius: 5px; margin: 0.5em 0; mix-blend-mode: normal; }
.callout[data-callout="claude-tui"] .callout-icon,
.callout[data-callout="claude-api"] .callout-icon { display: none; }
.callout[data-callout="claude-tui"] .callout-title,
.callout[data-callout="claude-api"] .callout-title {
  font-family: var(--font-monospace); font-size: 0.82em; letter-spacing: 0.02em;
  padding: 4px 8px; border-radius: 4px 4px 0 0; font-weight: 700; line-height: 1.6; white-space: normal;
}
.callout[data-callout="claude-tui"] .callout-title code,
.callout[data-callout="claude-api"] .callout-title code {
  background-color: rgba(0,0,0,0.28); color: #ff9c3c; padding: 0 4px; border-radius: 3px;
}
.callout[data-callout="claude-tui"] .callout-title a,
.callout[data-callout="claude-api"] .callout-title a {
  text-decoration: underline; text-underline-offset: 2px; font-weight: 700;
}
.callout[data-callout="claude-tui"] .callout-title a:not([href*="#"]):not([data-href*="#"]),
.callout[data-callout="claude-api"] .callout-title a:not([href*="#"]):not([data-href*="#"]) {
  background-color: rgba(0,0,0,0.28); padding: 0 4px; border-radius: 3px; text-decoration: none;
}

/* claude-tui — violet (the interactive terminal voice; same family as claude) */
.callout[data-callout="claude-tui"] {
  --callout-color: 155, 140, 255;
  background-color: rgba(155, 140, 255, 0.05);
  border: 1px solid rgba(155, 140, 255, 0.20);
  border-left: 3px solid #9b8cff;
}
.callout[data-callout="claude-tui"] .callout-title {
  background: linear-gradient(95deg, rgba(155,140,255,0.16) 0%, rgba(155,140,255,0.14) 78%, rgba(110,200,255,0.22) 100%);
  color: #b3a6ff;
}
.callout[data-callout="claude-tui"] .callout-title em { color: #a89cf0; font-style: italic; font-weight: 400; font-size: 0.95em; opacity: 0.92; }
.callout[data-callout="claude-tui"] .callout-content em { color: #c0b5ff; font-style: italic; }
.callout[data-callout="claude-tui"] .callout-title a:not([href*="#"]):not([data-href*="#"]) { color: #b3a6ff !important; }

/* claude-api — soft dark red (the summoned / API voice) */
.callout[data-callout="claude-api"] {
  --callout-color: 200, 90, 90;
  background-color: rgba(170, 50, 50, 0.10);
  border: 1px solid rgba(170, 50, 50, 0.30);
  border-left: 3px solid #b34747;
}
.callout[data-callout="claude-api"] .callout-title {
  background: linear-gradient(95deg, rgba(150,40,40,0.32) 0%, rgba(120,35,35,0.24) 78%, rgba(95,30,30,0.22) 100%);
  color: #e79a9a;
}
.callout[data-callout="claude-api"] .callout-title em { color: #d98a8a; font-style: italic; font-weight: 400; font-size: 0.95em; opacity: 0.92; }
.callout[data-callout="claude-api"] .callout-content em { color: #e0a0a0; font-style: italic; }
.callout[data-callout="claude-api"] .callout-title a:not([href*="#"]):not([data-href*="#"]) { color: #e79a9a !important; }
"""


def _tui_css_ok() -> bool:
    """persona-cards.css is enabled AND defines the claude-tui callout (so the
    terminal-voice + summon cards render styled, not on the default background)."""
    snip = ROOT / ".obsidian" / "snippets" / "persona-cards.css"
    app = ROOT / ".obsidian" / "appearance.json"
    if not snip.exists() or not app.exists():
        return False
    try:
        enabled = "persona-cards" in _load(app).get("enabledCssSnippets", [])
    except ValueError:
        return False
    return enabled and 'data-callout="claude-tui"' in snip.read_text(encoding="utf-8", errors="ignore")


def _tui_css_fix() -> None:
    snip = ROOT / ".obsidian" / "snippets" / "persona-cards.css"
    if not snip.exists():
        raise RuntimeError("persona-cards.css missing — `git checkout -- .obsidian/snippets/persona-cards.css`")
    txt = snip.read_text(encoding="utf-8", errors="ignore")
    if 'data-callout="claude-tui"' not in txt:
        snip.write_text(txt.rstrip("\n") + "\n" + TUI_CALLOUT_CSS, encoding="utf-8")
    app = ROOT / ".obsidian" / "appearance.json"
    d = _load(app) if app.exists() else {"accentColor": "", "cssTheme": ""}
    lst = d.setdefault("enabledCssSnippets", [])
    if "persona-cards" not in lst:
        lst.append("persona-cards")
    _save(app, d)


def _run(argv: list[str]):
    return subprocess.run([PY, str(STREAM), *argv], capture_output=True, text=True, cwd=str(ROOT))


def _drift() -> list[pathlib.Path]:
    """Threads whose CARD region differs from a fresh render AND are not flagged dirty
    (a `dirty` thread is known unsaved work, not drift). The staging zone below the cards' `---` is
    the operator's uncommitted draft — the non-destructive re-render carries it verbatim, so a staging
    difference is never drift; only a divergent card region is."""
    out = []
    for v in stream.find_stream_views():
        recs = stream.load_records(stream.records_dir(v.stem))
        rendered = stream.render_view(recs, stream._clean_fm_block(v))
        cur = v.read_text(encoding="utf-8") if v.exists() else ""
        fm, _, _ = stream._split_frontmatter(cur)
        if stream._staging(cur)[0] != stream._staging(rendered)[0] and fm.get("stream") != "dirty":
            out.append(v)
    return out


def _orphan_refs() -> list[tuple[str, str]]:
    """Inclusion integrity: a `list` manifest entry whose card is missing from the pool. Such an
    id is silently dropped from the rendered view (load_records skips it), so it never surfaces as
    drift — the one new-layout fault the render-check can't see. (subtree manifests resolve live,
    so they can't dangle.)"""
    pool = stream._all_pool_cards()
    out = []
    if stream.THREADS_DIR.exists():
        for m in sorted(stream.THREADS_DIR.glob("*.md")):
            man = stream._read_manifest(m.stem)
            out += [(m.stem, cid) for cid in man["ids"] if cid not in pool]
    return out


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    section("environment")
    req(f"python {sys.version_info.major}.{sys.version_info.minor} (need ≥ 3.8)",
        sys.version_info >= (3, 8), "upgrade Python to 3.8+")
    info("claude CLI (for Summon)", "found" if shutil.which("claude") else "absent — Summon won't work")
    env_file = pathlib.Path(os.environ.get("EXO_ENV", pathlib.Path.home() / ".config" / "exo" / ".env"))
    if env_file.exists():
        has = "ANTHROPIC_API_KEY" in env_file.read_text(encoding="utf-8", errors="ignore")
        info("API key", f"{env_file} present" + ("" if has else " but no ANTHROPIC_API_KEY"))
    else:
        info("API key", f"{env_file} absent — Summon disabled (everything else works)")

    section("apparatus — source (can't auto-fix; restore from git)")
    for rel in ("_system/stream.py", "_system/watch.py", "_system/capture_prompt.py",
                "_system/test_golden.py",
                "_system/ARCHITECTURE.md", "README.md",
                ".claude/CLAUDE.md", ".obsidian/plugins/exo-ribbon/main.js",
                ".obsidian/snippets/persona-cards.css",
                ".gitignore"):
        req(rel, (ROOT / rel).exists(), f"missing — `git checkout -- {rel}`")

    section("apparatus — wiring (auto-fixable)")
    fixable("alwaysUpdateLinks: false (hash-safety guard)", _guard_ok, _guard_fix)
    fixable("prompt hook → capture_prompt.py (fish capture)", _hook_ok, _hook_fix)
    fixable("card styling snippet (persona-cards) enabled + claude-tui/api callouts", _tui_css_ok, _tui_css_fix)
    fixable("plugin id = exo-ribbon", _manifest_ok, _manifest_fix)

    section("the spine — does it actually work")
    gp = subprocess.run([PY, str(SYS / "test_golden.py")], capture_output=True, text=True, cwd=str(ROOT))
    req("golden tests (enc:v1 / round-trip / pool / fork)", gp.returncode == 0,
        "code regression — do NOT edit normalize(); see test output")
    vp = _run(["validate"])
    req("validate (pool hash + referential integrity)", vp.returncode == 0,
        "a card body was changed and no longer hashes to its id — inspect _system/data/cards/")
    cp = subprocess.run([PY, str(STREAM), "id"], input="doctor", capture_output=True, text=True, cwd=str(ROOT))
    req("CLI entrypoint (`id` == library card_id)",
        cp.returncode == 0 and cp.stdout.strip() == stream.card_id("doctor"))

    views = stream.find_stream_views()
    req("at least one thread exists", len(views) >= 1, "no `type: stream` note under notes/")
    drift = _drift()
    if FIX and drift:
        for v in drift:
            _run(["run", "--view", str(v.relative_to(ROOT))])   # lossless: folds typed text → cards
        reconciled = [v for v in drift if v not in _drift()]
        for v in reconciled:
            _fixes.append(f"reconciled {v.relative_to(ROOT)}")
        drift = _drift()
    req("threads render clean from records", not drift,
        ("drifted: " + ", ".join(str(v.relative_to(ROOT)) for v in drift) +
         ("  (run with --fix to reconcile)" if not FIX else "  (`stream.py run --view <t>`)")))
    orphans = _orphan_refs()
    req("manifests resolve to pooled cards", not orphans,
        "manifest cites a card missing from the pool (silently dropped from the view): " +
        ", ".join(f"{t}:{c}" for t, c in orphans[:6]))

    section("daemon")
    daemon = stream._read_json(stream.STREAM_DIR / "daemon.json", {})
    pid = daemon.get("pid")
    alive = bool(pid) and pathlib.Path(f"/proc/{pid}").exists()
    info("watch.py", f"🟢 live (pid {pid})" if alive
         else "🔴 down — `_system/daemon.sh install` to run it as an independent service (not started for you)")

    # fresh baseline: refresh dirty index + the derived dashboard
    if FIX:
        _run(["scan"])
    try:
        stream.DASHBOARD.write_text(stream.render_dashboard(), encoding="utf-8")
        regen = "DASHBOARD.md refreshed"
    except Exception as e:
        regen = f"DASHBOARD refresh skipped ({e})"

    print("exo doctor" + (" --fix" if FIX else ""))
    print("\n".join(_lines))
    if _fixes:
        print("\nrepaired:")
        for f in _fixes:
            print(f"  + {f}")
    print()
    if _req_fail:
        hint = "" if FIX else "  (try --fix)"
        print(f"⚠ {_req_fail} check(s) still failing — see ✗ above.{hint} ({regen})")
        return 1
    tail = f" {len(_fixes)} repaired." if _fixes else ""
    print(f"✓ all required checks pass — vault is healthy.{tail} ({regen})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
