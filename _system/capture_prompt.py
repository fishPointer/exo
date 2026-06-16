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
    try:
        subprocess.run(
            [sys.executable, str(STREAM), "capture", "--view", str(THREAD)],
            input=prompt, text=True, capture_output=True, timeout=30)
    except Exception:
        pass            # never block prompt submission on a capture failure
    return 0            # silent: no stdout -> no context injection

if __name__ == "__main__":
    sys.exit(main())
