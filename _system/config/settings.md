# Managing `.claude/settings.json`

This is the Claude Code config for the vault. It's checked in, so changes ship to the team.
Per-machine overrides go in `.claude/settings.local.json` (gitignored) — use that for
anything machine-specific.

## What's wired up by default

One hook: every prompt you submit in a Claude Code session is captured as a `fish` card in
the default thread, at the source, before the model sees it.

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
        "command": "python3 \"$CLAUDE_PROJECT_DIR/_system/capture_prompt.py\"" } ] }
    ]
  }
}
```

- `$CLAUDE_PROJECT_DIR` is the vault root; the hook script self-locates from there.
- The hook is **silent** (prints nothing) — UserPromptSubmit stdout is injected into the
  model's context, so a capture hook must not pollute it.
- It's **idempotent**: if the agent later records the same body, the ids collide and the
  second write is a no-op. No double-capture.
- Capture target: the default thread (`notes/main.md`). Override per session with
  the `EXO_THREAD` env var (a path relative to the vault root).

## Common changes

- **Turn capture off:** delete the `UserPromptSubmit` block.
- **Add a permission** (so a command stops prompting): add a `permissions.allow` entry.
  Prefer `.claude/settings.local.json` for machine-local allowances.
- **Add another hook:** see Claude Code's hooks docs for events (`SessionStart`,
  `PostToolUse`, …). Keep hook scripts in `_system/`, self-locating via `__file__`, never
  with absolute paths — the vault must run from wherever it's cloned.

## Sanity check after editing

`settings.json` must be valid JSON or Claude Code ignores it:

```
python3 -c "import json; json.load(open('.claude/settings.json')); print('ok')"
```
