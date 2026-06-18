#!/usr/bin/env python3
"""
capture_prompt.py — Claude Code UserPromptSubmit hook for stream-cards.

Pipes every operator prompt into the recorder as a fish card the instant it's
sent, so TUI inputs are captured deterministically at the source — the agent's
discretion is out of the loop (the fix for "you forgot to publish some of my
inputs"). Reads the hook JSON on stdin, extracts `.prompt`, records it via the
single-source `stream.py record`, and prints NOTHING (UserPromptSubmit stdout is
injected into the model's context, so a capture hook must stay silent).

Idempotent by content-address: if the agent also records the same body, the ids
collide and the second write is a no-op — no double-capture.

Only OPERATOR prompts are cards. Synthetic messages the harness injects through
the same channel (task-notifications, system reminders, slash-command echoes) are
NOT the operator talking, so they're skipped — otherwise the thread fills with
noise like a `<task-notification>` carded as a fish post.
"""
import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime

# Self-locating: ROOT is the vault root (parent of _system/). The hook captures
# into the default thread; override with EXO_THREAD (a path relative to ROOT).
ROOT = pathlib.Path(__file__).resolve().parent.parent
STREAM = ROOT / "_system" / "stream.py"
THREAD = ROOT / os.environ.get("EXO_THREAD", "notes/main.md")

# Harness-injected, non-operator text that arrives on the prompt channel. Not a
# human turn → never carded. (Matched at the very start of the prompt.)
SYNTHETIC_PREFIXES = (
    "<task-notification>", "<system-reminder>", "<local-command-stdout>",
    "<command-message>", "<command-name>", "<command-args>",
)


def main() -> int:
    if os.environ.get("STREAM_SUMMON"):     # don't capture the summon's own prompt
        return 0
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return 0
    if prompt.startswith(SYNTHETIC_PREFIXES):   # harness noise, not the operator
        return 0
    # Bind the prompt to THIS terminal's lane, not the global head: pass the hook's
    # session_id so `capture` resolves reply_to against this session's last card. Falls
    # back to CLAUDE_CODE_SESSION_ID, then to the global head if neither is present.
    sid = (data.get("session_id") or "").strip()
    argv = [sys.executable, str(STREAM), "capture", "--view", str(THREAD)]
    if sid:
        argv += ["--session", sid]
    try:
        r = subprocess.run(
            argv, input=prompt, text=True, capture_output=True, timeout=30)
        if r.returncode != 0:                          # capture ran but failed — don't lose the turn
            _log_failure(prompt, f"exit {r.returncode}: {(r.stderr or '').strip()[:500]}")
    except Exception as e:                             # never BLOCK prompt submission, but never
        _log_failure(prompt, repr(e))                  # silently drop it either — leave a durable trace
    return 0            # silent: no stdout -> no context injection


def _log_failure(prompt: str, error: str) -> None:
    """A capture failure must not vanish — append the raw operator prompt to a local-only
    sidecar so nothing the operator typed is lost unsignalled (the hook's whole reason to exist)."""
    try:
        log = ROOT / ".stream" / "capture-failures.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                                "error": error, "prompt": prompt}, ensure_ascii=False) + "\n")
    except Exception:
        pass            # last resort — still never block the prompt


if __name__ == "__main__":
    sys.exit(main())
