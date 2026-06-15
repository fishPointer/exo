#!/usr/bin/env python3
"""
doctor.py — verify the whole vault is wired and working. Read-only except for
regenerating the derived DASHBOARD.md (a fresh baseline).

Deterministic: no LLM, no network. Runs the golden tests, checks integrity,
re-renders every thread to confirm no drift, and audits that the apparatus
(hook, plugin, snippet, configs) is actually wired the way it claims. Optional
bits (API key, claude CLI, the daemon) are reported, never failed.

Run:  python3 _system/doctor.py            # → exit 0 if all required checks pass
The /initialize skill is a thin wrapper over this.
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

_req_fail = 0   # required checks that failed
_lines: list[str] = []


def _emit(mark: str, name: str, detail: str = "") -> None:
    _lines.append(f"  {mark} {name}" + (f" — {detail}" if detail else ""))


def req(name: str, ok: bool, detail: str = "") -> bool:
    """A required check. A failure makes doctor exit non-zero."""
    global _req_fail
    _emit("✓" if ok else "✗", name, detail if not ok else "")
    if not ok:
        _req_fail += 1
    return ok


def info(name: str, detail: str) -> None:
    """An optional/environmental check. Reported, never failed."""
    _emit("·", name, detail)


def section(title: str) -> None:
    _lines.append("")
    _lines.append(f"{title}")


def _run(argv: list[str]) -> tuple[int, str]:
    p = subprocess.run([PY, str(STREAM), *argv], capture_output=True, text=True, cwd=str(ROOT))
    return p.returncode, (p.stdout + p.stderr).strip()


def main() -> int:
    section("environment")
    pyok = sys.version_info >= (3, 8)
    req(f"python {sys.version_info.major}.{sys.version_info.minor} (need ≥ 3.8)", pyok)
    info("claude CLI (for Summon)", "found" if shutil.which("claude") else "absent — Summon won't work")
    env_file = pathlib.Path(os.environ.get("EXO_ENV", pathlib.Path.home() / ".config" / "exo" / ".env"))
    if env_file.exists():
        has_key = "ANTHROPIC_API_KEY" in env_file.read_text(encoding="utf-8", errors="ignore")
        info("API key", f"{env_file} present" + ("" if has_key else " but no ANTHROPIC_API_KEY"))
    else:
        info("API key", f"{env_file} absent — Summon disabled (everything else works)")

    section("apparatus — files & wiring")
    for rel in ("_system/stream.py", "_system/watch.py", "_system/capture_prompt.py",
                "_system/test_golden.py", "README.md", "_system/ARCHITECTURE.md",
                "DASHBOARD.md", ".gitignore"):
        req(rel, (ROOT / rel).exists())

    # the capture hook must be wired to capture_prompt.py and the JSON must be valid
    settings = ROOT / ".claude" / "settings.json"
    hook_ok = False
    if req(".claude/settings.json", settings.exists()):
        try:
            cfg = json.loads(settings.read_text(encoding="utf-8"))
            cmds = [h.get("command", "")
                    for blk in cfg.get("hooks", {}).get("UserPromptSubmit", [])
                    for h in blk.get("hooks", [])]
            hook_ok = any("capture_prompt.py" in c for c in cmds)
        except (ValueError, AttributeError):
            hook_ok = False
        req("capture hook → capture_prompt.py", hook_ok)
    req(".claude/CLAUDE.md", (ROOT / ".claude" / "CLAUDE.md").exists())

    # obsidian: the link-rewrite guard, the plugin, the snippet + its enablement
    app = ROOT / ".obsidian" / "app.json"
    guard = False
    if req(".obsidian/app.json", app.exists()):
        try:
            guard = json.loads(app.read_text(encoding="utf-8")).get("alwaysUpdateLinks") is False
        except ValueError:
            guard = False
        req("alwaysUpdateLinks: false (hash-safety guard)", guard)
    man = ROOT / ".obsidian" / "plugins" / "exo-ribbon" / "manifest.json"
    if req(".obsidian/plugins/exo-ribbon/manifest.json", man.exists()):
        try:
            req("plugin id = exo-ribbon", json.loads(man.read_text(encoding="utf-8")).get("id") == "exo-ribbon")
        except ValueError:
            req("plugin id = exo-ribbon", False)
    req(".obsidian/plugins/exo-ribbon/main.js", (ROOT / ".obsidian/plugins/exo-ribbon/main.js").exists())
    snippet = ROOT / ".obsidian" / "snippets" / "stream-cards.css"
    appearance = ROOT / ".obsidian" / "appearance.json"
    enabled = False
    if appearance.exists():
        try:
            enabled = "stream-cards" in json.loads(appearance.read_text(encoding="utf-8")).get("enabledCssSnippets", [])
        except ValueError:
            enabled = False
    req("card styling snippet present + enabled", snippet.exists() and enabled,
        "stream-cards.css missing or not in appearance.json:enabledCssSnippets")

    section("the spine — does it actually work")
    gp = subprocess.run([PY, str(SYS / "test_golden.py")], capture_output=True, text=True, cwd=str(ROOT))
    req("golden tests (enc:v1 / round-trip / isolation)", gp.returncode == 0,
        gp.stdout.strip().splitlines()[-1] if gp.returncode else "")
    rc, out = _run(["validate"])
    req("validate (hash + referential integrity, all threads)", rc == 0, out.splitlines()[-1] if rc else "")
    # CLI entrypoint dispatches and agrees with the library
    cp = subprocess.run([PY, str(STREAM), "id"], input="doctor", capture_output=True, text=True, cwd=str(ROOT))
    req("CLI entrypoint (`id` == library card_id)",
        cp.returncode == 0 and cp.stdout.strip() == stream.card_id("doctor"))

    # every thread must re-render byte-identical from its records (no silent drift)
    views = stream.find_stream_views()
    req("at least one thread exists", len(views) >= 1, "no `type: stream` note under notes/")
    drift = []
    for v in views:
        rendered = stream.render_view(stream.load_records(stream.records_dir(v.stem)),
                                      stream._clean_fm_block(v))
        current = v.read_text(encoding="utf-8") if v.exists() else ""
        # a thread flagged dirty (unsaved edits) is allowed to differ — that's not drift
        fm, _, _ = stream._split_frontmatter(current)
        if current != rendered and fm.get("stream") != "dirty":
            drift.append(str(v.relative_to(ROOT)))
    req("threads render clean from records", not drift,
        f"{len(drift)} drifted (run Restore): {', '.join(drift)}" if drift else "")

    section("daemon")
    daemon = stream._read_json(stream.STREAM_DIR / "daemon.json", {})
    pid = daemon.get("pid")
    alive = bool(pid) and pathlib.Path(f"/proc/{pid}").exists()
    info("watch.py", f"🟢 live (pid {pid})" if alive else "🔴 down — `python3 _system/watch.py` to enable buttons")

    # fresh baseline: regenerate the derived dashboard
    try:
        stream.DASHBOARD.write_text(stream.render_dashboard(), encoding="utf-8")
        regen = "DASHBOARD.md refreshed"
    except Exception as e:  # never fail doctor on a derived-view write
        regen = f"DASHBOARD.md refresh skipped ({e})"

    print("exo doctor")
    print("\n".join(_lines))
    print()
    if _req_fail:
        print(f"⚠ {_req_fail} required check(s) FAILED — see ✗ above. ({regen})")
        return 1
    print(f"✓ all required checks pass — vault is healthy. ({regen})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
