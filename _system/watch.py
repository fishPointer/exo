#!/usr/bin/env python3
"""
watch.py — the 'watching' half of the backend: a sentinel-file daemon.

The Obsidian plugin's ribbon buttons drop a trigger file (`.stream/trigger.json`)
via the Vault API; this daemon notices the new nonce and runs the matching
stream.py command(s), writing the outcome to `.stream/result.json`.

Why a sentinel file (not child_process / HTTP)? It's the loosely-coupled,
cross-platform, no-ports, no-shell path: the plugin only ever *writes a file*,
the daemon owns all execution. Plugin and daemon never need to be up at the
same instant. (Recommended pattern for watcher-driven plugin<->backend coord.)

This daemon only ever acts on an EXPLICIT button click (a new nonce in the
trigger file). It runs no autonomous loop and generates no content on its own —
Summon (an API call) fires solely on your click. Safe to leave running.

Run it as an INDEPENDENT service (survives closing Claude Code / crashes):
      _system/daemon.sh install       # systemd user service, or detached fallback
Manage:  _system/daemon.sh {status|restart|stop|logs}
Direct (foreground, dies with the shell — debugging only):  python3 _system/watch.py
See _system/config/daemon.md.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
STREAM = ROOT / "_system" / "stream.py"
STREAM_DIR = ROOT / ".stream"
TRIGGER = STREAM_DIR / "trigger.json"
RESULT = STREAM_DIR / "result.json"
DAEMON = STREAM_DIR / "daemon.json"
API_LOG = STREAM_DIR / "api-log.jsonl"
SUMMON_INFLIGHT = STREAM_DIR / "summon-inflight.json"
VAULT = ROOT                                         # self-contained: summon runs in THIS vault
# API key store. Override with EXO_ENV; defaults to ~/.config/exo/.env (chmod 600).
ENV_FILE = pathlib.Path(os.environ.get("EXO_ENV", pathlib.Path.home() / ".config" / "exo" / ".env"))
DEFAULT_VIEW_REL = "notes/main.md"
POLL_SECONDS = 0.5
# The agent's terminal replies are carded by the agent itself via `stream.py record`
# (capture-at-source; see ARCHITECTURE §3), NOT here. Operator prompts are carded by the
# UserPromptSubmit hook (capture_prompt.py). Neither depends on this daemon being up —
# the watcher owns only the button/Summon path below.


def _update_daemon(**kw) -> None:
    cur = {}
    if DAEMON.exists():
        try:
            cur = json.loads(DAEMON.read_text(encoding="utf-8"))
        except ValueError:
            pass
    cur.update(kw)
    DAEMON.write_text(json.dumps(cur, indent=2), encoding="utf-8")


def _refresh_dashboard() -> None:
    subprocess.run([sys.executable, str(STREAM), "dashboard", "--write"],
                   capture_output=True, cwd=str(ROOT))

# Each ribbon primitive maps to ONE stream.py subcommand. `run` and `scan` now
# own their own composition inside stream.py (run = check->scrub->render->clear
# flag on the current thread; scan = vault-wide dirty pass). The generative step
# (floating/edited text -> new reply cards) is the LLM agent and is NOT
# automated here. `view` (the active note) is threaded into the per-thread ones.
# ── the Reactive lane: headless claude mints the reply card ───────────────────
# stream.py stays deterministic; the (non-deterministic) LLM call lives HERE, at
# the edge. claude's stdout is piped STRAIGHT into `stream.py record` — so the
# reply card is captured at its source, byte-for-byte, exactly like the Active
# lane. Same single-source guarantee, zero transcription.

def _build_prompt(cs: dict) -> str:
    p = ["You are replying in a content-addressed thread (stream-cards).",
         "Write ONLY the markdown body of your reply card — no header, no ^anchor.",
         "Be terse, in the dry 'claude' register.", ""]
    if cs.get("floating"):
        p.append("Operator instructions typed between cards:")
        p += [f"- {ln}" for ln in cs["floating"]]
    for m in cs.get("mutated", []):
        p.append("Operator edited a card (act on the intent; the original is restored):")
        p.append(f"  was: {m['record_body'][:300]}")
        p.append(f"  now: {m['view_body'][:300]}")
    return "\n".join(p)


def _thread_head(view_rel: str) -> str:
    sys.path.insert(0, str(ROOT / "_system"))
    import stream
    recs = stream.load_records(stream.records_dir(pathlib.Path(view_rel).stem))
    return max(recs.values(), key=stream._sort_key).id if recs else ""


def fire_reload(view_rel: str | None) -> None:
    """obsidian:// return-path: nudge Obsidian to focus/reload the note after a
    write. Best-effort — never fails the action."""
    if not view_rel:
        return
    import shutil
    import urllib.parse
    if not shutil.which("xdg-open"):
        return
    uri = "obsidian://stream-reload?" + urllib.parse.urlencode(
        {"vault": ROOT.name, "file": view_rel})
    try:
        subprocess.run(["xdg-open", uri], capture_output=True, timeout=5)
    except Exception:
        pass


def _do_reply(view_rel: str | None) -> tuple[bool, str]:
    vrel = view_rel or DEFAULT_VIEW_REL
    slug = pathlib.Path(vrel).stem
    subprocess.run([sys.executable, str(STREAM), "diff", "--view", vrel],
                   capture_output=True, text=True, cwd=str(ROOT))      # refresh sidecar
    sc = STREAM_DIR / "changesets" / f"{slug}.json"
    if not sc.exists():
        return True, "[reply] no changeset; nothing to do"
    cs = json.loads(sc.read_text(encoding="utf-8"))
    if not cs.get("floating") and not cs.get("mutated"):
        return True, "[reply] clean; nothing to reply to"
    claude_bin = os.environ.get("STREAM_CLAUDE_BIN", "claude")
    proc = subprocess.run([claude_bin, "-p", _build_prompt(cs)],
                          capture_output=True, text=True, cwd=str(ROOT))
    if proc.returncode != 0:
        return False, f"[reply] {claude_bin} failed: {proc.stderr[:200]}"
    body = proc.stdout.strip()
    if not body:
        return False, "[reply] empty generation"
    # capture-at-source: pipe the generation straight into the single-source recorder
    rec = subprocess.run([sys.executable, str(STREAM), "record", "--author", "claude",
                          "--reply-to", _thread_head(vrel), "--flair", "◈ *auto-reply*",
                          "--view", vrel],
                         input=body, capture_output=True, text=True, cwd=str(ROOT))
    fire_reload(vrel)
    return True, f"[reply] generated + recorded (single-source):\n{rec.stdout}{rec.stderr}".rstrip()


# ── the Summon: ONE user-triggered API call (zap button) ──────────────────────
# NOT automated — only runs when a `summon` sentinel arrives from a zap click.
# Headless `claude -p` runs in the VAULT cwd so it has the vault's skills,
# memories, and CLAUDE.md; its stdout is recorded via single-source `record`.

def _api_key() -> str | None:
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("export "):                  # .env uses `export KEY=...`
            s = s[7:].strip()
        if s.startswith("ANTHROPIC_API_KEY") and "=" in s:
            return s.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _log_api(entry: dict) -> None:
    entry = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **entry}
    API_LOG.parent.mkdir(exist_ok=True)
    with open(API_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _do_summon(view_rel: str | None) -> tuple[bool, str]:
    vrel = view_rel or DEFAULT_VIEW_REL
    key = _api_key()
    if not key:
        _log_api({"action": "summon", "ok": False, "error": "no ANTHROPIC_API_KEY"})
        _refresh_dashboard()
        return False, f"[summon] no ANTHROPIC_API_KEY in {ENV_FILE}"
    sys.path.insert(0, str(ROOT / "_system"))
    import stream
    rdir = stream.records_dir(pathlib.Path(vrel).stem)
    cards = sorted(stream.load_records(rdir).values(), key=stream._sort_key)
    if not cards:
        return True, "[summon] empty thread; nothing to do"
    head = cards[-1]
    recent = "\n\n".join(f"[{c.author}] {c.body.strip()}" for c in cards[-6:])
    prompt = (
        "You are Claude, summoned via the stream-cards zap button to the live thread at "
        f"{ROOT}. Below are the most recent cards. If the latest is from the operator (fish) "
        "and awaits a reply, answer it concisely in your own voice, drawing on this vault's "
        "memories and context. Output ONLY your reply body — no header, no card anchor.\n\n"
        f"--- recent thread ---\n{recent}\n--- end ---"
    )
    env = dict(os.environ, ANTHROPIC_API_KEY=key, STREAM_SUMMON="1")
    claude_bin = os.environ.get("STREAM_CLAUDE_BIN", "claude")
    started = datetime.now()
    # marker written ONCE (start time + epoch); the status-bar chip computes the
    # live elapsed client-side from `started_epoch`, so nothing synced is rewritten
    # while the call churns.
    SUMMON_INFLIGHT.write_text(json.dumps(
        {"started": started.strftime("%H:%M:%S"), "started_epoch": int(time.time())}),
        encoding="utf-8")
    _refresh_dashboard()                               # ONE dashboard write at start — no per-2s churn
    # immediate placeholder card so the NOTE itself shows a summon is staged
    ph_body = f"*(summon initiated {started.strftime('%H:%M:%S')} — churning…)*"
    subprocess.run([sys.executable, str(STREAM), "record", "--author", "claude-api",
                    "--reply-to", head.id, "--flair", "◈ *summon staged*", "--view", vrel],
                   input=ph_body, capture_output=True, text=True, cwd=str(ROOT))
    ph_id = stream.card_id(ph_body)
    fire_reload(vrel)
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen([claude_bin, "-p", prompt],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=str(VAULT), env=env)
    except Exception as e:
        SUMMON_INFLIGHT.unlink(missing_ok=True)
        _log_api({"action": "summon", "ok": False, "error": str(e)[:200]})
        _refresh_dashboard()
        return False, f"[summon] failed: {e}"
    while proc.poll() is None:                          # tick the dashboard every 2s
        elapsed = time.monotonic() - t0
        if elapsed > 300:                               # hard ceiling
            proc.kill()
            break
        print(f"[summon] churning… {int(elapsed)}s")    # local log heartbeat only — no synced writes
        time.sleep(2)
    out, err = proc.communicate()
    dur = round(time.monotonic() - t0, 1)
    SUMMON_INFLIGHT.unlink(missing_ok=True)
    body = (out or "").strip()
    ok = proc.returncode == 0 and bool(body)
    _log_api({"action": "summon", "ok": ok, "duration_s": dur,
              "exit": proc.returncode, "chars": len(body)})
    # the staged placeholder is replaced by the outcome — the reply, or a failure note
    (rdir / f"{ph_id}.md").unlink(missing_ok=True)
    if not ok:
        fail = (f"*(summon failed {datetime.now().strftime('%H:%M:%S')} after "
                f"{int(dur)}s — exit {proc.returncode})*")
        subprocess.run([sys.executable, str(STREAM), "record", "--author", "claude-api",
                        "--reply-to", head.id, "--flair", "◈ *summon failed*", "--view", vrel],
                       input=fail, capture_output=True, text=True, cwd=str(ROOT))
        fire_reload(vrel)
        return False, f"[summon] exit={proc.returncode}: {(err or '')[:200]}"
    rec = subprocess.run([sys.executable, str(STREAM), "record", "--author", "claude-api",
                          "--reply-to", head.id, "--flair", "◈ *summoned via API*",
                          "--view", vrel],
                         input=body, capture_output=True, text=True, cwd=str(ROOT))
    fire_reload(vrel)
    return True, f"[summon] API ok in {dur}s; recorded reply\n{rec.stdout}{rec.stderr}".rstrip()


def _argv(action: str, view: str | None) -> list[str] | None:
    v = (["--view", view] if view else [])
    return {
        "run":      ["run", *v],
        "diff":     ["diff", *v],
        "fold":     ["fold", *v],          # deterministic: floating -> fish cards
        "render":   ["render", *v, "--write"],
        "validate": ["validate"],          # store-wide; ignores view
        "scan":     ["scan"],              # vault-wide; ignores view
    }.get(action)


def run_action(action: str, view: str | None = None) -> tuple[bool, str]:
    if action == "reply":                  # Reactive lane (impure: the LLM call)
        return _do_reply(view)
    if action == "summon":                 # zap button: ONE user-triggered API call
        return _do_summon(view)
    argv = _argv(action, view)
    if argv is None:
        return False, f"unknown action {action!r}"
    p = subprocess.run([sys.executable, str(STREAM), *argv],
                       capture_output=True, text=True, cwd=str(ROOT))
    ok = p.returncode in (0, 2)            # 2 = diff found changes (informational)
    if action in ("run", "render"):
        fire_reload(view)                  # nudge Obsidian after a write
    return ok, f"$ stream.py {' '.join(argv)}\n{p.stdout}{p.stderr}".rstrip()


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)   # live log even when redirected to a file
    STREAM_DIR.mkdir(exist_ok=True)
    # singleton: refuse to start if another daemon is already live (no pile-ups)
    prior = {}
    if DAEMON.exists():
        try:
            prior = json.loads(DAEMON.read_text(encoding="utf-8"))
        except ValueError:
            pass
    other = prior.get("pid")
    if other and other != os.getpid() and pathlib.Path(f"/proc/{other}").exists():
        print(f"[watch] another daemon (pid {other}) is already live — exiting")
        return 0
    print(f"[watch] polling {TRIGGER.relative_to(ROOT)} every {POLL_SECONDS}s — Ctrl-C to stop")
    _update_daemon(pid=os.getpid(),
                   started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   last_action=None)
    _refresh_dashboard()
    last_nonce = None
    while True:
        try:
            if TRIGGER.exists():
                trig = json.loads(TRIGGER.read_text(encoding="utf-8"))
                nonce = trig.get("nonce")
                if nonce != last_nonce:
                    last_nonce = nonce
                    action = trig.get("action", "run")
                    view = trig.get("view")
                    print(f"\n[watch] trigger action={action!r} view={view!r} nonce={nonce}")
                    ok, output = run_action(action, view)
                    RESULT.write_text(json.dumps(
                        {"nonce": nonce, "action": action, "ok": ok,
                         "output": output}, indent=2, ensure_ascii=False),
                        encoding="utf-8")
                    print(output)
                    _update_daemon(last_action=action,
                                   last_action_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                   last_nonce=nonce, last_ok=ok)
                    _refresh_dashboard()
                    print(f"[watch] -> result.json + dashboard ({'ok' if ok else 'ERROR'})")
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            print("\n[watch] stopped.")
            return 0
        except Exception as e:  # never let one bad trigger kill the daemon
            print(f"[watch] error: {e}")
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
